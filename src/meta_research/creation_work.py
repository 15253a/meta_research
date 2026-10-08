from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from meta_research.owners.common import OwnerConflict, canonical_hash
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

    def run(self, call):
        if self._terminal:
            raise OwnerConflict("creation_work_closed")
        return self.runtime.run(call)

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
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or any(part in {".", ".."} for part in parts):
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

    def seal(self):
        try:
            identity = self.operation.seal()
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
