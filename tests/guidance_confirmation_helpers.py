"""Prepare public guidance confirmations for research behavior fixtures."""


def guidance_proposal_value(quest_ref, text, *, strength=3, work_materials=None, semantic_scope=None):
    return {"proposal_kind": "soft_constraint", "text": text,
        "assistant_understanding": text, "applies_to": ["The identified research scope."],
        "semantic_scope": semantic_scope or {"kind": "quest", "quest_ref": quest_ref},
        "strength": strength, "preserve_conditions": [], "work_materials": work_materials}


def prepare_guidance_submission(human, quest_ref, text, *, key, strength=3, work_materials=None, semantic_scope=None):
    proposal = human.record_agent_proposal("quest:" + quest_ref,
        guidance_proposal_value(quest_ref, text, strength=strength,
            work_materials=work_materials, semantic_scope=semantic_scope), key + "-proposal")
    return {"scope_ref": "quest:" + quest_ref, "text": text,
        "strength": strength, "work_materials": work_materials,
        "proposal_ref": proposal["proposal_ref"], "expected_proposal_hash": proposal["proposal_hash"]}
