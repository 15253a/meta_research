from fastapi.testclient import TestClient

from meta_research.web import create_app
from guidance_confirmation_helpers import prepare_guidance_submission
from test_public_human_reply_delivery import _login
from test_root_workspace import _make, _channel, _call, _accepted, _context, _external_context
from test_root_material_processing import _feedback
from test_public_plan_stage import _confirm_direct_quest, _DeterministicIdeaSkill
from test_dataset_effect_scope_recovery import _scope


def test_confirmed_material_scope_is_readable_and_retained_without_rebinding_the_successor(tmp_path):
    source = tmp_path / "local-observation.txt"
    source.write_text("Reported observation requiring independent calibration.\n")
    observed = {}

    class Consume(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            channel = _channel(self.runtime, _context(request, "idea"))
            with TestClient(create_app(self.runtime, base_url="http://testserver", control_key="control")) as client:
                headers = _login(client, self.runtime)
                scope = {"kind": "cycle", "quest_ref": observed["quest_ref"],
                    "question_ref": request.question_ref, "cycle_ref": request.context_pack["cycle_ref"]}
                receiver = client.get("/api/v1/work-materials/receiver", params={"quest_ref": observed["quest_ref"]}).json()
                selection = client.get("/api/v1/server-materials/inspect", params={"path": str(source)}).json()
                body = prepare_guidance_submission(self.runtime.owners.human_collaboration, observed["quest_ref"],
                    "仅本轮比较参考这份观察，总目标保持。", key="local-material", strength=5,
                    semantic_scope=scope, work_materials={"receiver": receiver, "selections": [selection],
                        "description": "Local observation, not confirmed scientific evidence."})
                submitted = client.post("/api/v1/human-collaboration/guidance", headers={**headers,
                    "Idempotency-Key": "confirm-local-material"}, json=body)
                assert submitted.status_code == 201, submitted.text
                reference = submitted.json()["work_materials"][0]["references"][0]
                provenance = reference["source_guidance"]
                assert provenance["semantic_scope"] == scope
                assert provenance["text"] == body["text"]
                assert provenance["scope_confirmation"] == "confirmed"
                assert provenance["assistant_understanding"]
                assert provenance["preserve_conditions"] == []
                assert reference["receiver"] == receiver
                discovered = _accepted(_call(self.runtime, channel, "research_workspace.materials.discover",
                    reference_ref=reference["reference_ref"]))
                observation = discovered["entries"][0]["observation"]["observation_ref"]
                treatment = _accepted(_call(self.runtime, channel, "research_workspace.materials.feedback",
                    **_feedback(reference["reference_ref"], selections=[{
                        "source": {"kind": "original_file", "path": "", "observation_ref": observation},
                        "custody": "linked_local", "purpose": "Keep the supplied observation with its local guidance boundary."}])))
                assert treatment["source_guidance"] == provenance
                observed.update(reference=reference["reference_ref"], treatment=treatment,
                    provenance=provenance, receiver=receiver)
            return super().generate_draft(request)

    provider = Consume()
    runtime = _make(tmp_path / "runtime", idea=provider)
    try:
        observed["quest_ref"] = _confirm_direct_quest(runtime)["quest_ref"]
        for _ in range(8):
            runtime.idea_stage.process_once()
            if "treatment" in observed:
                break
        assert "treatment" in observed
        successor = _external_context(_scope(runtime, quest_ref=observed["quest_ref"],
            run_ref="later-material-reader", root_ref="later-material-session"))
        discovered = _accepted(_call(runtime, _channel(runtime, successor), "research_workspace.materials.discover"))
        retained = next(item for item in discovered["retained_treatments"] if item["reference_ref"] == observed["reference"])
        assert retained["source_guidance"] == observed["provenance"]
        assert retained["receiver"] == observed["receiver"]
        assert observed["reference"] not in [item["reference_ref"] for item in discovered["references"]]
    finally:
        runtime.close()
