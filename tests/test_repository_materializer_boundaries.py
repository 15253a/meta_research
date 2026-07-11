"""CP11.4c.2b.1: keep repository materialization responsibilities separated."""
from __future__ import annotations

from orchestrator.repository_materializer import GitHubRepositoryMaterializer
from orchestrator.repository_materializer_adapter import _RepositoryAdapterMixin
from orchestrator.repository_materializer_archive import _RepositoryArchiveMixin
from orchestrator.repository_materializer_store import _RepositoryStoreMixin
from orchestrator.repository_materializer_transport import _RepositoryTransportMixin
from orchestrator.repository_materializer_tree import _RepositoryTreeMixin


def test_repository_materializer_facade_keeps_component_boundaries():
    expected_components = {
        _RepositoryTransportMixin,
        _RepositoryTreeMixin,
        _RepositoryArchiveMixin,
        _RepositoryAdapterMixin,
        _RepositoryStoreMixin,
    }
    assert expected_components <= set(GitHubRepositoryMaterializer.__mro__)

    expected_owners = {
        _RepositoryTransportMixin: {"_get_json", "_download_archive"},
        _RepositoryTreeMixin: {"_commit_tree", "_walk_tree"},
        _RepositoryArchiveMixin: {"_extract_archive", "_snapshot_repo"},
        _RepositoryAdapterMixin: {"_argv", "_adapter_spec"},
        _RepositoryStoreMixin: {"_verify_published", "_load_index"},
    }
    facade_methods = GitHubRepositoryMaterializer.__dict__
    for component, methods in expected_owners.items():
        assert methods <= component.__dict__.keys()
        assert methods.isdisjoint(facade_methods)
