from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.secret_detection import contains_secret


WRITING_DELIVERY_SCHEMA = "meta-research/writing-external-delivery/v1"
WRITING_DELIVERY_TARGET_BINDING_SCHEMA = (
    "meta-research/writing-delivery-target-binding/v1"
)
LOCAL_DIRECTORY_CHAIN_BINDING_SCHEMA = (
    "meta-research/local-directory-chain-binding/v1"
)
WRITING_DELIVERY_ACTIONS = (
    "publish",
    "overwrite",
    "delete",
    "send",
    "submit",
)
LOCAL_FILESYSTEM_PROVIDER_REF = "local-filesystem"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OPERATION_REF = re.compile(r"writing_delivery:[0-9a-f]{48}\Z")
_PAYLOAD_KEYS = {
    "schema_ref",
    "request_nonce",
    "operation_ref",
    "action",
    "provider_ref",
    "target",
    "target_binding",
    "effects",
    "run_ref",
    "document_type",
    "asset_ref",
    "version_ref",
    "content_hash",
    "manifest_hash",
    "version_receipt",
    "citation_decision_ref",
    "citation_receipt",
    "renderer_asset_ref",
    "renderer_version_ref",
    "renderer_content_hash",
    "renderer_manifest_hash",
    "renderer_artifact_sha256",
    "renderer_format",
    "renderer_media_type",
    "renderer_receipt",
}
_RECEIPT_KEYS = {
    "status",
    "issuer",
    "kind",
    "receipt_ref",
    "subject_ref",
    "payload_hash",
}
_LOCAL_TARGET_KEYS = {"path", "permissions", "expected_existing_hash"}
_REMOTE_TARGET_KEYS = {"target_ref", "permissions", "expected_existing_hash"}
_TARGET_BINDING_KEYS = {
    "schema_ref",
    "provider_ref",
    "action",
    "target_hash",
    "provider_binding",
    "provider_binding_hash",
}


class WritingDeliveryOutcomeUnknown(RuntimeError):
    """The provider may have applied the effect but did not return an ACK."""


@dataclass(frozen=True)
class WritingDeliveryProviderRequest:
    operation_ref: str
    provider_operation_ref: str
    action: str
    target: dict[str, object]
    target_binding: dict[str, object]
    artifact: bytes | None
    artifact_sha256: str
    request_hash: str


@dataclass(frozen=True)
class WritingDeliveryProviderObservation:
    observation_ref: str
    provider_ref: str
    provider_operation_ref: str
    outcome: str
    observed_at: float
    details: dict[str, object]

    @property
    def observation_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_ref": self.observation_ref,
            "provider_ref": self.provider_ref,
            "provider_operation_ref": self.provider_operation_ref,
            "outcome": self.outcome,
            "observed_at": self.observed_at,
            "details": self.details,
        }


class WritingDeliveryProvider(Protocol):
    provider_ref: str
    production_ready: bool
    supported_actions: frozenset[str]

    def request(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        target_binding: dict[str, object],
        artifact: bytes | None,
        artifact_sha256: str,
    ) -> WritingDeliveryProviderRequest: ...

    def execute(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation: ...

    def reconcile(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation: ...

    def verify_target_current(
        self, action: str, target: dict[str, object]
    ) -> dict[str, object]: ...


def _require_token(value: object, code: str = "writing_delivery_payload_invalid") -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise OwnerConflict(code)
    return value


def _require_hash(value: object, code: str = "writing_delivery_payload_invalid") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise OwnerConflict(code)
    return value


def _normalized_receipt(
    value: object,
    *,
    issuer: str,
    subject_ref: str,
    code: str = "writing_delivery_payload_invalid",
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
        raise OwnerConflict(code)
    if (
        value.get("status") != "accepted"
        or value.get("issuer") != issuer
        or value.get("subject_ref") != subject_ref
    ):
        raise OwnerConflict(code)
    result: dict[str, str] = {}
    for key in _RECEIPT_KEYS:
        candidate = value.get(key)
        if not isinstance(candidate, str) or not candidate:
            raise OwnerConflict(code)
        result[key] = candidate
    _require_token(result["kind"], code)
    _require_token(result["receipt_ref"], code)
    _require_token(result["subject_ref"], code)
    _require_hash(result["payload_hash"], code)
    return {key: result[key] for key in sorted(_RECEIPT_KEYS)}


def normalize_writing_delivery_target(
    provider_ref: str,
    action: str,
    target: object,
) -> dict[str, object]:
    """Normalize the exact target frozen into HC's impact preview.

    Local filesystem effects deliberately use a narrow, real production
    contract. Other providers share an opaque target reference and explicit
    permission scopes, leaving send/submit adapters extensible without making
    any such adapter a built-in production capability.
    """

    _require_token(provider_ref, "writing_delivery_target_invalid")
    if action not in WRITING_DELIVERY_ACTIONS or not isinstance(target, dict):
        raise OwnerConflict("writing_delivery_target_invalid")
    if provider_ref == LOCAL_FILESYSTEM_PROVIDER_REF:
        if action not in {"publish", "overwrite", "delete"} or set(target) != _LOCAL_TARGET_KEYS:
            raise OwnerConflict("writing_delivery_target_invalid")
        raw_path = target.get("path")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or len(raw_path) > 4096
            or "\x00" in raw_path
        ):
            raise OwnerConflict("writing_delivery_target_invalid")
        path = Path(raw_path)
        if not path.is_absolute():
            raise OwnerConflict("writing_delivery_target_invalid")
        try:
            normalized = str(path.resolve(strict=False))
        except (OSError, RuntimeError) as error:
            raise OwnerConflict("writing_delivery_target_invalid") from error
        if raw_path != normalized or normalized == os.path.sep:
            raise OwnerConflict("writing_delivery_target_invalid")
        permissions = target.get("permissions")
        if type(permissions) is not int or permissions != 0o600:
            raise OwnerConflict("writing_delivery_target_invalid")
        expected = target.get("expected_existing_hash")
        if action == "publish":
            if expected is not None:
                raise OwnerConflict("writing_delivery_target_invalid")
        else:
            _require_hash(expected, "writing_delivery_target_invalid")
        return {
            "path": normalized,
            "permissions": permissions,
            "expected_existing_hash": expected,
        }

    if action not in {"send", "submit", "publish"} or set(target) != _REMOTE_TARGET_KEYS:
        raise OwnerConflict("writing_delivery_target_invalid")
    target_ref = _require_token(target.get("target_ref"), "writing_delivery_target_invalid")
    permissions = target.get("permissions")
    if (
        not isinstance(permissions, list)
        or not permissions
        or len(permissions) > 32
        or any(
            not isinstance(item, str)
            or _SAFE_TOKEN.fullmatch(item) is None
            for item in permissions
        )
        or len(set(permissions)) != len(permissions)
    ):
        raise OwnerConflict("writing_delivery_target_invalid")
    expected = target.get("expected_existing_hash")
    if expected is not None:
        _require_hash(expected, "writing_delivery_target_invalid")
    return {
        "target_ref": target_ref,
        "permissions": list(permissions),
        "expected_existing_hash": expected,
    }


def _expected_effect(
    provider_ref: str, action: str, target: dict[str, object]
) -> dict[str, object]:
    target_ref = target.get("path", target.get("target_ref"))
    effect_kind = {
        "publish": "create_file" if provider_ref == LOCAL_FILESYSTEM_PROVIDER_REF else "publish",
        "overwrite": "overwrite_file",
        "delete": "delete_file",
        "send": "send",
        "submit": "submit",
    }[action]
    return {
        "effect_kind": effect_kind,
        "target_ref": target_ref,
        "destructive": action in {"overwrite", "delete", "send", "submit"},
    }


def writing_delivery_effects(
    provider_ref: str, action: str, target: object
) -> list[dict[str, object]]:
    normalized_target = normalize_writing_delivery_target(
        provider_ref, action, target
    )
    return [_expected_effect(provider_ref, action, normalized_target)]


def normalize_writing_delivery_target_binding(
    provider_ref: str,
    action: str,
    target: object,
    value: object,
) -> dict[str, object]:
    """Validate the provider-owned identity captured for one exact target.

    The outer envelope is provider-neutral so remote adapters can bind an ETag,
    immutable recipient identity, or another provider-native token.  Local
    inode identities stay out of the caller-supplied target envelope.
    """

    normalized_target = normalize_writing_delivery_target(
        provider_ref, action, target
    )
    if not isinstance(value, dict) or set(value) != _TARGET_BINDING_KEYS:
        raise OwnerConflict("writing_delivery_target_binding_invalid")
    provider_binding = value.get("provider_binding")
    if not isinstance(provider_binding, dict) or not provider_binding:
        raise OwnerConflict("writing_delivery_target_binding_invalid")
    try:
        serialized = canonical_json(provider_binding)
        normalized_provider_binding = json.loads(serialized)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("writing_delivery_target_binding_invalid") from error
    if (
        not isinstance(normalized_provider_binding, dict)
        or len(serialized.encode("utf-8")) > 64 * 1024
        or contains_secret(normalized_provider_binding)
    ):
        raise OwnerConflict("writing_delivery_target_binding_invalid")
    target_hash = canonical_hash(normalized_target)
    provider_binding_hash = canonical_hash(normalized_provider_binding)
    if (
        value.get("schema_ref") != WRITING_DELIVERY_TARGET_BINDING_SCHEMA
        or value.get("provider_ref") != provider_ref
        or value.get("action") != action
        or value.get("target_hash") != target_hash
        or value.get("provider_binding_hash") != provider_binding_hash
    ):
        raise OwnerConflict("writing_delivery_target_binding_invalid")
    return {
        "schema_ref": WRITING_DELIVERY_TARGET_BINDING_SCHEMA,
        "provider_ref": provider_ref,
        "action": action,
        "target_hash": target_hash,
        "provider_binding": normalized_provider_binding,
        "provider_binding_hash": provider_binding_hash,
    }


def _target_binding_document(
    provider_ref: str,
    action: str,
    target: dict[str, object],
    provider_binding: dict[str, object],
) -> dict[str, object]:
    return normalize_writing_delivery_target_binding(
        provider_ref,
        action,
        target,
        {
            "schema_ref": WRITING_DELIVERY_TARGET_BINDING_SCHEMA,
            "provider_ref": provider_ref,
            "action": action,
            "target_hash": canonical_hash(target),
            "provider_binding": provider_binding,
            "provider_binding_hash": canonical_hash(provider_binding),
        },
    )


def derive_writing_delivery_operation_ref(value: object) -> str:
    if not isinstance(value, dict) or "operation_ref" in value:
        raise OwnerConflict("writing_delivery_payload_invalid")
    if set(value) != _PAYLOAD_KEYS - {"operation_ref"}:
        raise OwnerConflict("writing_delivery_payload_invalid")
    return f"writing_delivery:{canonical_hash(value)[:48]}"


def normalize_writing_delivery_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PAYLOAD_KEYS:
        raise OwnerConflict("writing_delivery_payload_invalid")
    if value.get("schema_ref") != WRITING_DELIVERY_SCHEMA:
        raise OwnerConflict("writing_delivery_payload_invalid")
    request_nonce = _require_token(value.get("request_nonce"))
    operation_ref = value.get("operation_ref")
    if not isinstance(operation_ref, str) or _OPERATION_REF.fullmatch(operation_ref) is None:
        raise OwnerConflict("writing_delivery_payload_invalid")
    action = value.get("action")
    provider_ref = value.get("provider_ref")
    if action not in WRITING_DELIVERY_ACTIONS or not isinstance(provider_ref, str):
        raise OwnerConflict("writing_delivery_payload_invalid")
    target = normalize_writing_delivery_target(provider_ref, action, value.get("target"))
    target_binding = normalize_writing_delivery_target_binding(
        provider_ref,
        action,
        target,
        value.get("target_binding"),
    )
    effects = value.get("effects")
    if effects != writing_delivery_effects(provider_ref, action, target):
        raise OwnerConflict("writing_delivery_payload_invalid")
    document_type = value.get("document_type")
    if document_type not in {"report", "paper", "presentation"}:
        raise OwnerConflict("writing_delivery_payload_invalid")
    result: dict[str, object] = {
        "schema_ref": WRITING_DELIVERY_SCHEMA,
        "request_nonce": request_nonce,
        "operation_ref": operation_ref,
        "action": action,
        "provider_ref": _require_token(provider_ref),
        "target": target,
        "target_binding": target_binding,
        "effects": list(effects),
        "run_ref": _require_token(value.get("run_ref")),
        "document_type": document_type,
        "asset_ref": _require_token(value.get("asset_ref")),
        "version_ref": _require_token(value.get("version_ref")),
        "content_hash": _require_hash(value.get("content_hash")),
        "manifest_hash": _require_hash(value.get("manifest_hash")),
        "citation_decision_ref": _require_token(value.get("citation_decision_ref")),
        "renderer_asset_ref": _require_token(value.get("renderer_asset_ref")),
        "renderer_version_ref": _require_token(value.get("renderer_version_ref")),
        "renderer_content_hash": _require_hash(value.get("renderer_content_hash")),
        "renderer_manifest_hash": _require_hash(value.get("renderer_manifest_hash")),
        "renderer_artifact_sha256": _require_hash(value.get("renderer_artifact_sha256")),
    }
    for key in ("renderer_format", "renderer_media_type"):
        candidate = value.get(key)
        if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 255:
            raise OwnerConflict("writing_delivery_payload_invalid")
        result[key] = candidate
    result["version_receipt"] = _normalized_receipt(
        value.get("version_receipt"),
        issuer="research_memory",
        subject_ref=str(result["version_ref"]),
    )
    result["citation_receipt"] = _normalized_receipt(
        value.get("citation_receipt"),
        issuer="research_graph",
        subject_ref=str(result["citation_decision_ref"]),
    )
    result["renderer_receipt"] = _normalized_receipt(
        value.get("renderer_receipt"),
        issuer="research_memory",
        subject_ref=str(result["renderer_version_ref"]),
    )
    without_ref = dict(result)
    without_ref.pop("operation_ref")
    if derive_writing_delivery_operation_ref(without_ref) != operation_ref:
        raise OwnerConflict("writing_delivery_operation_identity_invalid")
    return result


class WritingDeliveryProviderRegistry:
    def __init__(self, providers: tuple[WritingDeliveryProvider, ...] = ()) -> None:
        self._providers: dict[str, WritingDeliveryProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: WritingDeliveryProvider) -> None:
        provider_ref = _require_token(
            getattr(provider, "provider_ref", None),
            "writing_delivery_provider_invalid",
        )
        if provider_ref in self._providers and self._providers[provider_ref] is not provider:
            raise OwnerConflict("writing_delivery_provider_conflict")
        actions = getattr(provider, "supported_actions", None)
        if (
            not isinstance(actions, frozenset)
            or not actions
            or not actions <= set(WRITING_DELIVERY_ACTIONS)
            or type(getattr(provider, "production_ready", None)) is not bool
            or not callable(getattr(provider, "request", None))
            or not callable(getattr(provider, "verify_target_current", None))
            or not callable(getattr(provider, "execute", None))
            or not callable(getattr(provider, "reconcile", None))
        ):
            raise OwnerConflict("writing_delivery_provider_invalid")
        self._providers[provider_ref] = provider

    def require(
        self, provider_ref: str, *, production: bool = False
    ) -> WritingDeliveryProvider:
        provider = self._providers.get(provider_ref)
        if provider is None:
            raise OwnerConflict("writing_delivery_provider_unavailable")
        if production and not provider.production_ready:
            raise OwnerConflict("writing_delivery_provider_not_production")
        return provider

    def capabilities(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "provider_ref": provider.provider_ref,
                "production_ready": provider.production_ready,
                "supported_actions": sorted(provider.supported_actions),
            }
            for provider in sorted(
                self._providers.values(), key=lambda item: item.provider_ref
            )
        )

    def verify_target_current(
        self,
        provider_ref: str,
        action: str,
        target: object,
        *,
        production: bool = False,
        target_binding: object | None = None,
    ) -> dict[str, object]:
        provider = self.require(provider_ref, production=production)
        if action not in provider.supported_actions:
            raise OwnerConflict("writing_delivery_action_unavailable")
        normalized = normalize_writing_delivery_target(
            provider_ref, action, target
        )
        observed = normalize_writing_delivery_target_binding(
            provider_ref,
            action,
            normalized,
            provider.verify_target_current(action, normalized),
        )
        if target_binding is not None:
            expected = normalize_writing_delivery_target_binding(
                provider_ref,
                action,
                normalized,
                target_binding,
            )
            if observed != expected:
                raise OwnerConflict("writing_delivery_target_stale")
        return observed


class LocalFilesystemWritingDeliveryProvider:
    provider_ref = LOCAL_FILESYSTEM_PROVIDER_REF
    production_ready = True
    # A portable filesystem does not expose atomic compare-and-overwrite/delete.
    # Advertising those actions as production-ready would leave a TOCTOU window
    # in which a confirmed target could be replaced by unrelated user data.
    # Future providers may expose them only with a real CAS/ETag primitive.
    supported_actions = frozenset({"publish"})

    def request(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        target_binding: dict[str, object],
        artifact: bytes | None,
        artifact_sha256: str,
    ) -> WritingDeliveryProviderRequest:
        if _OPERATION_REF.fullmatch(operation_ref) is None or action not in self.supported_actions:
            raise OwnerConflict("writing_delivery_provider_request_invalid")
        normalized_target = normalize_writing_delivery_target(
            self.provider_ref, action, target
        )
        normalized_binding = normalize_writing_delivery_target_binding(
            self.provider_ref,
            action,
            normalized_target,
            target_binding,
        )
        self._local_directory_binding(
            Path(str(normalized_target["path"])).parent,
            normalized_binding,
        )
        _require_hash(artifact_sha256, "writing_delivery_provider_request_invalid")
        if action == "delete":
            if artifact is not None:
                raise OwnerConflict("writing_delivery_provider_request_invalid")
        elif artifact is not None and (
            not isinstance(artifact, bytes)
            or hashlib.sha256(artifact).hexdigest() != artifact_sha256
        ):
            raise OwnerConflict("writing_delivery_artifact_invalid")
        provider_operation_ref = operation_ref
        request_hash = canonical_hash(
            {
                "provider_ref": self.provider_ref,
                "provider_operation_ref": provider_operation_ref,
                "action": action,
                "target": normalized_target,
                "target_binding": normalized_binding,
                "artifact_sha256": artifact_sha256,
            }
        )
        return WritingDeliveryProviderRequest(
            operation_ref=operation_ref,
            provider_operation_ref=provider_operation_ref,
            action=action,
            target=normalized_target,
            target_binding=normalized_binding,
            artifact=artifact,
            artifact_sha256=artifact_sha256,
            request_hash=request_hash,
        )

    def execute(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation:
        self._validate_request(request)
        path = Path(str(request.target["path"]))
        if request.action == "publish":
            if request.artifact is None:
                raise OwnerConflict("writing_delivery_artifact_unavailable")
            target_hash = self._publish_no_replace(
                path,
                request.artifact,
                request.target_binding,
            )
        elif request.action == "overwrite":
            expected_hash = str(request.target["expected_existing_hash"])
            self._verify_existing(path, expected_hash)
            if request.artifact is None:
                raise OwnerConflict("writing_delivery_artifact_unavailable")
            self._replace(path, request.artifact, expected_hash)
            target_hash = self._target_hash(path)
        else:
            self._verify_existing(path, str(request.target["expected_existing_hash"]))
            path.unlink()
            self._fsync_directory(path.parent)
            target_hash = self._target_hash(path)
        return self._observation(request, "completed", {"target_hash": target_hash})

    def verify_target_current(
        self, action: str, target: dict[str, object]
    ) -> dict[str, object]:
        normalized = normalize_writing_delivery_target(
            self.provider_ref, action, target
        )
        path = Path(str(normalized["path"]))
        parent_descriptor, parent_chain_digest, component_count = (
            self._open_parent_chain(path.parent)
        )
        try:
            if action == "publish":
                try:
                    os.stat(
                        path.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise OwnerConflict("writing_delivery_target_already_exists")
            else:
                expected = str(normalized["expected_existing_hash"])
                self._verify_existing(path, expected)
        finally:
            os.close(parent_descriptor)
        return _target_binding_document(
            self.provider_ref,
            action,
            normalized,
            {
                "schema_ref": LOCAL_DIRECTORY_CHAIN_BINDING_SCHEMA,
                "parent_path_hash": canonical_hash(str(path.parent)),
                "parent_chain_digest": parent_chain_digest,
                "component_count": component_count,
            },
        )

    @staticmethod
    def _open_parent_chain(
        parent: Path,
        expected_chain_digest: str | None = None,
        expected_component_count: int | None = None,
    ) -> tuple[int, str, int]:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        no_follow_flag = getattr(os, "O_NOFOLLOW", None)
        if directory_flag is None or no_follow_flag is None:
            raise OwnerConflict("writing_delivery_target_parent_unavailable")
        parts = parent.parts
        if not parts or parts[0] != os.path.sep:
            raise OwnerConflict("writing_delivery_target_parent_unavailable")
        flags = (
            os.O_RDONLY
            | directory_flag
            | no_follow_flag
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor: int | None = None
        identities: list[dict[str, int]] = []
        try:
            descriptor = os.open(os.path.sep, flags)
            root_info = os.fstat(descriptor)
            if not stat.S_ISDIR(root_info.st_mode):
                raise OwnerConflict("writing_delivery_target_parent_unavailable")
            root_identity = {
                "device": int(root_info.st_dev),
                "inode": int(root_info.st_ino),
            }
            identities.append(root_identity)
            for component in parts[1:]:
                if component in {"", ".", ".."}:
                    raise OwnerConflict(
                        "writing_delivery_target_parent_unavailable"
                    )
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
                info = os.fstat(descriptor)
                if not stat.S_ISDIR(info.st_mode):
                    raise OwnerConflict(
                        "writing_delivery_target_parent_unavailable"
                    )
                identity = {
                    "device": int(info.st_dev),
                    "inode": int(info.st_ino),
                }
                identities.append(identity)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise OwnerConflict(
                "writing_delivery_target_stale"
                if expected_chain_digest is not None
                else "writing_delivery_target_parent_unavailable"
            ) from error
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        if descriptor is None:
            raise OwnerConflict("writing_delivery_target_parent_unavailable")
        chain_digest = LocalFilesystemWritingDeliveryProvider._directory_chain_digest(
            identities
        )
        if (
            expected_chain_digest is not None
            and (
                chain_digest != expected_chain_digest
                or len(identities) != expected_component_count
            )
        ):
            os.close(descriptor)
            raise OwnerConflict("writing_delivery_target_stale")
        return descriptor, chain_digest, len(identities)

    @staticmethod
    def _directory_chain_digest(
        identities: list[dict[str, int]],
    ) -> str:
        return canonical_hash(
            {
                "schema_ref": LOCAL_DIRECTORY_CHAIN_BINDING_SCHEMA,
                "directory_identities": identities,
            }
        )

    def reconcile(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation:
        self._validate_request(request)
        path = Path(str(request.target["path"]))
        actual_hash = self._target_hash_at_binding(
            path,
            request.target_binding,
        )
        if request.action == "delete":
            outcome = "completed" if actual_hash is None else (
                "not_found"
                if actual_hash == request.target["expected_existing_hash"]
                else "partial"
            )
        else:
            outcome = (
                "completed"
                if actual_hash == request.artifact_sha256
                else "not_found" if actual_hash is None else "partial"
            )
        return self._observation(request, outcome, {"target_hash": actual_hash})

    def _target_hash_at_binding(
        self,
        path: Path,
        target_binding: dict[str, object],
    ) -> str | None:
        parent_descriptor = self._open_confirmed_parent(
            path.parent,
            target_binding,
        )
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(
                    path.name,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                return None
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OwnerConflict("writing_delivery_target_not_regular_file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise OwnerConflict("writing_delivery_target_permissions_stale")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            current_parent = self._open_confirmed_parent(
                path.parent,
                target_binding,
            )
            os.close(current_parent)
            return digest.hexdigest()
        except OSError as error:
            raise OwnerConflict("writing_delivery_target_stale") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _validate_request(self, request: WritingDeliveryProviderRequest) -> None:
        if (
            request.provider_operation_ref != request.operation_ref
            or request.action not in self.supported_actions
            or request.target
            != normalize_writing_delivery_target(
                self.provider_ref, request.action, request.target
            )
            or request.target_binding
            != normalize_writing_delivery_target_binding(
                self.provider_ref,
                request.action,
                request.target,
                request.target_binding,
            )
        ):
            raise OwnerConflict("writing_delivery_provider_request_invalid")
        self._local_directory_binding(
            Path(str(request.target["path"])).parent,
            request.target_binding,
        )

    def _publish_no_replace(
        self,
        path: Path,
        content: bytes,
        target_binding: dict[str, object],
    ) -> str:
        """Create one file relative to an already-open, confirmed directory.

        Every filesystem effect is dirfd-relative.  Replacing the human-
        confirmed parent pathname with a symlink after confirmation therefore
        cannot redirect the artifact into another directory.
        """

        parent_descriptor = self._open_confirmed_parent(
            path.parent,
            target_binding,
        )
        temporary_name = (
            f".{path.name}.writing-delivery-{os.getpid()}-{time.time_ns()}"
        )
        linked = False
        try:
            try:
                os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise OwnerConflict("writing_delivery_target_already_exists")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(parent_descriptor)
            try:
                current_parent = self._open_confirmed_parent(
                    path.parent,
                    target_binding,
                )
            except OwnerConflict as stale_error:
                try:
                    os.unlink(path.name, dir_fd=parent_descriptor)
                    linked = False
                    os.fsync(parent_descriptor)
                except FileNotFoundError:
                    linked = False
                except OSError as cleanup_error:
                    raise WritingDeliveryOutcomeUnknown(
                        "provider_outcome_unknown"
                    ) from cleanup_error
                raise stale_error
            else:
                os.close(current_parent)
        except FileExistsError as error:
            raise OwnerConflict("writing_delivery_target_already_exists") from error
        except OwnerConflict:
            raise
        except OSError as error:
            if linked:
                raise WritingDeliveryOutcomeUnknown(
                    "provider_outcome_unknown"
                ) from error
            raise OwnerConflict("writing_delivery_provider_write_failed") from error
        finally:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_descriptor)
        return hashlib.sha256(content).hexdigest()

    @classmethod
    def _open_confirmed_parent(
        cls,
        parent: Path,
        target_binding: dict[str, object],
    ) -> int:
        expected_digest, expected_component_count = cls._local_directory_binding(
            parent,
            target_binding,
        )
        descriptor, _digest, _component_count = cls._open_parent_chain(
            parent,
            expected_digest,
            expected_component_count,
        )
        return descriptor

    @staticmethod
    def _local_directory_binding(
        parent: Path,
        target_binding: dict[str, object],
    ) -> tuple[str, int]:
        provider_binding = target_binding.get("provider_binding")
        if (
            not isinstance(provider_binding, dict)
            or set(provider_binding)
            != {
                "schema_ref",
                "parent_path_hash",
                "parent_chain_digest",
                "component_count",
            }
            or provider_binding.get("schema_ref")
            != LOCAL_DIRECTORY_CHAIN_BINDING_SCHEMA
            or provider_binding.get("parent_path_hash")
            != canonical_hash(str(parent))
        ):
            raise OwnerConflict("writing_delivery_target_binding_invalid")
        parent_chain_digest = provider_binding.get("parent_chain_digest")
        component_count = provider_binding.get("component_count")
        if (
            not isinstance(parent_chain_digest, str)
            or _SHA256.fullmatch(parent_chain_digest) is None
            or type(component_count) is not int
            or component_count != len(parent.parts)
        ):
            raise OwnerConflict("writing_delivery_target_binding_invalid")
        return parent_chain_digest, component_count

    def _replace(self, path: Path, content: bytes, expected_hash: str) -> None:
        temporary = self._write_temporary(path, content)
        try:
            # Recheck immediately before the atomic name replacement. This
            # refuses a concurrent target mutation instead of overwriting it.
            self._verify_existing(path, expected_hash)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_temporary(path: Path, content: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.writing-delivery-", dir=path.parent
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _target_hash(path: Path) -> str | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OwnerConflict("writing_delivery_target_not_regular_file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verify_existing(cls, path: Path, expected_hash: str) -> None:
        actual_hash = cls._target_hash(path)
        if actual_hash is None:
            raise OwnerConflict("writing_delivery_target_missing")
        if actual_hash != expected_hash:
            raise OwnerConflict("writing_delivery_target_stale")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise OwnerConflict("writing_delivery_target_permissions_stale")

    def _observation(
        self,
        request: WritingDeliveryProviderRequest,
        outcome: str,
        details: dict[str, object],
    ) -> WritingDeliveryProviderObservation:
        observed_at = time.time()
        return WritingDeliveryProviderObservation(
            observation_ref=(
                f"provider_observation:{canonical_hash({'operation_ref': request.operation_ref, 'outcome': outcome, 'details': details, 'observed_at': observed_at})[:48]}"
            ),
            provider_ref=self.provider_ref,
            provider_operation_ref=request.provider_operation_ref,
            outcome=outcome,
            observed_at=observed_at,
            details=details,
        )


class InMemoryWritingDeliveryProvider:
    """Deterministic conformance adapter; never a production capability."""

    provider_ref = "sandbox-memory"
    production_ready = False
    supported_actions = frozenset(WRITING_DELIVERY_ACTIONS)

    def __init__(self) -> None:
        self._completed: dict[str, WritingDeliveryProviderRequest] = {}

    def request(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        target_binding: dict[str, object],
        artifact: bytes | None,
        artifact_sha256: str,
    ) -> WritingDeliveryProviderRequest:
        if _OPERATION_REF.fullmatch(operation_ref) is None or action not in self.supported_actions:
            raise OwnerConflict("writing_delivery_provider_request_invalid")
        normalized_target = normalize_writing_delivery_target(
            self.provider_ref, action, target
        )
        normalized_binding = normalize_writing_delivery_target_binding(
            self.provider_ref,
            action,
            normalized_target,
            target_binding,
        )
        if action != "delete" and artifact is not None and (
            not isinstance(artifact, bytes)
            or hashlib.sha256(artifact).hexdigest() != artifact_sha256
        ):
            raise OwnerConflict("writing_delivery_artifact_invalid")
        request_hash = canonical_hash(
            {
                "operation_ref": operation_ref,
                "action": action,
                "target": normalized_target,
                "target_binding": normalized_binding,
                "artifact_sha256": artifact_sha256,
            }
        )
        return WritingDeliveryProviderRequest(
            operation_ref=operation_ref,
            provider_operation_ref=operation_ref,
            action=action,
            target=normalized_target,
            target_binding=normalized_binding,
            artifact=artifact,
            artifact_sha256=artifact_sha256,
            request_hash=request_hash,
        )

    def execute(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation:
        if request.action != "delete" and request.artifact is None:
            raise OwnerConflict("writing_delivery_artifact_unavailable")
        self._completed[request.operation_ref] = request
        return self._observation(request, "completed")

    def reconcile(
        self, request: WritingDeliveryProviderRequest
    ) -> WritingDeliveryProviderObservation:
        return self._observation(
            request,
            "completed" if request.operation_ref in self._completed else "not_found",
        )

    def verify_target_current(
        self, action: str, target: dict[str, object]
    ) -> dict[str, object]:
        normalized = normalize_writing_delivery_target(
            self.provider_ref, action, target
        )
        return _target_binding_document(
            self.provider_ref,
            action,
            normalized,
            {
                "schema_ref": "meta-research/sandbox-target-binding/v1",
                "target_hash": canonical_hash(normalized),
            },
        )

    def _observation(
        self, request: WritingDeliveryProviderRequest, outcome: str
    ) -> WritingDeliveryProviderObservation:
        observed_at = time.time()
        return WritingDeliveryProviderObservation(
            observation_ref=f"sandbox_observation:{canonical_hash({'operation_ref': request.operation_ref, 'outcome': outcome, 'observed_at': observed_at})[:48]}",
            provider_ref=self.provider_ref,
            provider_operation_ref=request.provider_operation_ref,
            outcome=outcome,
            observed_at=observed_at,
            details={"sandbox": True},
        )


def default_writing_delivery_registry() -> WritingDeliveryProviderRegistry:
    return WritingDeliveryProviderRegistry((LocalFilesystemWritingDeliveryProvider(),))
