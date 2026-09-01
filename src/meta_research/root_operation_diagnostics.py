from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    ensure_transport_key,
    read_transport_envelope,
    write_transport_envelope,
)
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    RootAgentKind,
    validate_root_capability_diagnostics,
)


ROOT_OPERATION_DIAGNOSTIC_RECORD_SCHEMA = (
    "meta-research/root-operation-diagnostic-record/v1"
)
ROOT_OPERATION_DIAGNOSTIC_PAGE_SCHEMA = (
    "meta-research/root-operation-diagnostic-page/v1"
)


class RootOperationDiagnosticError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RootOperationDiagnosticRecorder(Protocol):
    def record(
        self,
        *,
        operation_ref: str,
        root_kind: RootAgentKind,
        diagnostics: dict[str, object],
    ) -> "RootOperationDiagnosticRecord": ...


@dataclass(frozen=True)
class RootOperationDiagnosticRecord:
    operation_ref: str
    root_kind: RootAgentKind
    diagnostics: dict[str, object]
    diagnostics_hash: str

    def as_public_dict(self) -> dict[str, object]:
        return {
            "schema_ref": ROOT_OPERATION_DIAGNOSTIC_RECORD_SCHEMA,
            "operation_ref": self.operation_ref,
            "root_kind": self.root_kind,
            "diagnostics": self.diagnostics,
            "diagnostics_hash": self.diagnostics_hash,
        }


def root_operation_diagnostic_ref(
    root_kind: RootAgentKind,
    *,
    source_ref: str,
    phase: str,
) -> str:
    if (
        root_kind not in ROOT_AGENT_KINDS
        or not source_ref
        or len(source_ref) > 2_048
        or not phase
        or len(phase) > 256
    ):
        raise RootOperationDiagnosticError(
            "root_operation_diagnostic_identity_invalid"
        )
    return f"root-operation:{root_kind}:" + canonical_hash(
        {
            "root_kind": root_kind,
            "source_ref": source_ref,
            "phase": phase,
        }
    )


class RootOperationDiagnosticStore:
    """Signed diagnostic-only sidecars, independent from domain acceptance.

    The store deliberately has no callback into an Owner and no participation
    in provider identity or Stage gates.  Adapters write it best-effort after a
    real turn; readers receive only strictly revalidated sealed records.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._records = root / "records"
        # Initialization is lazy: an unavailable diagnostic disk must not
        # prevent the production runtime itself from starting.
        self._key: bytes | None = None

    def record(
        self,
        *,
        operation_ref: str,
        root_kind: RootAgentKind,
        diagnostics: dict[str, object],
    ) -> RootOperationDiagnosticRecord:
        record = self._validated_record(
            {
                "schema_ref": ROOT_OPERATION_DIAGNOSTIC_RECORD_SCHEMA,
                "operation_ref": operation_ref,
                "root_kind": root_kind,
                "diagnostics": diagnostics,
                "diagnostics_hash": canonical_hash(diagnostics),
            }
        )
        try:
            self._records.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._records.is_symlink() or not self._records.is_dir():
                raise OSError("diagnostic record root is not a directory")
            key = self._key_for_write()
            write_transport_envelope(
                self._path_for(operation_ref),
                record.as_public_dict(),
                key,
            )
        except (OSError, ProviderSupervisorError) as error:
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_unavailable"
            ) from error
        return record

    def query(
        self, operation_ref: str
    ) -> RootOperationDiagnosticRecord | None:
        path = self._path_for(operation_ref)
        if not path.exists():
            return None
        return self._read(path)

    def query_page(
        self,
        *,
        root_kind: RootAgentKind | None = None,
        operation_ref: str | None = None,
        limit: int = 128,
    ) -> dict[str, object]:
        if (
            root_kind is not None
            and root_kind not in ROOT_AGENT_KINDS
            or isinstance(limit, bool)
            or not 1 <= limit <= 256
        ):
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_query_invalid"
            )
        if operation_ref is not None:
            record = self.query(operation_ref)
            records = (
                []
                if record is None
                or root_kind is not None
                and record.root_kind != root_kind
                else [record]
            )
            return self._public_page(records, limit=limit)
        records: list[RootOperationDiagnosticRecord] = []
        if not self._records.exists():
            return self._public_page(records, limit=limit)
        if self._records.is_symlink() or not self._records.is_dir():
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_store_unavailable"
            )
        try:
            paths = sorted(self._records.glob("*.json"))
        except OSError as error:
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_store_unavailable"
            ) from error
        for path in paths:
            record = self._read(path)
            if root_kind is not None and record.root_kind != root_kind:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return self._public_page(records, limit=limit)

    @staticmethod
    def _public_page(
        records: list[RootOperationDiagnosticRecord],
        *,
        limit: int,
    ) -> dict[str, object]:
        counts = {
            kind: sum(record.root_kind == kind for record in records)
            for kind in ROOT_AGENT_KINDS
        }
        return {
            "schema_ref": ROOT_OPERATION_DIAGNOSTIC_PAGE_SCHEMA,
            "status": "observed" if records else "not_observed",
            "root_kinds": list(ROOT_AGENT_KINDS),
            "page_counts": counts,
            "items": [record.as_public_dict() for record in records],
            "limit": limit,
        }

    def _key_for_write(self) -> bytes:
        if self._key is not None:
            return self._key
        try:
            _key_path, self._key = ensure_transport_key(self._root)
        except (OSError, ProviderSupervisorError) as error:
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_store_unavailable"
            ) from error
        return self._key

    def _key_for_read(self) -> bytes:
        if self._key is not None:
            return self._key
        key_path = self._root / "provider-operations" / ".transport-seal.key"
        if not key_path.is_file():
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_invalid"
            )
        return self._key_for_write()

    def _path_for(self, operation_ref: str) -> Path:
        if (
            not isinstance(operation_ref, str)
            or not operation_ref
            or len(operation_ref) > 512
        ):
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_identity_invalid"
            )
        record_name = canonical_hash({"operation_ref": operation_ref}) + ".json"
        return self._records / record_name

    def _read(self, path: Path) -> RootOperationDiagnosticRecord:
        try:
            payload = read_transport_envelope(path, self._key_for_read())
        except (OSError, ProviderSupervisorError) as error:
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_invalid"
            ) from error
        record = self._validated_record(payload)
        if path != self._path_for(record.operation_ref):
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_invalid"
            )
        return record

    @staticmethod
    def _validated_record(
        value: dict[str, object],
    ) -> RootOperationDiagnosticRecord:
        operation_ref = value.get("operation_ref")
        root_kind = value.get("root_kind")
        diagnostics = value.get("diagnostics")
        diagnostics_hash = value.get("diagnostics_hash")
        if (
            set(value)
            != {
                "schema_ref",
                "operation_ref",
                "root_kind",
                "diagnostics",
                "diagnostics_hash",
            }
            or value.get("schema_ref")
            != ROOT_OPERATION_DIAGNOSTIC_RECORD_SCHEMA
            or not isinstance(operation_ref, str)
            or not operation_ref
            or len(operation_ref) > 512
            or root_kind not in ROOT_AGENT_KINDS
            or not isinstance(diagnostics, dict)
            or not isinstance(diagnostics_hash, str)
            or len(diagnostics_hash) != 64
            or canonical_hash(diagnostics) != diagnostics_hash
        ):
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_invalid"
            )
        try:
            validated = validate_root_capability_diagnostics(
                diagnostics,
                root_kind=cast(RootAgentKind, root_kind),
            )
        except ValueError as error:
            raise RootOperationDiagnosticError(
                "root_operation_diagnostic_record_invalid"
            ) from error
        return RootOperationDiagnosticRecord(
            operation_ref=operation_ref,
            root_kind=cast(RootAgentKind, root_kind),
            diagnostics=validated,
            diagnostics_hash=diagnostics_hash,
        )


__all__ = [
    "ROOT_OPERATION_DIAGNOSTIC_PAGE_SCHEMA",
    "ROOT_OPERATION_DIAGNOSTIC_RECORD_SCHEMA",
    "RootOperationDiagnosticError",
    "RootOperationDiagnosticRecord",
    "RootOperationDiagnosticRecorder",
    "RootOperationDiagnosticStore",
    "root_operation_diagnostic_ref",
]
