from __future__ import annotations

import pytest

from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    ROOT_ROLE_OPERATION_DELTAS,
    root_operation_catalog,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    TARGET_ROOT_SEMANTIC_OPERATION_IDS,
)


COMMON_OPERATION_IDS = (
    "agent_runtime.acquisition.request",
    "agent_runtime.acquisition.request.reconcile",
    "human_request.open",
    "human_request.open.reconcile",
)


@pytest.mark.parametrize(
    ("root_kind", "specialized_count"),
    (
        ("deepfetch", 0),
        ("acquisition", 0),
        ("companion", 0),
        ("idea", 0),
        ("plan", 0),
        ("bundle", 14),
        ("target", 1),
        ("reasoning", 3),
        ("writing", 0),
    ),
)
def test_catalog_is_common_operations_plus_only_the_role_delta(
    root_kind: str,
    specialized_count: int,
) -> None:
    catalog = root_operation_catalog(
        root_kind, common_operation_ids=COMMON_OPERATION_IDS
    )

    assert catalog[:specialized_count] == ROOT_ROLE_OPERATION_DELTAS[root_kind]
    assert catalog[specialized_count:] == COMMON_OPERATION_IDS
    assert len(catalog) == 4 + specialized_count
    assert len(catalog) == len(set(catalog))


def test_non_specialized_roots_do_not_invent_completion_operations() -> None:
    for root_kind in ROOT_AGENT_KINDS:
        if root_kind in {"bundle", "target", "reasoning"}:
            continue
        catalog = root_operation_catalog(
            root_kind, common_operation_ids=COMMON_OPERATION_IDS
        )
        assert catalog == COMMON_OPERATION_IDS
        assert not any(
            operation_id.endswith((".submit", ".complete", ".status"))
            for operation_id in catalog
        )


def test_catalog_rejects_duplicate_or_overlapping_grants() -> None:
    with pytest.raises(ValueError, match="root_common_operation_catalog_invalid"):
        root_operation_catalog(
            "idea", common_operation_ids=("human_request.open",) * 2
        )
    with pytest.raises(ValueError, match="root_operation_catalog_overlap"):
        root_operation_catalog(
            "target",
            common_operation_ids=("agent_runtime.target_run.observe",),
        )


def test_all_root_catalogs_use_the_registered_human_request_operations() -> None:
    assert tuple(ROOT_AGENT_SEMANTIC_OPERATION_IDS) == ROOT_AGENT_KINDS
    for root_kind, catalog in ROOT_AGENT_SEMANTIC_OPERATION_IDS.items():
        assert catalog == root_operation_catalog(
            root_kind,
            common_operation_ids=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS,
        )
        assert not any("acquisition.request" in item for item in catalog)

    assert BUNDLE_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS[
        "bundle"
    ]
    assert TARGET_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS[
        "target"
    ]
    assert REASONING_ROOT_SEMANTIC_OPERATION_IDS == (
        ROOT_AGENT_SEMANTIC_OPERATION_IDS["reasoning"]
    )
