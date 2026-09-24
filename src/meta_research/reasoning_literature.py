"""Resolve the exact frozen literature revision without embedding its corpus.

These helpers never treat a preview as an evidence authority. The supplied
reader is the RM Owner's immutable revision reader, not a latest lookup.
"""
from meta_research.context_presentation import literature_reference
from meta_research.owners.common import OwnerConflict


def frozen_reasoning_literature(context_pack, *, revision_reader=None, revision_verifier=None):
    value=context_pack.get("question_literature_input")
    if not isinstance(value,dict) or value.get("kind") not in {"none","revision"}:
        raise OwnerConflict("reasoning_literature_binding_invalid")
    if value["kind"]=="none":
        if set(value)!={"kind"}:
            raise OwnerConflict("reasoning_literature_binding_invalid")
        return None
    binding=value.get("binding")
    question=context_pack.get("accepted_question_binding",{})
    if (set(value)!={"kind","revision_ref","binding"} or not isinstance(binding,dict)
        or binding.get("revision_ref")!=value.get("revision_ref")
        or binding.get("question_ref")!=question.get("question_ref")):
        raise OwnerConflict("question_literature_revision_invalid")
    if binding.get("kind")=="QuestionLiteratureReference":
        if not callable(revision_reader):
            raise OwnerConflict("question_literature_revision_verifier_unavailable")
        exact=revision_reader(question_ref=question["question_ref"],revision_ref=value["revision_ref"])
        if exact is None or literature_reference(exact)!=binding:
            raise OwnerConflict("question_literature_revision_invalid")
        # The RM exact reader verifies the original snapshot and association.
        return exact
    if binding.get("kind")!="QuestionLiteratureRevision":
        raise OwnerConflict("reasoning_literature_binding_invalid")
    if callable(revision_verifier):
        revision_verifier(binding)
    return binding


def cited_literature_refs(*documents):
    refs=set()
    for document in documents:
        if not isinstance(document,dict):
            continue
        outcome=document.get("scientific_outcome",document)
        if not isinstance(outcome,dict) or not isinstance(outcome.get("evidence"),list):
            continue
        for citation in outcome["evidence"]:
            if isinstance(citation,dict) and citation.get("kind")=="LiteratureRecord":
                ref=citation.get("ref")
                if not isinstance(ref,str) or not ref:
                    raise OwnerConflict("scientific_outcome_evidence_invalid")
                refs.add(ref)
    return refs


def reasoning_literature_leaves(context_pack, *, revision_reader=None,
        revision_verifier=None, cited_documents=None):
    revision=frozen_reasoning_literature(context_pack,revision_reader=revision_reader,
        revision_verifier=revision_verifier)
    if revision is None:
        return []
    records=revision.get("records")
    if not isinstance(records,list):
        raise OwnerConflict("reasoning_literature_binding_invalid")
    # Old immutable packs retain their old complete leaf closure. New packs
    # store only actual citations, including citations removed in review.
    pointer=context_pack["question_literature_input"]["binding"].get("kind")=="QuestionLiteratureReference"
    selected=cited_literature_refs(*cited_documents) if pointer and cited_documents is not None else None
    leaves=[]
    seen=set()
    for record in records:
        if not isinstance(record,dict) or not isinstance(record.get("ref"),str) or record["ref"] in seen:
            raise OwnerConflict("reasoning_literature_binding_invalid")
        seen.add(record["ref"])
        if selected is None or record["ref"] in selected:
            leaves.append({"kind":"LiteratureRecord","ref":record["ref"],
                "evidence_basis":record.get("evidence_basis"),
                "evidence_basis_ref":record.get("evidence_basis_ref")})
    if selected is not None and not selected.issubset(seen):
        raise OwnerConflict("scientific_outcome_evidence_invalid")
    return leaves
