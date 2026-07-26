"""Durable Bundle target graph and exact admission seam.

Callers hand this module the frozen Plan target declarations plus the already
claimed ``build_target`` ids.  The module owns graph validation, atomic durable
registration, readiness derivation, publication-backed admission, and compact
graph projections.  ``seq`` is used only for deterministic ordering.

Publication bytes live outside SQLite.  Their re-verification is therefore an
injected adapter at this seam; all DB identity and legal-state checks remain
inside :class:`BundleGraph`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)


_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMAL_PUBLICATION_SCHEMA = "meta-research-pool-db-binding/v1"


class BundleGraphError(RuntimeError):
    """Base error for the durable Bundle graph seam."""


class GraphValidationError(BundleGraphError):
    """A Plan graph or its resolved database identities are invalid."""


class AdmissionError(BundleGraphError):
    """A target does not possess the exact facts required for admission."""


class AdmissionConflict(AdmissionError):
    """An admission replay disagrees with an already durable admission."""


@dataclass(frozen=True)
class VerifiedPublication:
    """Filesystem-verified identity returned by a publication adapter."""

    manifest_ref: str
    manifest_hash: str
    baseline_id: int
    variant_id: int
    evaluation_id: int
    attempt_id: int
    source_ref: Optional[str] = None
    source_hash: Optional[str] = None
    source_hash_alg: Optional[str] = None


class PublicationVerifier(Protocol):
    """Adapter seam for re-reading a content-addressed publication."""

    def verify(
            self, manifest_ref: str, expected_hash: str) -> VerifiedPublication:
        ...


@dataclass(frozen=True)
class ReadyTarget:
    target_id: int
    target_key: str
    seq: int
    gpu_count: int


@dataclass(frozen=True)
class TargetOverview:
    target_id: int
    target_key: str
    seq: int
    status: str
    admitted: bool
    depends_on: Tuple[str, ...]
    blocked_by: Tuple[str, ...]
    gpu_count: int


@dataclass(frozen=True)
class BundleOverview:
    cycle_id: int
    ready: Tuple[str, ...]
    targets: Tuple[TargetOverview, ...]


@dataclass(frozen=True)
class TargetRef:
    target_id: int
    target_key: str
    seq: int


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: int
    target_id: int
    cycle_id: int
    manifest_ref: str
    manifest_hash: str


@dataclass(frozen=True)
class _Registration:
    target_id: int
    target_key: str
    dependencies: Tuple[str, ...]
    parent_target_key: Optional[str]
    parent_baseline_ref: Optional[str]
    source_inputs: Tuple[Tuple[str, str], ...]
    gpu_count: int


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Nest safely in a caller transaction while keeping one operation atomic."""
    name = "bundle_graph_operation"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def _positive_id(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GraphValidationError(f"{label} must be a positive integer")
    return value


def _safe_key(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_KEY_RE.fullmatch(value) is None:
        raise GraphValidationError(
            f"{label} must match {_SAFE_KEY_RE.pattern}")
    return value


def _string_sequence(value: Any, *, label: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GraphValidationError(f"{label} must be an array")
    result = tuple(_safe_key(item, label=f"{label}[]") for item in value)
    if len(set(result)) != len(result):
        raise GraphValidationError(f"{label} contains duplicate target keys")
    return result


class BundleGraph:
    """Deep module for target DAG registration, readiness, and admission."""

    def __init__(
            self,
            conn: sqlite3.Connection,
            *,
            publication_verifier: PublicationVerifier,
            connection_guard: Optional[
                Callable[[], ContextManager[None]]] = None) -> None:
        if not callable(getattr(conn, "execute", None)):
            raise TypeError("conn must provide SQLite execute()")
        if not callable(getattr(publication_verifier, "verify", None)):
            raise TypeError("publication_verifier must provide verify()")
        if connection_guard is not None and not callable(connection_guard):
            raise TypeError("connection_guard must be callable")
        self._conn = conn
        self._publication_verifier = publication_verifier
        self._connection_guard = connection_guard or nullcontext

    def validate_plan_declaration(
            self,
            plan_targets: Sequence[Mapping[str, Any]]) -> None:
        """Validate the complete DAG before any domain identity is claimed.

        Registration additionally proves the resolved build-target ownership.
        This pure preflight exists so an invalid edge/cycle cannot leave
        baseline or variant claims behind before those ids are available.
        """
        if isinstance(plan_targets, (str, bytes)) or not isinstance(
                plan_targets, Sequence):
            raise GraphValidationError("plan_targets must be an array")
        target_ids: Dict[str, int] = {}
        for index, target in enumerate(plan_targets, start=1):
            if not isinstance(target, Mapping):
                raise GraphValidationError(
                    f"plan_targets[{index - 1}] must be an object")
            key = _safe_key(
                target.get("target_key"),
                label=f"plan_targets[{index - 1}].target_key")
            if key in target_ids:
                raise GraphValidationError(f"duplicate target_key: {key}")
            target_ids[key] = index
        registrations = self._parse_registrations(
            plan_targets=plan_targets, target_ids=target_ids)
        self._validate_acyclic(registrations)

    def register_plan(
            self,
            *,
            cycle_id: int,
            plan_targets: Sequence[Mapping[str, Any]],
            target_ids: Mapping[str, int]) -> None:
        """Validate and atomically persist one cycle's complete target graph.

        ``target_ids`` is the trusted result of claiming/inserting the Plan's
        build targets.  Replaying an identical registration is a no-op; any
        difference fails closed.
        """
        cycle = _positive_id(cycle_id, label="cycle_id")
        registrations = self._parse_registrations(
            plan_targets=plan_targets, target_ids=target_ids)
        self._validate_resolved_targets(cycle, registrations)
        self._validate_acyclic(registrations)

        with self._connection_guard():
            with _atomic(self._conn):
                existing = self._load_registration(cycle)
                if existing:
                    if existing != registrations:
                        raise GraphValidationError(
                            f"cycle {cycle} graph registration conflicts with "
                            "durable graph")
                    return

                by_key = {item.target_key: item for item in registrations}
                for item in registrations:
                    parent_id = (
                        by_key[item.parent_target_key].target_id
                        if item.parent_target_key is not None else None)
                    self._conn.execute(
                        "INSERT INTO bundle_target_node("
                        "target_id,cycle_id,target_key,parent_target_id,"
                        "parent_baseline_ref) VALUES (?,?,?,?,?)",
                        (
                            item.target_id,
                            cycle,
                            item.target_key,
                            parent_id,
                            item.parent_baseline_ref,
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO bundle_resource_request("
                        "build_target_id,cycle_id,gpu_count,worker_slots"
                        ") VALUES (?,?,?,1)",
                        (item.target_id, cycle, item.gpu_count),
                    )
                for item in registrations:
                    for dependency_key in item.dependencies:
                        self._conn.execute(
                            "INSERT INTO bundle_target_dependency("
                            "cycle_id,upstream_target_id,downstream_target_id"
                            ") VALUES (?,?,?)",
                            (
                                cycle,
                                by_key[dependency_key].target_id,
                                item.target_id,
                            ),
                        )
                    for input_key, upstream_key in item.source_inputs:
                        self._conn.execute(
                            "INSERT INTO bundle_source_request("
                            "cycle_id,downstream_target_id,input_key,"
                            "upstream_target_id) VALUES (?,?,?,?)",
                            (
                                cycle,
                                item.target_id,
                                input_key,
                                by_key[upstream_key].target_id,
                            ),
                        )

    def ready_frontier(self, cycle_id: int) -> Tuple[ReadyTarget, ...]:
        """Return all pending nodes whose every dependency is admitted."""
        overview = self.overview(cycle_id)
        return tuple(
            ReadyTarget(
                target_id=target.target_id,
                target_key=target.target_key,
                seq=target.seq,
                gpu_count=target.gpu_count,
            )
            for target in overview.targets
            if target.status == "pending" and not target.blocked_by
        )

    def runnable_frontier(self, cycle_id: int) -> Tuple[ReadyTarget, ...]:
        """Return fresh or interrupted work whose dependencies are admitted.

        A provider/owner interruption does not roll a durable target back to
        ``pending``.  The same Worker task must resume ``building``, ``smoke``
        or ``running`` without creating replacement domain identities.
        """
        overview = self.overview(cycle_id)
        return tuple(
            ReadyTarget(
                target_id=target.target_id,
                target_key=target.target_key,
                seq=target.seq,
                gpu_count=target.gpu_count,
            )
            for target in overview.targets
            if (
                target.status in {
                    "pending", "building", "smoke", "running"}
                and not target.blocked_by
            )
        )

    def descendants(self, target_id: int) -> Tuple[TargetRef, ...]:
        """Return every transitive dependent once, ordered by ``seq`` then id."""
        target = _positive_id(target_id, label="target_id")
        row = self._conn.execute(
            "SELECT cycle_id FROM bundle_target_node WHERE target_id=?",
            (target,),
        ).fetchone()
        if row is None:
            raise GraphValidationError(
                f"build_target {target} is not registered in a Bundle graph")
        cycle = int(row[0])
        rows = self._conn.execute(
            "WITH RECURSIVE descendants(target_id) AS ("
            "  SELECT downstream_target_id "
            "  FROM bundle_target_dependency "
            "  WHERE cycle_id=? AND upstream_target_id=? "
            "  UNION "
            "  SELECT d.downstream_target_id "
            "  FROM bundle_target_dependency d "
            "  JOIN descendants seen ON seen.target_id=d.upstream_target_id "
            "  WHERE d.cycle_id=?"
            ") "
            "SELECT n.target_id,n.target_key,bt.seq "
            "FROM descendants seen "
            "JOIN bundle_target_node n "
            "ON n.target_id=seen.target_id AND n.cycle_id=? "
            "JOIN build_target bt ON bt.id=n.target_id "
            "ORDER BY bt.seq,n.target_id",
            (cycle, target, cycle, cycle),
        ).fetchall()
        return tuple(
            TargetRef(
                target_id=int(item[0]),
                target_key=str(item[1]),
                seq=int(item[2]),
            )
            for item in rows
        )

    def admit(
            self,
            *,
            target_id: int,
            publication_decision_id: int) -> AdmissionRecord:
        """Reverify and durably admit one exact completed target.

        Admission requires all of the following at once:

        * the registered target is ``complete``;
        * its exact Bundle phase commit exists;
        * the named append-only gate decision is a complete formal publication;
        * publication identities close to this target's legal DB objects and
          successful exact evaluation attempt;
        * the injected adapter re-reads the named manifest and returns the same
          immutable identities.

        Exact replay returns the original record.  Any differing replay fails
        closed and never overwrites the durable fact.
        """
        try:
            target = _positive_id(target_id, label="target_id")
            decision_id = _positive_id(
                publication_decision_id, label="publication_decision_id")
        except GraphValidationError as error:
            raise AdmissionError(str(error)) from error

        target_row = self._conn.execute(
            "SELECT n.cycle_id,bt.status,bt.baseline_id,bt.variant_id,"
            "bt.evaluation_id "
            "FROM bundle_target_node n "
            "JOIN build_target bt "
            "ON bt.id=n.target_id AND bt.cycle_id=n.cycle_id "
            "WHERE n.target_id=?",
            (target,),
        ).fetchone()
        if target_row is None:
            raise AdmissionError(
                f"build_target {target} is not registered in a Bundle graph")
        cycle = int(target_row[0])
        if target_row[1] != "complete":
            raise AdmissionError(
                f"build_target {target} status is not complete")
        if any(value is None for value in target_row[2:5]):
            raise AdmissionError(
                f"build_target {target} lacks exact domain identities")
        baseline_id, variant_id, evaluation_id = (
            int(target_row[2]), int(target_row[3]), int(target_row[4]))

        phase_rows = self._conn.execute(
            "SELECT id FROM phase_commit "
            "WHERE cycle_id=? AND stage='bundle' AND target_id=?",
            (cycle, target),
        ).fetchall()
        if len(phase_rows) != 1:
            raise AdmissionError(
                f"build_target {target} lacks one exact Bundle phase commit")
        phase_commit_id = int(phase_rows[0][0])

        decision_row = self._conn.execute(
            "SELECT cycle_id,actor,type,payload_json FROM decision WHERE id=?",
            (decision_id,),
        ).fetchone()
        if (
            decision_row is None
            or decision_row[0] != cycle
            or decision_row[1] != "gate"
            or decision_row[2] != "pool_publication"
        ):
            raise AdmissionError(
                f"decision {decision_id} is not this target's formal publication")
        try:
            event = json.loads(decision_row[3])
        except (TypeError, json.JSONDecodeError) as error:
            raise AdmissionError("formal publication decision JSON is invalid") from error
        if not isinstance(event, dict):
            raise AdmissionError("formal publication decision is not an object")
        if event.get("schema") != _FORMAL_PUBLICATION_SCHEMA:
            raise AdmissionError("formal publication decision schema is invalid")
        manifest_ref = event.get("manifest_ref")
        manifest_hash = event.get("manifest_hash")
        if (
            not isinstance(manifest_ref, str)
            or not 1 <= len(manifest_ref) <= 4096
            or not isinstance(manifest_hash, str)
            or _HASH_RE.fullmatch(manifest_hash) is None
        ):
            raise AdmissionError(
                "formal publication manifest ref/hash is invalid")
        event_identity = (
            event.get("baseline_id"),
            event.get("variant_id"),
            event.get("evaluation_id"),
            event.get("attempt_id"),
        )
        if event_identity[:3] != (baseline_id, variant_id, evaluation_id):
            raise AdmissionError(
                "formal publication identity does not match exact target")
        attempt_id = event_identity[3]
        if isinstance(attempt_id, bool) or not isinstance(attempt_id, int):
            raise AdmissionError(
                "formal publication attempt identity is invalid")

        self._validate_legal_domain(
            target_id=target,
            cycle_id=cycle,
            baseline_id=baseline_id,
            variant_id=variant_id,
            evaluation_id=evaluation_id,
            attempt_id=attempt_id,
        )
        try:
            publication = self._publication_verifier.verify(
                manifest_ref, manifest_hash)
        except Exception as error:
            raise AdmissionError(
                f"publication verification failed for build_target {target}") from error
        if not isinstance(publication, VerifiedPublication):
            raise AdmissionError(
                "publication verifier returned an invalid proof type")
        verified_identity = (
            publication.baseline_id,
            publication.variant_id,
            publication.evaluation_id,
            publication.attempt_id,
        )
        if (
            publication.manifest_ref != manifest_ref
            or publication.manifest_hash != manifest_hash
            or verified_identity
            != (baseline_id, variant_id, evaluation_id, attempt_id)
        ):
            raise AdmissionError(
                "verified publication conflicts with exact formal publication")
        self._validate_verified_source(publication)

        values = (
            target,
            cycle,
            phase_commit_id,
            decision_id,
            manifest_ref,
            manifest_hash,
            baseline_id,
            variant_id,
            evaluation_id,
            attempt_id,
            publication.source_ref,
            publication.source_hash,
            publication.source_hash_alg,
        )
        with self._connection_guard():
            with _atomic(self._conn):
                # Re-read mutable lifecycle facts inside the write operation.
                # The DB trigger independently repeats these exact checks.
                self._validate_legal_domain(
                    target_id=target,
                    cycle_id=cycle,
                    baseline_id=baseline_id,
                    variant_id=variant_id,
                    evaluation_id=evaluation_id,
                    attempt_id=attempt_id,
                )
                existing = self._conn.execute(
                    "SELECT id,target_id,cycle_id,phase_commit_id,"
                    "publication_decision_id,manifest_ref,manifest_hash,"
                    "baseline_id,variant_id,evaluation_id,attempt_id,"
                    "source_ref,source_hash,source_hash_alg "
                    "FROM bundle_target_admission WHERE target_id=?",
                    (target,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing[1:]) != values:
                        raise AdmissionConflict(
                            f"build_target {target} admission replay conflicts "
                            "with durable admission")
                    return AdmissionRecord(
                        admission_id=int(existing[0]),
                        target_id=target,
                        cycle_id=cycle,
                        manifest_ref=manifest_ref,
                        manifest_hash=manifest_hash,
                    )
                try:
                    cursor = self._conn.execute(
                        "INSERT INTO bundle_target_admission("
                        "target_id,cycle_id,phase_commit_id,"
                        "publication_decision_id,manifest_ref,manifest_hash,"
                        "baseline_id,variant_id,evaluation_id,attempt_id,"
                        "source_ref,source_hash,source_hash_alg"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                except sqlite3.IntegrityError as error:
                    raise AdmissionError(
                        f"build_target {target} admission facts conflict") from error
                admission_id = int(cursor.lastrowid)
        return AdmissionRecord(
            admission_id=admission_id,
            target_id=target,
            cycle_id=cycle,
            manifest_ref=manifest_ref,
            manifest_hash=manifest_hash,
        )

    def overview(self, cycle_id: int) -> BundleOverview:
        """Return a bounded deterministic graph/status projection (no raw logs)."""
        cycle = _positive_id(cycle_id, label="cycle_id")
        rows = self._conn.execute(
            "SELECT n.target_id,n.target_key,bt.seq,bt.status,"
            "CASE WHEN a.id IS NULL THEN 0 ELSE 1 END,r.gpu_count "
            "FROM bundle_target_node n "
            "JOIN build_target bt ON bt.id=n.target_id AND bt.cycle_id=n.cycle_id "
            "JOIN bundle_resource_request r "
            "ON r.build_target_id=n.target_id AND r.cycle_id=n.cycle_id "
            "LEFT JOIN bundle_target_admission a ON a.target_id=n.target_id "
            "WHERE n.cycle_id=? ORDER BY bt.seq,n.target_id",
            (cycle,),
        ).fetchall()
        key_by_id = {int(row[0]): str(row[1]) for row in rows}
        dependencies: Dict[int, list[int]] = {
            target_id: [] for target_id in key_by_id}
        blocked: Dict[int, list[int]] = {
            target_id: [] for target_id in key_by_id}
        dep_rows = self._conn.execute(
            "SELECT d.downstream_target_id,d.upstream_target_id,"
            "CASE WHEN a.id IS NULL THEN 0 ELSE 1 END "
            "FROM bundle_target_dependency d "
            "LEFT JOIN bundle_target_admission a "
            "ON a.target_id=d.upstream_target_id "
            "WHERE d.cycle_id=? "
            "ORDER BY d.downstream_target_id,d.upstream_target_id",
            (cycle,),
        ).fetchall()
        for downstream, upstream, admitted in dep_rows:
            downstream_id, upstream_id = int(downstream), int(upstream)
            if downstream_id not in dependencies or upstream_id not in key_by_id:
                raise GraphValidationError(
                    f"cycle {cycle} durable dependency references an unknown node")
            dependencies[downstream_id].append(upstream_id)
            if not admitted:
                blocked[downstream_id].append(upstream_id)

        targets = tuple(
            TargetOverview(
                target_id=int(row[0]),
                target_key=str(row[1]),
                seq=int(row[2]),
                status=str(row[3]),
                admitted=bool(row[4]),
                depends_on=tuple(
                    key_by_id[target_id]
                    for target_id in dependencies[int(row[0])]),
                blocked_by=tuple(
                    key_by_id[target_id]
                    for target_id in blocked[int(row[0])]),
                gpu_count=int(row[5]),
            )
            for row in rows
        )
        ready = tuple(
            target.target_key for target in targets
            if target.status == "pending" and not target.blocked_by)
        return BundleOverview(cycle_id=cycle, ready=ready, targets=targets)

    def _validate_legal_domain(
            self,
            *,
            target_id: int,
            cycle_id: int,
            baseline_id: int,
            variant_id: int,
            evaluation_id: int,
            attempt_id: int) -> None:
        row = self._conn.execute(
            "SELECT b.status,v.baseline_id,v.status,e.variant_id,e.status,"
            "e.build_target_id,ea.evaluation_id,ea.cycle_id,"
            "ea.build_target_id,ea.status "
            "FROM baseline b "
            "JOIN variant v ON v.id=? "
            "JOIN evaluation e ON e.id=? "
            "JOIN evaluation_attempt ea ON ea.id=? "
            "WHERE b.id=?",
            (variant_id, evaluation_id, attempt_id, baseline_id),
        ).fetchone()
        expected = (
            "legal",
            baseline_id,
            "legal",
            variant_id,
            "success",
            target_id,
            evaluation_id,
            cycle_id,
            target_id,
            "success",
        )
        if row is None or tuple(row) != expected:
            raise AdmissionError(
                "target publication domain objects are not exact and legal")

    @staticmethod
    def _validate_verified_source(publication: VerifiedPublication) -> None:
        source = (
            publication.source_ref,
            publication.source_hash,
            publication.source_hash_alg,
        )
        if source == (None, None, None):
            return
        if (
            not isinstance(source[0], str)
            or not 1 <= len(source[0]) <= 4096
            or not isinstance(source[1], str)
            or _HASH_RE.fullmatch(source[1]) is None
            or not isinstance(source[2], str)
            or not 1 <= len(source[2]) <= 64
        ):
            raise AdmissionError(
                "verified publication source ref/hash is invalid")

    def _parse_registrations(
            self,
            *,
            plan_targets: Sequence[Mapping[str, Any]],
            target_ids: Mapping[str, int]) -> Tuple[_Registration, ...]:
        if isinstance(plan_targets, (str, bytes)) or not isinstance(
                plan_targets, Sequence):
            raise GraphValidationError("plan_targets must be an array")
        if not isinstance(target_ids, Mapping):
            raise GraphValidationError("target_ids must be a mapping")

        parsed = []
        seen = set()
        for index, raw in enumerate(plan_targets):
            if not isinstance(raw, Mapping):
                raise GraphValidationError(
                    f"plan_targets[{index}] must be an object")
            key = _safe_key(
                raw.get("target_key"),
                label=f"plan_targets[{index}].target_key",
            )
            if key in seen:
                raise GraphValidationError(f"duplicate target_key: {key}")
            seen.add(key)
            dependencies = _string_sequence(
                raw.get("depends_on", ()),
                label=f"target {key}.depends_on",
            )
            if key in dependencies:
                raise GraphValidationError(f"target {key} depends on itself")

            parent_target_key = None
            parent_baseline_ref = None
            parent = raw.get("parent_baseline")
            if parent is not None:
                if not isinstance(parent, Mapping):
                    raise GraphValidationError(
                        f"target {key}.parent_baseline must be an object")
                parent_keys = set(parent)
                if parent_keys == {"target_key"}:
                    parent_target_key = _safe_key(
                        parent["target_key"],
                        label=f"target {key}.parent_baseline.target_key",
                    )
                    if parent_target_key == key:
                        raise GraphValidationError(
                            f"target {key} is its own parent baseline")
                    if parent_target_key not in dependencies:
                        raise GraphValidationError(
                            f"target {key} parent target must also be a dependency")
                elif parent_keys == {"baseline_ref"}:
                    parent_baseline_ref = parent["baseline_ref"]
                    if (
                        not isinstance(parent_baseline_ref, str)
                        or not parent_baseline_ref
                        or len(parent_baseline_ref) > 4096
                    ):
                        raise GraphValidationError(
                            f"target {key}.parent_baseline.baseline_ref is invalid")
                else:
                    raise GraphValidationError(
                        f"target {key}.parent_baseline must choose exactly "
                        "target_key or baseline_ref")

            sources = raw.get("published_source_inputs", ())
            if isinstance(sources, (str, bytes)) or not isinstance(
                    sources, Sequence):
                raise GraphValidationError(
                    f"target {key}.published_source_inputs must be an array")
            source_inputs = []
            source_keys = set()
            for source_index, source in enumerate(sources):
                if not isinstance(source, Mapping) or set(source) != {
                        "input_key", "target_key"}:
                    raise GraphValidationError(
                        f"target {key}.published_source_inputs[{source_index}] "
                        "must contain only input_key and target_key")
                input_key = _safe_key(
                    source["input_key"],
                    label=f"target {key} source input_key",
                )
                upstream_key = _safe_key(
                    source["target_key"],
                    label=f"target {key} source target_key",
                )
                if input_key in source_keys:
                    raise GraphValidationError(
                        f"target {key} has duplicate source input {input_key}")
                if upstream_key == key:
                    raise GraphValidationError(
                        f"target {key} cannot consume its own publication")
                if upstream_key not in dependencies:
                    raise GraphValidationError(
                        f"target {key} published source must also be a dependency")
                source_keys.add(input_key)
                source_inputs.append((input_key, upstream_key))

            resources = raw.get("resources")
            if resources is not None:
                if not isinstance(resources, Mapping) or set(resources) != {
                        "gpu_count"}:
                    raise GraphValidationError(
                        f"target {key}.resources must contain only gpu_count")
                gpu_count = resources["gpu_count"]
                if (
                    isinstance(gpu_count, bool)
                    or not isinstance(gpu_count, int)
                    or not 0 <= gpu_count <= 64
                ):
                    raise GraphValidationError(
                        f"target {key}.resources.gpu_count must be 0..64")
            else:
                legacy_gpu = raw.get("gpu_required", False)
                if not isinstance(legacy_gpu, bool):
                    raise GraphValidationError(
                        f"target {key}.gpu_required must be boolean")
                gpu_count = 1 if legacy_gpu else 0

            if key not in target_ids:
                raise GraphValidationError(
                    f"target_ids is missing target_key {key}")
            parsed.append(_Registration(
                target_id=_positive_id(
                    target_ids[key], label=f"target_ids[{key!r}]"),
                target_key=key,
                dependencies=dependencies,
                parent_target_key=parent_target_key,
                parent_baseline_ref=parent_baseline_ref,
                source_inputs=tuple(source_inputs),
                gpu_count=gpu_count,
            ))

        extra = set(target_ids) - seen
        if extra:
            raise GraphValidationError(
                f"target_ids contains unknown target keys: {sorted(extra)}")
        known = {item.target_key for item in parsed}
        for item in parsed:
            for dependency in item.dependencies:
                if dependency not in known:
                    raise GraphValidationError(
                        f"target {item.target_key} depends on missing target "
                        f"{dependency}")
            if (
                item.parent_target_key is not None
                and item.parent_target_key not in known
            ):
                raise GraphValidationError(
                    f"target {item.target_key} has missing parent target "
                    f"{item.parent_target_key}")
        return tuple(parsed)

    def _validate_resolved_targets(
            self, cycle_id: int,
            registrations: Tuple[_Registration, ...]) -> None:
        if self._conn.execute(
                "SELECT 1 FROM cycle WHERE id=?", (cycle_id,)).fetchone() is None:
            raise GraphValidationError(f"cycle {cycle_id} does not exist")
        ids = [item.target_id for item in registrations]
        if len(ids) != len(set(ids)):
            raise GraphValidationError(
                "different target keys resolve to the same build_target")
        by_key = {item.target_key: item for item in registrations}
        baseline_by_target: Dict[int, Optional[int]] = {}
        kind_by_target: Dict[int, str] = {}
        parent_by_baseline: Dict[int, Optional[int]] = {}
        for item in registrations:
            row = self._conn.execute(
                "SELECT cycle_id,target_kind,baseline_id FROM build_target "
                "WHERE id=?",
                (item.target_id,),
            ).fetchone()
            if row is None:
                raise GraphValidationError(
                    f"build_target {item.target_id} does not exist")
            if int(row[0]) != cycle_id:
                raise GraphValidationError(
                    f"build_target {item.target_id} belongs to cycle {row[0]}, "
                    f"not cycle {cycle_id}")
            kind_by_target[item.target_id] = str(row[1])
            baseline_id = None if row[2] is None else int(row[2])
            baseline_by_target[item.target_id] = baseline_id
            if baseline_id is not None:
                baseline = self._conn.execute(
                    "SELECT parent_id FROM baseline WHERE id=?",
                    (baseline_id,),
                ).fetchone()
                if baseline is None:
                    raise GraphValidationError(
                        f"build_target {item.target_id} baseline "
                        f"{baseline_id} does not exist")
                parent_by_baseline[baseline_id] = (
                    None if baseline[0] is None else int(baseline[0]))

        for item in registrations:
            if kind_by_target[item.target_id] != "build":
                if (
                    item.parent_target_key is not None
                    or item.parent_baseline_ref is not None
                ):
                    raise GraphValidationError(
                        f"target {item.target_key} parent_baseline is only "
                        "valid for build targets")
                continue
            baseline_id = baseline_by_target[item.target_id]
            if baseline_id is None:
                raise GraphValidationError(
                    f"build target {item.target_key} has no baseline identity")
            expected_parent = None
            if item.parent_target_key is not None:
                parent_target = by_key[item.parent_target_key]
                expected_parent = baseline_by_target[parent_target.target_id]
                if expected_parent is None:
                    raise GraphValidationError(
                        f"parent target {item.parent_target_key} has no "
                        "baseline identity")
            elif item.parent_baseline_ref is not None:
                parent = self._conn.execute(
                    "SELECT id,status FROM baseline WHERE canonical_key=?",
                    (item.parent_baseline_ref,),
                ).fetchone()
                if parent is None or str(parent[1]) != "legal":
                    raise GraphValidationError(
                        f"target {item.target_key} parent baseline_ref does "
                        "not resolve to a legal baseline")
                expected_parent = int(parent[0])
            if parent_by_baseline[baseline_id] != expected_parent:
                raise GraphValidationError(
                    f"target {item.target_key} durable baseline parent "
                    "does not match the Plan declaration")

    @staticmethod
    def _validate_acyclic(
            registrations: Tuple[_Registration, ...]) -> None:
        incoming = {
            item.target_key: set(item.dependencies) for item in registrations}
        ready = sorted(key for key, deps in incoming.items() if not deps)
        visited = []
        while ready:
            key = ready.pop(0)
            visited.append(key)
            for downstream in sorted(incoming):
                deps = incoming[downstream]
                if key in deps:
                    deps.remove(key)
                    if not deps and downstream not in visited and downstream not in ready:
                        ready.append(downstream)
                        ready.sort()
        if len(visited) != len(registrations):
            cycle_keys = sorted(key for key, deps in incoming.items() if deps)
            raise GraphValidationError(
                f"bundle target dependency cycle: {cycle_keys}")

    def _load_registration(
            self, cycle_id: int) -> Tuple[_Registration, ...]:
        rows = self._conn.execute(
            "SELECT n.target_id,n.target_key,n.parent_target_id,"
            "n.parent_baseline_ref,r.gpu_count "
            "FROM bundle_target_node n "
            "JOIN build_target bt ON bt.id=n.target_id AND bt.cycle_id=n.cycle_id "
            "JOIN bundle_resource_request r "
            "ON r.build_target_id=n.target_id AND r.cycle_id=n.cycle_id "
            "WHERE n.cycle_id=? ORDER BY bt.seq,n.target_id",
            (cycle_id,),
        ).fetchall()
        if not rows:
            return ()
        key_by_id = {int(row[0]): str(row[1]) for row in rows}
        deps: Dict[int, list[str]] = {target_id: [] for target_id in key_by_id}
        for downstream, upstream in self._conn.execute(
                "SELECT downstream_target_id,upstream_target_id "
                "FROM bundle_target_dependency WHERE cycle_id=? "
                "ORDER BY id", (cycle_id,)).fetchall():
            deps[int(downstream)].append(key_by_id[int(upstream)])
        sources: Dict[int, list[Tuple[str, str]]] = {
            target_id: [] for target_id in key_by_id}
        for downstream, input_key, upstream in self._conn.execute(
                "SELECT downstream_target_id,input_key,upstream_target_id "
                "FROM bundle_source_request WHERE cycle_id=? "
                "ORDER BY id", (cycle_id,)).fetchall():
            sources[int(downstream)].append(
                (str(input_key), key_by_id[int(upstream)]))
        return tuple(
            _Registration(
                target_id=int(row[0]),
                target_key=str(row[1]),
                dependencies=tuple(deps[int(row[0])]),
                parent_target_key=(
                    key_by_id[int(row[2])] if row[2] is not None else None),
                parent_baseline_ref=(
                    str(row[3]) if row[3] is not None else None),
                source_inputs=tuple(sources[int(row[0])]),
                gpu_count=int(row[4]),
            )
            for row in rows
        )
