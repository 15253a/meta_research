from __future__ import annotations

import hashlib

import pytest

from orchestrator.artifact_capability import (ArtifactCapabilityError, open_artifact,
                                              read_artifact_bytes)


def test_open_descriptor_survives_path_swap_but_durable_binding_rejects_it(tmp_path):
    artifact = tmp_path / "checkpoint.bin"
    original = b"trusted-checkpoint"
    artifact.write_bytes(original)
    with open_artifact(
            artifact, expected_hash=hashlib.sha256(original).hexdigest()) as capability:
        moved = tmp_path / "moved.bin"
        artifact.rename(moved)
        artifact.write_bytes(b"replacement")
        capability.verify_unchanged()  # consumption still refers to the trusted open inode
        with pytest.raises(ArtifactCapabilityError, match="durable path"):
            capability.verify_path_binding()


def test_safe_read_rejects_symlink_leaf(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ArtifactCapabilityError, match="non symlink|symlink"):
        read_artifact_bytes(link)
