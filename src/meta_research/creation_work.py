from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.protected_creation_runtime import ProtectedCreationRuntime
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS


@contextmanager
def owned_directory(path):
    descriptors = []
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
        yield descriptor
    except OSError as error:
        raise OwnerConflict("creation_work_unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class ProtectedCreation:
    def __init__(self, owner, binding, inputs, operation_ref):
        if owner._creation_runtime_configuration is None:
            raise OwnerConflict("protected_creation_runtime_unavailable")
        executable, credentials_home = owner._creation_runtime_configuration
        identity = canonical_hash({"workspace_ref": binding.location.workspace_ref,
                                   "root_session_ref": binding.location.root_session_ref})
        self.runtime = ProtectedCreationRuntime(owner._bases["companion"] / ".protected" / identity,
                                               executable, credentials_home)
        for item in inputs.references:
            original = Path(item["source"]["absolute_path"]).absolute()
            if original.is_relative_to(self.runtime.jail_root):
                raise OwnerConflict("creation_original_in_work")
        self.owner = owner
        self.operation = owner._hc.begin_creation_material_operation(inputs, binding, operation_ref)
        self.operation.work = self
        self.binding = replace(binding, location=replace(binding.location, directory=self.runtime.work_directory))
        self.work_ref = "creation_work:" + canonical_hash({"operation_ref": operation_ref})
        self.relative_directory = "operations/" + canonical_hash({"operation_ref": operation_ref})
        self._token = None
        self._terminal = False
        self._executed = False
        self.sealed_identity = None
        self.read_basis = None
        self.read_literature = None
        self.execution_binding = {"operation_ref": operation_ref, "fence_ref": self.operation.fence_ref,
            "binding_hash": self.operation.binding_hash, "work_ref": self.work_ref,
            "work_directory": "/workspace/" + self.relative_directory,
            "runtime_contract": "linux-x86_64-chroot-v1"}

    @property
    def work_directory(self):
        return self.runtime.work_directory / self.relative_directory

    def channel(self, extra_operations=()):
        from meta_research.root_resident_mcp import RootResidentMcpAccess
        if self._terminal or self.owner._creation_gateway is None or self.owner._creation_endpoint is None:
            raise OwnerConflict("creation_material_channel_unavailable")
        operations = (*CREATION_MATERIAL_OPERATION_IDS, *extra_operations)
        connection, _ = self.owner._creation_gateway.issue_channel(
            run_ref=self.operation.operation_ref, attempt_ref=self.operation.operation_ref,
            root_session_ref=self.operation.binding.location.root_session_ref,
            fence_ref=self.operation.fence_ref, capability_binding_hash=self.operation.binding_hash,
            operation_ids=operations, root_kind="companion", phase="creation_materials")
        self._revoke()
        self._token = connection.token
        self.owner._creation_channels[hashlib.sha256(connection.token.encode()).hexdigest()] = self.operation
        return RootResidentMcpAccess(self.owner._creation_endpoint + "/mcp", connection.token,
                                    self.operation.binding_hash, operations, self.binding)

    def bind_context_readers(self, basis, literature):
        memory = self.owner._hc._research_memory.creation_bases
        exact = memory.query(basis["basis_ref"], basis["basis_hash"])
        memory.require_current(exact)
        identity = exact.get("input_identity")
        if (identity is None or identity["anchor"] != self.operation.inputs.anchor.as_dict()
            or identity["material_set_hash"] != self.operation.inputs.set_hash):
            raise OwnerConflict("creation_basis_unbound")
        self.read_basis = memory.reference(exact)
        self.read_literature = None if literature is None else dict(literature["source_snapshot"])
        if self.read_literature is not None:
            metadata = memory._owner.read_literature_snapshot_metadata(self.read_literature["snapshot_ref"])
            anchor = self.operation.inputs.anchor
            if (metadata["snapshot_hash"] != self.read_literature["snapshot_hash"]
                or metadata.get("creation_context_kind", "quest_initialization") != anchor.kind
                or (metadata.get("creation_context_ref") or metadata["initialization_id"]) != anchor.ref
                or metadata.get("context_generation") != anchor.generation):
                raise OwnerConflict("creation_literature_unbound")
        self.execution_binding["context_readers"] = {"basis": self.read_basis, "literature": self.read_literature}

    def completed_handoff(self):
        outcome = self.runtime.request_stop()
        if not outcome["descendants_ended"] or outcome["status"] not in {"completed", "stopped"}:
            raise OwnerConflict("protected_creation_unknown_outcome")
        self._executed = True

    def run(self, call):
        if self._terminal:
            raise OwnerConflict("creation_work_closed")
        completed = self.runtime.run(call)
        self._executed = True
        return completed

    def request_stop(self):
        return self.runtime.request_stop()

    @contextmanager
    def new_copy(self, effect_key, suffix):
        with owned_directory(self.runtime.work_directory) as root:
            parent = root
            opened = []
            try:
                for part in self.relative_directory.split("/"):
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                        os.chown(part, 65534, 65534, dir_fd=parent, follow_symlinks=False)
                    except FileExistsError:
                        pass
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    opened.append(parent)
                name = canonical_hash({"effect_key": effect_key}) + suffix
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        yield stream, name
                        stream.flush()
                        os.fsync(stream.fileno())
                        details = os.fstat(stream.fileno())
                        if details.st_nlink != 1:
                            raise OwnerConflict("creation_work_unsafe")
                        os.fchown(stream.fileno(), 65534, 65534)
                except BaseException:
                    os.unlink(name, dir_fd=parent)
                    raise
            except OSError as error:
                raise OwnerConflict("creation_work_unsafe") from error
            finally:
                for descriptor in reversed(opened):
                    os.close(descriptor)

    @contextmanager
    def read_file(self, path):
        from meta_research.server_materials import relative_parts
        parts = relative_parts(path)
        if not parts:
            raise OwnerConflict("creation_work_path_invalid")
        with owned_directory(self.work_directory) as root:
            opened = []
            try:
                parent = root
                for part in parts[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    opened.append(parent)
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
                opened.append(descriptor)
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise OwnerConflict("creation_work_unsafe")
                yield descriptor, details
            except OSError as error:
                raise OwnerConflict("creation_work_unsafe") from error
            finally:
                for descriptor in reversed(opened):
                    os.close(descriptor)

    def copy_receipt_available(self, receipt):
        with self.read_file(receipt["work_file"]["path"]) as (_, details):
            if (details.st_dev, details.st_ino) != (receipt["device"], receipt["inode"]):
                raise OwnerConflict("creation_work_changed")

    def retain_source(self, source):
        from sqlalchemy import text
        from meta_research.creation_inputs import require_identity
        from meta_research.server_materials import observation

        if self.sealed_identity is None or not self._terminal:
            raise OwnerConflict("creation_work_not_sealed")
        require_identity(self.owner._hc, self.sealed_identity)
        key = "creation_custody:" + canonical_hash({"operation": self.operation.operation_ref,
            "identity": self.sealed_identity.digest, "source": source})
        database = self.owner._hc._database
        with database.read() as connection:
            row = connection.execute(text("SELECT receipt_json FROM root_creation_custody WHERE custody_ref=:ref"),
                                     {"ref": key}).first()
        if row is not None:
            receipt = json.loads(row.receipt_json)
            with self.owner.server_files._open(receipt["locator"]) as (descriptor, details):
                if details["kind"] != "file" or int(details["size"]) != receipt["bytes"]:
                    raise OwnerConflict("creation_custody_changed")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != receipt["sha256"]:
                    raise OwnerConflict("creation_custody_changed")
            return receipt

        @contextmanager
        def selected_file():
            if source["kind"] == "work_file":
                if source["work_ref"] != self.work_ref:
                    raise OwnerConflict("creation_work_unbound")
                if source["trial_ref"] is not None:
                    with database.read() as connection:
                        trial = connection.execute(text("SELECT receipt_json FROM hc_creation_material_copies "
                            "WHERE effect_key=:ref AND operation_ref=:operation AND fence_ref=:fence AND state='ready'"),
                            {"ref": source["trial_ref"], "operation": self.operation.operation_ref,
                             "fence": self.operation.fence_ref}).first()
                    if trial is None or json.loads(trial.receipt_json)["work_file"] != source:
                        raise OwnerConflict("creation_trial_unbound")
                with self.read_file(source["path"]) as (descriptor, details):
                    yield descriptor, observation(details), None
            else:
                reference = next((item for item in self.operation.inputs.references
                                  if item["reference_ref"] == source["reference_ref"]), None)
                if reference is None:
                    raise OwnerConflict("material_not_visible")
                with self.owner.server_files._open_reference(reference["source"], source["path"]) as (descriptor, details):
                    if details["kind"] != "file" or details["observation_ref"] != source["observation_ref"]:
                        raise OwnerConflict("material_source_changed")
                    locator = reference["source"]["absolute_path"].rstrip("/")
                    if source["path"]:
                        locator += "/" + source["path"]
                    yield descriptor, details, locator

        destination = self.owner._bases["companion"] / ".creation-custody"
        self.owner._ensure_directory(destination)
        name = key.split(":", 1)[1]
        with selected_file() as (descriptor, before, original_locator), owned_directory(destination) as directory:
            digest = hashlib.sha256()
            count = 0
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o400, dir_fd=directory)
            try:
                with os.fdopen(output, "wb") as stream:
                    while True:
                        chunk = os.pread(descriptor, 1024 * 1024, count)
                        if not chunk:
                            break
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if os.fstat(stream.fileno()).st_nlink != 1:
                        raise OwnerConflict("creation_work_unsafe")
                if observation(os.fstat(descriptor)) != before or count != int(before["size"]):
                    raise OwnerConflict("creation_source_changed")
                receipt = {"custody_ref": key, "source": source, "locator": str(destination / name),
                    "original_locator": original_locator, "sha256": digest.hexdigest(), "bytes": count}
                require_identity(self.owner._hc, self.sealed_identity)
                with database.fenced_write() as connection:
                    connection.execute(text("INSERT INTO root_creation_custody(custody_ref,operation_ref,source_json,receipt_json) "
                        "VALUES(:ref,:operation,:source,:receipt)"), {"ref": key, "operation": self.operation.operation_ref,
                        "source": canonical_json(source), "receipt": canonical_json(receipt)})
                return receipt
            except BaseException:
                os.unlink(name, dir_fd=directory)
                raise

    def seal(self):
        try:
            if not self._executed:
                raise OwnerConflict("protected_creation_unknown_outcome")
            outcome = self.runtime.request_stop()
            if not outcome["descendants_ended"] or outcome["status"] not in {"completed", "stopped"}:
                raise OwnerConflict("protected_creation_unknown_outcome")
            identity = self.operation.seal()
            self.sealed_identity = identity
            self._terminal = True
            return identity
        finally:
            self._revoke()

    def fail(self, *, unknown_outcome=False):
        self.operation.fail(unknown_outcome=unknown_outcome)
        self._terminal = True
        self._revoke()

    def _revoke(self):
        if self._token is not None:
            self.owner._creation_channels.pop(hashlib.sha256(self._token.encode()).hexdigest(), None)
            self.owner._creation_gateway.revoke_channel(self._token)
            self._token = None


class ProtectedCreationRunner:
    runtime_conditions = (
        "This actual creation runtime is Linux x86_64 in a private filesystem. "
        "Native parent and delegated children, shell, HOME, caches and outputs share that boundary. "
        "External originals are unreadable as host paths; use the granted bounded material discover/read/copy tools. "
        "Only explicit /bin/node and Python3.12 standard-library light programs are supplied. "
        "Tk GUI, third-party Python scientific packages and implicit Node process.execPath self-spawn are unsupported. "
        "An unsupported command fails here; there is no host execution fallback. "
        "Keep edits and output in this operation's declared work directory."
    )

    def __init__(self, work, *, read_only_inputs=(), environment=None):
        self.work = work
        self.read_only_inputs = tuple(read_only_inputs)
        self.environment = dict(environment or {})
        self._stop_requested = False

    def run_job(self, job_ref, argv, prompt, timeout_seconds, environment=None):
        from meta_research.idea_skill import IdeaSkillUnavailable
        from meta_research.protected_creation_runtime import NativeCreationCall, ProtectedCreationError
        if job_ref != self.work.operation.operation_ref:
            raise IdeaSkillUnavailable("creation_operation_identity_invalid")
        slots = {}
        for option in ("--output-schema", "--output-last-message"):
            if argv.count(option) != 1:
                raise IdeaSkillUnavailable("creation_native_handoff_invalid")
            index = argv.index(option)
            if index + 1 == len(argv):
                raise IdeaSkillUnavailable("creation_native_handoff_invalid")
            slots[option] = Path(argv[index + 1])
        try:
            completed = self.work.run(NativeCreationCall(tuple(argv), prompt, timeout_seconds,
                {**self.environment, **(environment or {})},
                (*self.read_only_inputs, slots["--output-schema"]), (slots["--output-last-message"],)))
            if self._stop_requested and completed.returncode != 0:
                raise IdeaSkillUnavailable("codex_cli_stopped")
            return completed
        except ProtectedCreationError as error:
            raise IdeaSkillUnavailable(error.code) from error

    def cancel_job(self, job_ref):
        if job_ref != self.work.operation.operation_ref:
            return {"status": "not_running", "descendants_ended": False}
        self._stop_requested = True
        return self.work.request_stop()
