from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pytest

from meta_research.deepfetch import (
    DeepFetchUnavailable,
    _deepfetch_skill_root,
    _import_v4_public_artifacts,
)
from test_deepfetch_adapter import PROTOTYPE_FINAL, PROTOTYPE_LEDGER, _empty_reading


@pytest.mark.parametrize("origins", [[], [{"receipt_ref": "rm_asset_receipt_d3acf857920143a0a1c2ea45c8a28d3c"}]])
def test_provided_material_receipt_is_not_a_supplemental_discovery_receipt(tmp_path, origins):
    """The actual final CLI must explain the same boundary as the host importer."""
    paper_id = "doi:10.1000/example@" + hashlib.sha256(b"published").hexdigest()[:12]
    ledger = copy.deepcopy(PROTOTYPE_LEDGER)
    paper = ledger["papers"].pop("doi:10.1000/example")
    paper["identity"]["paper_id"] = paper_id
    paper.update(paper_version="published", discovery_origins=origins, fulltext_path=None)
    paper["reading"] = _empty_reading()
    ledger.update(schema_version="deepfetch.papers.v4.1", paper_order=[paper_id],
                  papers={paper_id: paper}, missing_fulltexts=[paper_id],
                  limitations=["Provided material does not contain the full paper."])
    ledger["run"].update(dimensions_used=[], active_search_elapsed_seconds=0)
    public = tmp_path / "public"
    (public / "fulltext").mkdir(parents=True)
    (public / "papers.json").write_text(json.dumps(ledger), encoding="utf-8")
    (public / "summary.md").write_text(f"Provided material only [{paper_id}].", encoding="utf-8")
    envelope = copy.deepcopy(PROTOTYPE_FINAL)
    envelope.update(completion="limited", limitations=ledger["limitations"])
    envelope["workflow"]["reader_assignments"] = []

    completed = subprocess.run([
        sys.executable, str(_deepfetch_skill_root() / "scripts" / "papers.py"),
        "validate", "--out-dir", str(public), "--final",
        "--literature-access-mode", "provided_only",
    ], capture_output=True, text=True)
    if origins:
        assert completed.returncode != 0
        assert "provided_only requires discovery_origins=[]" in completed.stdout + completed.stderr
        with pytest.raises(DeepFetchUnavailable, match="deepfetch_source_provenance_invalid"):
            _import_v4_public_artifacts(public, envelope, acquisition_request_ids=(),
                acquisition_item_proofs=(), literature_access_mode="provided_only")
    else:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        imported = _import_v4_public_artifacts(public, envelope, acquisition_request_ids=(),
            acquisition_item_proofs=(), literature_access_mode="provided_only")
        assert imported[5]["papers"][paper_id]["discovery_origins"] == []
        assert imported[0] == "limited"
