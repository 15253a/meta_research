"""Deliver confirmed guidance through the active provider's native input.

The existing provider supervisor still owns launch, cancellation and outcome
reconciliation. These signed messages supplement its frozen initial input.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
import sys
import threading
import time

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.human_guidance import human_input_identities, live_guidance_prompt
import logging
from meta_research.provider_supervisor import (
    ProviderSupervisorError, read_transport_key_for_operation, read_transport_envelope, write_transport_envelope,
)


_operation = ContextVar("native_guidance_operation", default=None)
LOGGER = logging.getLogger(__name__)


@contextmanager
def live_guidance_operation(authority=None, scope=None, binding=None):
    state = {"authority": authority, "scope": scope, "binding": binding, "pumps": []}
    token = _operation.set(state)
    try:
        yield
    finally:
        for pump in reversed(state["pumps"]):
            pump.stop()
        _operation.reset(token)


def native_guidance_operation(method):
    @wraps(method)
    def call(*args, **kwargs):
        with live_guidance_operation():
            return method(*args, **kwargs)
    return call


def bind_live_guidance(authority, scope, binding):
    state = _operation.get()
    if state is not None:
        state.update(authority=authority, scope=scope, binding=binding)


def live_native_argv(argv, directory, *, enabled=True, force_transport=False, passthrough_exec=False):
    state = _operation.get()
    has_guidance = state is not None and state["binding"] is not None
    if not enabled or not (has_guidance or force_transport):
        return argv
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    _key_path, key = read_transport_key_for_operation(directory)
    original = directory / "native-provider-argv.json"
    if original.exists():
        if read_transport_envelope(original, key) != {"argv": argv}:
            raise OSError("native_provider_identity_conflict")
    else:
        write_transport_envelope(original, {"argv": argv}, key)
    if has_guidance:
        pump = _GuidancePump(state["authority"], state["scope"], state["binding"], directory, key)
        state["pumps"].append(pump)
        pump.start()
    wrapped = [sys.executable, "-m", "meta_research.native_codex_bridge",
               "--provider-argv", str(original), "--operation-directory", str(directory)]
    if passthrough_exec:
        wrapped.append("--passthrough-exec")
    # The mature supervisor binds these exact paths before launching any
    # provider. Preserve that contract for the native transport wrapper.
    for flag in ("--output-schema", "--output-last-message"):
        if flag in argv:
            wrapped.extend([flag, argv[argv.index(flag) + 1]])
    return [*wrapped, "-"]


def native_human_input_history(workspace, native_session_ref, *, exclude_directory=None):
    if native_session_ref is None:
        return ()
    observed = []
    for path in (Path(workspace) / "provider-operations").glob("*/*/native-turn-id.json"):
        directory = path.parent
        if directory == exclude_directory:
            continue
        _key_path, key = read_transport_key_for_operation(directory)
        accepted = read_transport_envelope(path, key)
        if accepted.get("thread_id") != native_session_ref:
            continue
        start = read_transport_envelope(directory / "native-turn-start.json", key)
        if (start.get("thread_id") != native_session_ref
                or accepted.get("client_user_message_id") != start.get("client_user_message_id")
                or not isinstance(accepted.get("turn_id"), str)):
            raise OwnerConflict("native_guidance_send_identity_invalid")
        observed.append(start["parameters"]["input"][0]["text"])
    for path in (Path(workspace) / "provider-operations").glob("*/*/live-guidance/*.sending"):
        directory = path.parent.parent
        if directory == exclude_directory:
            continue
        _key_path, key = read_transport_key_for_operation(directory)
        sent = read_transport_envelope(path, key)
        if sent.get("thread_id") != native_session_ref:
            continue
        if (sent.get("id") != path.stem or not isinstance(sent.get("text"), str)
                or not isinstance(sent.get("turn_id"), str)):
            raise OwnerConflict("native_guidance_send_identity_invalid")
        receipt_path = path.with_suffix(".receipt")
        if not receipt_path.exists():
            from meta_research.native_codex_bridge import NativeTerminalTurnError, reconcile_native_operation

            try:
                recovered_turn = reconcile_native_operation(directory)
            except NativeTerminalTurnError:
                recovered_turn = True
            if recovered_turn is None:
                raise OwnerConflict("human_input_delivery_pending")
            try:
                recovered = read_transport_envelope(directory / "native-reconciled.json", key)
            except ProviderSupervisorError as error:
                if isinstance(error.__cause__, FileNotFoundError):
                    raise OwnerConflict("human_input_delivery_pending") from error
                raise
            turn = recovered["turn"]
            found = (recovered["thread_id"] == native_session_ref and turn["id"] == sent["turn_id"]
                and any(item.get("type") == "userMessage" and item.get("clientId") == sent["id"]
                    and any(part.get("type") == "text" and part.get("text") == sent["text"]
                            for part in item.get("content", [])) for item in turn.get("items", [])))
            if not found:
                raise OwnerConflict("human_input_delivery_pending")
            write_transport_envelope(receipt_path, {**sent, "status": "accepted", "reconciled": True}, key)
        receipt = read_transport_envelope(receipt_path, key)
        if (receipt.get("status") != "accepted"
                or any(receipt.get(name) != value for name, value in sent.items())):
            raise OwnerConflict("native_guidance_receipt_invalid")
        observed.append(sent["text"])
    return tuple(observed)


class _GuidancePump:
    def __init__(self, authority, scope, binding, directory, key):
        self.authority, self.scope, self.binding = authority, scope, binding
        self.directory, self.key = directory, key
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        delay, last_error_log = 0.1, 0.0
        while not self._stop.wait(delay):
            try:
                messages = self.authority.prepare_live_guidance(scope=self.scope, binding=self.binding)
                for message in messages:
                    identity = canonical_hash(message)
                    path = self.directory / "live-guidance" / (identity + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if not path.exists():
                        if message.get("kind") == "human_request_response":
                            start_path = self.directory / "native-turn-start.json"
                            if not start_path.exists():
                                continue
                            start = read_transport_envelope(start_path, self.key)
                            prior = native_human_input_history(self.directory.parent.parent.parent,
                                start["thread_id"], exclude_directory=self.directory)
                            initial = start["parameters"]["input"][0]["text"]
                            if human_input_identities((live_guidance_prompt(message),)).issubset(
                                    human_input_identities((*prior, initial))):
                                continue
                        write_transport_envelope(path, {"id": identity, "message": message}, self.key)
                delay = 0.1
            except OwnerConflict as error:
                if error.code == "human_input_delivery_pending":
                    delay = 1
                    continue
                # Losing current execution authority stops this sender. It must
                # never revive a terminal root or silently launch another turn.
                try:
                    write_transport_envelope(self.directory / "live-guidance-closed.json",
                        {"binding": self.binding.as_dict(), "scope": self.scope.as_dict(),
                         "reason": error.code}, self.key)
                except OSError:
                    delay = min(5, max(1, delay * 2))
                else:
                    return
            except Exception:
                # A transient read/storage failure is not revocation or delivery
                # success. Retain every pending signed message and retry.
                if time.monotonic() - last_error_log >= 60:
                    LOGGER.warning("live guidance preparation unavailable", exc_info=True)
                    last_error_log = time.monotonic()
                delay = min(5, max(1, delay * 2))
