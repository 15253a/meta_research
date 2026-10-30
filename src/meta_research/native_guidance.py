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
import logging
from meta_research.provider_supervisor import (
    read_transport_key_for_operation, read_transport_envelope, write_transport_envelope,
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


def live_native_argv(argv, directory, *, enabled=True):
    state = _operation.get()
    if not enabled or state is None or state["binding"] is None:
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
    pump = _GuidancePump(state["authority"], state["scope"], state["binding"], directory, key)
    state["pumps"].append(pump)
    pump.start()
    wrapped = [sys.executable, "-m", "meta_research.native_codex_bridge",
               "--provider-argv", str(original), "--operation-directory", str(directory)]
    # The mature supervisor binds these exact paths before launching any
    # provider. Preserve that contract for the native transport wrapper.
    for flag in ("--output-schema", "--output-last-message"):
        if flag in argv:
            wrapped.extend([flag, argv[argv.index(flag) + 1]])
    return [*wrapped, "-"]


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
                        write_transport_envelope(path, {"id": identity, "message": message}, self.key)
                delay = 0.1
            except OwnerConflict as error:
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
