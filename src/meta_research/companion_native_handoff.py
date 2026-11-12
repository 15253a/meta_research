"""Keep an accepted creation Companion's native history in the active account home.

Only the exact root rollout is copied. Credentials, provider state databases,
creation work and capabilities are not transferred or re-authorized here.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.protected_creation_runtime import ProtectedCreationRuntime, _regular_reader
from meta_research.root_workspace import _directory_fd, _child_directory, _read_bytes


_NATIVE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _matching_rollouts(home: Path, native: str) -> list[Path]:
    with _directory_fd(home):
        sessions = home / "sessions"
        if not sessions.exists():
            return []
        with _directory_fd(sessions):
            result = []
            for root, directories, files in os.walk(sessions, followlinks=False):
                if any((Path(root) / name).is_symlink() for name in directories):
                    raise OwnerConflict("companion_native_handoff_unsafe")
                for name in files:
                    if name.endswith("-" + native + ".jsonl"):
                        path = Path(root) / name
                        if path.is_symlink():
                            raise OwnerConflict("companion_native_handoff_unsafe")
                        result.append(path)
            return result


def _store_new(home: Path, relative: Path, content: bytes) -> None:
    """Publish complete bytes with exclusive creation; never replace a rollout."""
    with _directory_fd(home) as directory, ExitStack() as stack:
        for part in relative.parts[:-1]:
            directory = stack.enter_context(_child_directory(directory, part, create=True))
        temporary = ".native-handoff-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, relative.name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                pass
            os.fsync(directory)
        finally:
            os.unlink(temporary, dir_fd=directory)


def _copy_rollout(source: Path, home: Path, relative: Path) -> tuple[str, int]:
    """Stream the original history; its length is not a material-reader limit."""
    with _regular_reader(source) as original, _directory_fd(home) as directory, ExitStack() as stack:
        before = os.fstat(original.fileno())
        if before.st_nlink != 1:
            raise OwnerConflict("companion_native_handoff_unsafe")
        for part in relative.parts[:-1]:
            directory = stack.enter_context(_child_directory(directory, part, create=True))
        temporary = ".native-handoff-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while chunk := original.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            after = os.fstat(original.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OwnerConflict("companion_native_handoff_changed")
            try:
                os.link(temporary, relative.name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=directory)
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as existing:
                details = os.fstat(existing.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise OwnerConflict("companion_native_handoff_conflict")
                original.seek(0)
                while chunk := original.read(1024 * 1024):
                    if existing.read(len(chunk)) != chunk:
                        raise OwnerConflict("companion_native_handoff_conflict")
            os.fsync(directory)
            return digest.hexdigest(), size
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _existing_custody(home: Path, receipt: Path, identity: dict[str, str]) -> bool:
    path = home / receipt
    if not path.exists() and not path.is_symlink():
        return False
    with _regular_reader(path) as stream:
        details = os.fstat(stream.fileno())
        if details.st_nlink != 1 or details.st_mode & 0o077 or details.st_uid != os.geteuid():
            raise OwnerConflict("companion_native_handoff_unsafe")
        record = json.load(stream)
    if any(record.get(key) != value for key, value in identity.items()):
        raise OwnerConflict("companion_native_handoff_conflict")
    relative = Path(record["destination_relative_path"])
    if (relative.is_absolute() or len(relative.parts) != 5 or relative.parts[0] != "sessions"
            or any(part in {".", ".."} for part in relative.parts)
            or not relative.name.endswith("-" + identity["native_session_ref"] + ".jsonl")
            or type(record.get("source_bytes")) is not int or record["source_bytes"] < 1
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("source_sha256", ""))):
        raise OwnerConflict("companion_native_handoff_conflict")
    # After custody, the active native provider owns this continuing history.
    # Do not rescan all sessions or rehash its inherited context on every turn.
    with _regular_reader(home / relative) as stream:
        details = os.fstat(stream.fileno())
        metadata = json.loads(stream.readline())
        if (details.st_nlink != 1 or details.st_size < record["source_bytes"]
                or metadata.get("type") != "session_meta"
                or metadata.get("payload", {}).get("id") != identity["native_session_ref"]):
            raise OwnerConflict("companion_native_handoff_conflict")
    return True


def ensure_companion_native_handoff(adapter, binding, native: str) -> None:
    roots = adapter._workspaces
    if roots is None or roots._creation_runtime_configuration is None:
        return
    location = binding.location
    if location.root_kind != "companion" or location.quest_ref is None:
        return
    human = roots._hc
    facts = human.query_companion_work_context(location.root_session_ref)
    if (facts is None or facts["quest_ref"] != location.quest_ref
            or facts["scope_ref"] != "quest:" + location.quest_ref or facts["status"] != "open"):
        raise OwnerConflict("companion_native_handoff_scope_invalid")
    expected = roots.bind_companion_session(facts["scope_ref"], location.root_session_ref).location
    if (expected.workspace_ref, expected.directory) != (location.workspace_ref, location.directory):
        raise OwnerConflict("companion_native_handoff_scope_invalid")
    initialization = human.query_quest_creation(facts["initialization_id"])
    if initialization["status"] != "completed" or initialization["quest_ref"] != location.quest_ref:
        raise OwnerConflict("companion_native_handoff_scope_invalid")
    with human._database.read() as connection:
        preparation = connection.execute(text("SELECT session_ref,native_session_ref FROM hc_intent_drafting_sessions "
            "WHERE initialization_id=:initialization"), {"initialization": facts["initialization_id"]}).one()
        if preparation.session_ref != facts["preparation_root_session_ref"]:
            raise OwnerConflict("companion_native_handoff_scope_invalid")
        # New native generations have their own ordinary history; only the
        # explicitly inherited creation identity needs this custody boundary.
        if native != preparation.native_session_ref:
            return
        operations = connection.execute(text("SELECT workspace_ref,anchor_json,state FROM hc_creation_material_operations "
            "WHERE root_session_ref=:root"), {"root": preparation.session_ref}).all()
    if not operations:
        return  # A creation that never used a protected runtime already used this home.
    prepared = roots._initialization_location(facts["initialization_id"], preparation.session_ref)
    if (_NATIVE.fullmatch(native) is None or any(row.workspace_ref != prepared.workspace_ref
            or json.loads(row.anchor_json).get("kind") != "quest_initialization"
            or json.loads(row.anchor_json).get("ref") != facts["initialization_id"]
            for row in operations)):
        raise OwnerConflict("companion_native_handoff_scope_invalid")
    if any(row.state not in {"sealed", "failed"} for row in operations):
        raise OwnerConflict("companion_native_handoff_unsealed")
    executable, active_home = roots._creation_runtime_configuration
    runner_home = getattr(adapter._runner, "_protected_environment", {}).get("CODEX_HOME")
    if runner_home is None or Path(runner_home).absolute() != active_home:
        raise OwnerConflict("companion_native_handoff_home_invalid")
    custody_identity = {"schema_ref": "meta-research/companion-native-handoff/v1",
        "quest_ref": location.quest_ref, "initialization_id": facts["initialization_id"],
        "preparation_root_session_ref": preparation.session_ref,
        "companion_root_session_ref": location.root_session_ref, "native_session_ref": native}
    receipt = Path("native-handoffs") / (canonical_hash({"native_session_ref": native}) + ".json")
    if _existing_custody(active_home, receipt, custody_identity):
        return
    identity = canonical_hash({"workspace_ref": prepared.workspace_ref, "root_session_ref": preparation.session_ref})
    runtime = ProtectedCreationRuntime(roots._bases["companion"] / ".protected" / identity, executable, active_home)
    with runtime.exclusive_sealed_work():
        source_home = runtime.jail_root / "home/creation/.codex"
        sources = _matching_rollouts(source_home, native)
        if len(sources) != 1:
            raise OwnerConflict("companion_native_handoff_missing")
        source = sources[0]
        relative = source.relative_to(source_home)
        with _regular_reader(source) as stream:
            metadata = json.loads(stream.readline())
        if metadata.get("type") != "session_meta" or metadata.get("payload", {}).get("id") != native:
            raise OwnerConflict("companion_native_handoff_identity_invalid")
        destinations = _matching_rollouts(active_home, native)
        if destinations and destinations != [active_home / relative]:
            raise OwnerConflict("companion_native_handoff_conflict")
        source_hash, source_bytes = _copy_rollout(source, active_home, relative)
        provenance = canonical_json({**custody_identity,
            "source_path": str(source), "source_sha256": source_hash,
            "source_bytes": source_bytes, "destination_relative_path": relative.as_posix()}).encode()
        _store_new(active_home, receipt, provenance)
        if _read_bytes(active_home, receipt.as_posix()) != provenance:
            raise OwnerConflict("companion_native_handoff_conflict")
