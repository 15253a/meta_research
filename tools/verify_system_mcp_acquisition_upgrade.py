"""Run once on baseline with 'capture', then on release with 'verify'.

Use the same disposable workspace and executable on both runs. No production
database or device is accessed; this checks the public Acquisition binding.
"""
from dataclasses import replace
import json
from pathlib import Path
import sys

from meta_research.acquisition import AcquisitionRuntimeBinding
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.owners.common import canonical_hash


class Delegate:
    def runtime_binding(self):
        return AcquisitionRuntimeBinding("upgrade-fixture", "1", ("lawful-fulltext-routing",))


mode, workspace_arg, evidence_arg = sys.argv[1:]
adapter = CodexAcquisitionRootAdapter(Path(workspace_arg), Delegate())
binding = adapter.runtime_binding()
evidence_path = Path(evidence_arg)
if mode == "capture":
    evidence_path.write_text(json.dumps(binding.as_dict(), indent=2), encoding="utf-8")
else:
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    historical = AcquisitionRuntimeBinding(value["provider_ref"], value["provider_version"], tuple(value["capability_bindings"]))
    assert binding != historical
    assert adapter.runtime_binding_compatible(historical)
    assert not adapter.runtime_binding_compatible(replace(historical, provider_version="changed-delegate"))
    assert not adapter.runtime_binding_compatible(replace(historical, capability_bindings=("unreviewed-capability",)))
    print(json.dumps({
        "baseline_binding_hash": canonical_hash(historical.as_dict()),
        "release_binding_hash": canonical_hash(binding.as_dict()),
        "compatible": True,
        "changed_delegate_rejected": True,
        "changed_capabilities_rejected": True,
    }, indent=2))
