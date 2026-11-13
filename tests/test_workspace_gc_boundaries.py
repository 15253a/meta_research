"""Original-path protection and filesystem boundaries in isolated workspaces."""
import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
import meta_research.target_workspace_cleanup as cleanup_module
from meta_research.target_workspace_cleanup import (
    _directory_fd, _protected_originals, _workspace_bytes,
)
from test_completed_workspace_cleanup import _complete, _target_cleanup


def test_mixed_workspace_keeps_exact_files_directory_and_alias_original(tmp_path, monkeypatch):
    runtime, _, _, workspace, _, _, _ = _complete(tmp_path)
    try:
        parent = workspace / 'originals' / 'nested'
        parent.mkdir(parents=True)
        original = parent / 'data.csv'
        original.write_bytes(b'subject,value\na,36\n')
        directory = workspace / 'registered-directory'
        (directory / 'nested').mkdir(parents=True)
        (directory / 'nested' / 'data.txt').write_bytes(b'entire registered directory survives')
        (directory / 'empty').mkdir()
        aliased = parent / 'via-alias.txt'
        aliased.write_bytes(b'real original reached through external parent alias')
        alias = tmp_path / 'original-parent-alias'
        alias.symlink_to(parent, target_is_directory=True)
        (parent / 'scratch.txt').write_bytes(b'delete sibling')
        (workspace / 'originals' / 'unused').mkdir()
        (workspace / 'originals' / 'unused' / 'scratch.txt').write_bytes(b'delete subtree')
        (workspace / 'unregistered-empty').mkdir()
        (workspace / 'scratch.bin').write_bytes(b'delete top-level scratch')

        locators = (original, directory, alias / aliased.name)
        assets = []
        for index, locator in enumerate(locators):
            asset = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind='local_path', custody_mode='linked_local',
                display_name=locator.name, source_locator=str(locator)),
                idempotency_key=f'mixed-original-{index}').asset
            assert asset is not None
            assets.append(asset)
        # The alias itself lies outside the workspace; protection must include
        # its actual destination, without retaining unrelated parent contents.
        assert aliased in _protected_originals(tuple(map(str, locators)))
        identities = {path: path.stat().st_ino for path in (original, directory, aliased)}
        late_original = parent / 'registered-during-scan.csv'
        late_original.write_bytes(b'new custody must be seen before removal')
        scan = cleanup_module._workspace_bytes

        def register_during_scan(path, *args, **kwargs):
            amount = scan(path, *args, **kwargs)
            if path == workspace:
                asset = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                    source_kind='local_path', custody_mode='linked_local',
                    display_name=late_original.name, source_locator=str(late_original)),
                    idempotency_key='registered-during-scan').asset
                assert asset is not None
            return amount

        with monkeypatch.context() as patch:
            patch.setattr(cleanup_module, '_workspace_bytes', register_during_scan)
            skipped, = _target_cleanup(runtime, dry_run=False)
        assert skipped['reason'] == 'linked_local_scope_changed', skipped
        assert skipped['action'] == 'skipped'
        assert (workspace / 'scratch.bin').exists()
        assert late_original.read_bytes() == b'new custody must be seen before removal'
        with runtime._database.read() as connection:
            before = list(connection.exec_driver_sql('SELECT * FROM rm_asset_custodies'))

        report, = _target_cleanup(runtime, dry_run=False)
        assert report['action'] == 'pruned', report
        assert {path.relative_to(workspace).as_posix() for path in workspace.rglob('*')} == {
            'originals', 'originals/nested', 'originals/nested/data.csv',
            'originals/nested/via-alias.txt', 'originals/nested/registered-during-scan.csv',
            'registered-directory',
            'registered-directory/nested', 'registered-directory/nested/data.txt',
            'registered-directory/empty',
        }
        assert all(path.stat().st_ino == inode for path, inode in identities.items())
        assert runtime.owners.research_memory.materialize_asset(assets[0].version_ref).content == original.read_bytes()
        assert runtime.owners.research_memory.materialize_asset(assets[2].version_ref).content == aliased.read_bytes()
        assert (directory / 'nested' / 'data.txt').read_bytes() == b'entire registered directory survives'
        with runtime._database.read() as connection:
            assert list(connection.exec_driver_sql('SELECT * FROM rm_asset_custodies')) == before
    finally:
        runtime.close()


def test_directory_handles_reject_symlinked_ancestors(tmp_path):
    external = tmp_path / 'external'
    child = external / 'workspace'
    child.mkdir(parents=True)
    sentinel = child / 'original.txt'
    sentinel.write_bytes(b'external original')
    alias = tmp_path / 'alias'
    alias.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        with _directory_fd(alias / 'workspace'):
            pytest.fail('opened a workspace through a symlink')
    assert sentinel.read_bytes() == b'external original'


def test_boundary_scan_rejects_external_symlink_without_traversing(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    external = tmp_path / 'external'
    external.mkdir()
    sentinel = external / 'original.txt'
    sentinel.write_bytes(b'external original')
    (workspace / 'escape').symlink_to(external, target_is_directory=True)
    with pytest.raises(OwnerConflict, match='workspace_cleanup_unsafe_boundary'):
        _workspace_bytes(workspace)
    assert sentinel.read_bytes() == b'external original'


@pytest.mark.parametrize('locator', [None, '', 'relative/original.txt', 'missing'])
def test_unknown_original_scope_is_rejected(tmp_path, locator):
    if locator == 'missing':
        locator = str(tmp_path / 'missing-original')
    with pytest.raises(OwnerConflict, match='linked_local_locator_unavailable'):
        _protected_originals((locator,))
