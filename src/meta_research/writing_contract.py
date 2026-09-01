from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import re
from typing import Protocol

from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash
from meta_research.root_capabilities import root_capability_profile


WRITING_REPORT_INTENT_SCHEMA = "meta-research/writing-report-intent/v1"
WRITING_PAPER_INTENT_SCHEMA = "meta-research/writing-paper-intent/v1"
WRITING_PRESENTATION_INTENT_SCHEMA = (
    "meta-research/writing-presentation-intent/v1"
)
WRITING_RESEARCH_SNAPSHOT_SCHEMA = "meta-research/writing-research-snapshot/v1"
WRITING_RUNTIME_BINDING_SCHEMA = "meta-research/writing-runtime-binding/v1"
WRITING_EXECUTION_BUDGET_SCHEMA = "meta-research/writing-execution-budget/v1"
WRITING_CHILD_REVIEW_TASK_SCHEMA = "meta-research/writing-child-review-task/v1"
WRITING_ADVISORY_REVIEW_TASK_SCHEMA = (
    "meta-research/writing-advisory-review-task/v1"
)

WRITING_CHILD_REVIEW_RUBRIC = (
    "evidence_coverage",
    "citation_binding",
    "unsupported_certainty",
    "internal_consistency",
    "intent_alignment",
)
WRITING_ADVISORY_REVIEW_RUBRIC = WRITING_CHILD_REVIEW_RUBRIC

# Deprecated public alias retained only because the immutable pre-0030 Writing
# module imports it while reconstructing its historical runtime binding. Current
# admissions use ``default_writing_execution_budget`` below and never enforce
# this value as a logical ceiling.
WRITING_MAX_CONTENT_REVISIONS = 5
WRITING_MAX_OUTPUT_BYTES = 24 * 1024 * 1024
_LEGACY_WRITING_MAX_CONTENT_REVISIONS = WRITING_MAX_CONTENT_REVISIONS
_LEGACY_WRITING_MAX_OUTPUT_BYTES = 24 * 1024 * 1024

_CLAIM_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SUPPORTED_CLAIM_MARKER = re.compile(
    r"\A<!-- meta-research-claim:supported refs=([^\n]+) -->\n(.+)\Z",
    re.DOTALL,
)
_CLASSIFIED_CLAIM_MARKER = re.compile(
    r"\A<!-- meta-research-claim:(inference|uncertainty|evidence-gap) -->\n(.+)\Z",
    re.DOTALL,
)
_STRUCTURE_MARKER = re.compile(
    r"\A<!-- meta-research-structure -->\n(#{2,6} [^\n]+)\Z"
)
_PAPER_SECTION_MARKER = re.compile(
    r"\A<!-- meta-research-paper-section role="
    r"(abstract|framing|related-work|methods|model|evidence|results|analysis|"
    r"synthesis|evaluation|discussion|limitations|implications|conclusion|appendix)"
    r" -->\n(## [^\n]+)\Z"
)
_DOCUMENT_TITLE = re.compile(r"\A# [^\n]+\Z")
_CITATION_ANCHOR = re.compile(
    r"\[\[citation:([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\]\]"
)
_VISIBLE_CLASSIFICATION = {
    "inference": "**Inference:**",
    "uncertainty": "**Uncertainty:**",
    "evidence-gap": "**Evidence gap:**",
}

_WRITING_SAFE_CAPABILITIES = {
    "accepted-rm-source-staging",
    "approval-policy-never",
    "environment-inheritance-none",
    "filesystem-read-root-confined",
    "global-config-ignored",
    "user-config-loaded",
    "harness-child-agent-review",
    "mcp-config-empty",
    "native-session-resume",
    "shell-tool-enabled",
    "structured-output-json-schema",
    "trusted-local-quest-authorization",
    "external-research-disabled",
}
_WRITING_SAFE_RESOURCE_PREFIXES = (
    "adapter-source:",
    "codex-config:",
    "disabled-codex-config:",
    "disabled-codex-features:",
    "external-effects:",
    "harness-artifact:",
    "output-route:",
    "output-schema:",
    "package:",
    "provider-output-limits:",
    "provider-timeout-seconds:",
    "runtime-policy:",
    "sandbox-policy:",
    "transport-seal-key:",
    "writing-run-limits:",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PRESENTATION_HEADING = re.compile(r"## Slide ([1-9][0-9]?): ([^\n]+)\Z")

WRITING_DOCUMENT_TYPES = ("report", "paper", "presentation")


@dataclass(frozen=True)
class WritingDocumentProfile:
    """Canonical authoring and default-render contract for one document type.

    The registry of concrete renderers remains open.  These defaults identify
    the product's built-in profile; they are not a closed list of formats or
    providers.
    """

    document_type: str
    profile_ref: str
    intent_schema_ref: str
    default_format: str
    canonical_extension: str
    media_type: str

    @property
    def canonical_media_type(self) -> str:
        return self.media_type


_DOCUMENT_PROFILES = {
    "report": WritingDocumentProfile(
        document_type="report",
        profile_ref="report-v1",
        intent_schema_ref=WRITING_REPORT_INTENT_SCHEMA,
        default_format="markdown",
        canonical_extension=".md",
        media_type="text/markdown; charset=utf-8",
    ),
    "paper": WritingDocumentProfile(
        document_type="paper",
        profile_ref="paper-v1",
        intent_schema_ref=WRITING_PAPER_INTENT_SCHEMA,
        default_format="docx",
        canonical_extension=".docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    ),
    "presentation": WritingDocumentProfile(
        document_type="presentation",
        profile_ref="presentation-v1",
        intent_schema_ref=WRITING_PRESENTATION_INTENT_SCHEMA,
        default_format="pptx",
        canonical_extension=".pptx",
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
    ),
}


def writing_document_profile(document_type: str) -> WritingDocumentProfile:
    try:
        return _DOCUMENT_PROFILES[document_type]
    except (KeyError, TypeError) as error:
        raise OwnerConflict("writing_document_type_invalid") from error


def writing_child_review_document_profile(
    document_type: str,
) -> dict[str, str] | None:
    """Return the exact type profile available to a fresh-context reviewer.

    Report review predates document profiles and intentionally keeps its
    historical task bytes.  New document types bind the packaged reference
    text and its digest into both the child task and its durable effect hash.
    """

    profile = writing_document_profile(document_type)
    if document_type == "report":
        return None
    reference_name = f"references/{profile.profile_ref}.md"
    content = (
        files("meta_research")
        / "skills"
        / "writing-report"
        / reference_name
    ).read_text(encoding="utf-8")
    return {
        "document_type": profile.document_type,
        "profile_ref": profile.profile_ref,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def writing_advisory_review_document_profile(
    document_type: str,
) -> dict[str, str] | None:
    """Return the current root-finalization profile without changing history.

    The original profile bytes remain an input to historical child task hashes.
    Current bindings use a separately named advisory resource so those immutable
    hashes stay reproducible.
    """

    profile = writing_document_profile(document_type)
    if document_type == "report":
        return None
    reference_name = f"references/{profile.profile_ref}-advisory.md"
    content = (
        files("meta_research")
        / "skills"
        / "writing-report"
        / reference_name
    ).read_text(encoding="utf-8")
    return {
        "document_type": profile.document_type,
        "profile_ref": profile.profile_ref,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def writing_intent_schema(document_type: str) -> str:
    return writing_document_profile(document_type).intent_schema_ref


@dataclass(frozen=True)
class WritingRuntimeBinding:
    packaged_skill_bundle_hash: str
    instruction_set_hash: str
    model_ref: str
    harness_adapter_ref: str
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": WRITING_RUNTIME_BINDING_SCHEMA,
            "packaged_skill_bundle_hash": self.packaged_skill_bundle_hash,
            "instruction_set_hash": self.instruction_set_hash,
            "model_ref": self.model_ref,
            "harness_adapter_ref": self.harness_adapter_ref,
            "mcp_bindings": list(self.mcp_bindings),
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }

    def validate(self) -> None:
        for value in (
            self.packaged_skill_bundle_hash,
            self.instruction_set_hash,
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise OwnerConflict("writing_runtime_binding_invalid")
        for value in (self.model_ref, self.harness_adapter_ref):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise OwnerConflict("writing_runtime_binding_invalid")
        for values in (
            self.mcp_bindings,
            self.capability_bindings,
            self.resource_bindings,
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > 256
                or len(values) != len(set(values))
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > 2048
                    for value in values
                )
            ):
                raise OwnerConflict("writing_runtime_binding_invalid")
        if (
            self.mcp_bindings
            or any(
                value
                not in (
                    _WRITING_SAFE_CAPABILITIES
                    | set(
                        root_capability_profile("writing").runtime_bindings()
                    )
                )
                for value in self.capability_bindings
            )
            or any(
                not value.startswith(_WRITING_SAFE_RESOURCE_PREFIXES)
                or "\n" in value
                or "\r" in value
                or "../" in value
                for value in self.resource_bindings
            )
        ):
            raise OwnerConflict("writing_runtime_binding_unauthorized")


def default_writing_execution_budget() -> dict[str, object]:
    return {
        "schema_ref": WRITING_EXECUTION_BUDGET_SCHEMA,
        "max_content_revisions": None,
        "max_output_bytes": None,
    }


def validate_writing_execution_budget(value: object) -> dict[str, object]:
    expected = default_writing_execution_budget()
    if isinstance(value, dict) and value == expected:
        return dict(expected)
    # Runs admitted by an older build retain their immutable budget document,
    # but those former logical ceilings are no longer enforced. Accepting the
    # exact legacy shape preserves restart compatibility without carrying the
    # old stop policy into successor Attempts.
    legacy = {
        "schema_ref": WRITING_EXECUTION_BUDGET_SCHEMA,
        "max_content_revisions": _LEGACY_WRITING_MAX_CONTENT_REVISIONS,
        "max_output_bytes": _LEGACY_WRITING_MAX_OUTPUT_BYTES,
    }
    if not isinstance(value, dict) or value != legacy:
        raise OwnerConflict("writing_execution_budget_invalid")
    return dict(legacy)


def writing_child_review_task_hash(
    *,
    run_ref: str,
    provider_job_ref: str | None,
    root_session_ref: str,
    primary_session_ref: str,
    intent_hash: str,
    snapshot_hash: str,
    predecessor_version_ref: str | None,
    predecessor_markdown_hash: str | None,
    feedback_hash: str,
    reviewed_markdown_hash: str,
    reviewed_citations_hash: str,
    document_type: str = "report",
) -> str:
    """Bind one durable advisory operation independently of a crash Fence.

    Attempt/Fence identify the current Owner lease and intentionally rotate on
    restart.  The provider job and exact content/lineage identify the external
    review effect and remain stable so a completed-but-unacknowledged review can
    be reconciled without running the native Session turn again.
    """

    task = {
        "schema_ref": WRITING_CHILD_REVIEW_TASK_SCHEMA,
        "run_ref": run_ref,
        "provider_job_ref": provider_job_ref,
        "root_session_ref": root_session_ref,
        "primary_session_ref": primary_session_ref,
        "intent_hash": intent_hash,
        "snapshot_hash": snapshot_hash,
        "predecessor_version_ref": predecessor_version_ref,
        "predecessor_markdown_hash": predecessor_markdown_hash,
        "feedback_hash": feedback_hash,
        "reviewed_markdown_hash": reviewed_markdown_hash,
        "reviewed_citations_hash": reviewed_citations_hash,
        "rubric": list(WRITING_CHILD_REVIEW_RUBRIC),
        "fresh_context_mode": "fork_turns:none",
    }
    document_profile = writing_child_review_document_profile(document_type)
    if document_profile is not None:
        task["document_profile"] = document_profile
    return canonical_hash(task)


def writing_advisory_review_task_hash(
    *,
    run_ref: str,
    provider_job_ref: str | None,
    root_session_ref: str,
    primary_session_ref: str,
    intent_hash: str,
    snapshot_hash: str,
    predecessor_version_ref: str | None,
    predecessor_markdown_hash: str | None,
    feedback_hash: str,
    reviewed_markdown_hash: str,
    reviewed_citations_hash: str,
    document_type: str = "report",
) -> str:
    """Bind the current root advisory finalization to immutable inputs."""

    task = {
        "schema_ref": WRITING_ADVISORY_REVIEW_TASK_SCHEMA,
        "run_ref": run_ref,
        "provider_job_ref": provider_job_ref,
        "root_session_ref": root_session_ref,
        "primary_session_ref": primary_session_ref,
        "intent_hash": intent_hash,
        "snapshot_hash": snapshot_hash,
        "predecessor_version_ref": predecessor_version_ref,
        "predecessor_markdown_hash": predecessor_markdown_hash,
        "feedback_hash": feedback_hash,
        "reviewed_markdown_hash": reviewed_markdown_hash,
        "reviewed_citations_hash": reviewed_citations_hash,
        "rubric": list(WRITING_ADVISORY_REVIEW_RUBRIC),
    }
    document_profile = writing_advisory_review_document_profile(document_type)
    if document_profile is not None:
        task["document_profile"] = document_profile
    return canonical_hash(task)


def validate_writing_claim_inventory(
    markdown: str,
    citations: tuple[dict[str, str], ...],
) -> dict[str, object]:
    """Extract the complete, explicit claim coverage carried by a report.

    Headings are structural. Every other Markdown block must declare itself as
    supported, inference, uncertainty, or an evidence gap. Supported blocks
    enumerate citation refs and display matching inline anchors, making omitted
    material claims mechanically rejectable by RG.
    """

    if not isinstance(markdown, str) or not markdown.strip():
        raise OwnerConflict("writing_claim_inventory_invalid")
    citations_by_ref = {
        citation.get("citation_ref"): citation
        for citation in citations
        if isinstance(citation, dict)
        and isinstance(citation.get("citation_ref"), str)
    }
    if len(citations_by_ref) != len(citations):
        raise OwnerConflict("writing_claim_inventory_invalid")
    supported: list[dict[str, object]] = []
    classifications: list[dict[str, str]] = []
    structures: list[str] = []
    used_refs: set[str] = set()
    blocks = re.split(r"\n[ \t]*\n", markdown.strip())
    for ordinal, raw_block in enumerate(blocks):
        block = raw_block.strip()
        if not block:
            continue
        if ordinal == 0 and _DOCUMENT_TITLE.fullmatch(block) is not None:
            structures.append(block)
            continue
        structure_match = _STRUCTURE_MARKER.fullmatch(block)
        if structure_match is not None:
            structures.append(structure_match.group(1))
            continue
        paper_section_match = _PAPER_SECTION_MARKER.fullmatch(block)
        if paper_section_match is not None:
            structures.append(paper_section_match.group(2))
            continue
        supported_match = _SUPPORTED_CLAIM_MARKER.fullmatch(block)
        if supported_match is not None:
            refs = tuple(
                item.strip() for item in supported_match.group(1).split(",")
            )
            body = supported_match.group(2).strip()
            anchors = tuple(_CITATION_ANCHOR.findall(body))
            if (
                not refs
                or len(refs) != len(set(refs))
                or any(_CLAIM_REF.fullmatch(item) is None for item in refs)
                or set(anchors) != set(refs)
                or len(anchors) != len(refs)
                or any(item not in citations_by_ref for item in refs)
            ):
                raise OwnerConflict("writing_claim_inventory_invalid")
            claim = _normalize_claim_text(_CITATION_ANCHOR.sub("", body))
            if not claim:
                raise OwnerConflict("writing_claim_inventory_invalid")
            for citation_ref in refs:
                citation = citations_by_ref[citation_ref]
                if _normalize_claim_text(str(citation.get("claim", ""))) != claim:
                    raise OwnerConflict("writing_claim_inventory_invalid")
                if citation_ref in used_refs:
                    raise OwnerConflict("writing_claim_inventory_invalid")
                used_refs.add(citation_ref)
            supported.append(
                {
                    "ordinal": ordinal,
                    "claim": claim,
                    "citation_refs": list(refs),
                }
            )
            continue
        classified_match = _CLASSIFIED_CLAIM_MARKER.fullmatch(block)
        if classified_match is not None:
            classification = classified_match.group(1)
            body = classified_match.group(2).strip()
            if (
                not body.startswith(_VISIBLE_CLASSIFICATION[classification])
                or _CITATION_ANCHOR.search(body) is not None
            ):
                raise OwnerConflict("writing_claim_inventory_invalid")
            classifications.append(
                {
                    "classification": classification,
                    "text": _normalize_claim_text(body),
                }
            )
            continue
        raise OwnerConflict("writing_claim_unclassified")
    if (
        not structures
        or not structures[0].startswith("# ")
        or used_refs != set(citations_by_ref)
    ):
        raise OwnerConflict("writing_claim_inventory_invalid")
    return {
        "schema_ref": "meta-research/writing-claim-inventory/v1",
        "supported_claims": supported,
        "classified_claims": classifications,
        "structures": structures,
    }


def validate_writing_document(
    document_type: str,
    markdown: str,
    citations: tuple[dict[str, str], ...],
) -> dict[str, object]:
    """Validate canonical content semantics before an Owner records execution.

    A paper and a presentation share the report-v1 claim/citation ledger, but
    they do not share its unconstrained outline.  Their authoring structures
    are validated here, before any renderer turns the semantic source into an
    Office package.  A renderer is consequently incapable of making a report
    masquerade as another document type by changing only its extension.
    """

    profile = writing_document_profile(document_type)
    inventory = validate_writing_claim_inventory(markdown, citations)
    result = {
        **inventory,
        "document_type": profile.document_type,
        "profile_ref": profile.profile_ref,
    }
    if document_type == "report":
        return result
    blocks = tuple(
        block.strip()
        for block in re.split(r"\n[ \t]*\n", markdown.strip())
        if block.strip()
    )
    if document_type == "paper":
        paper = _validate_paper_structure(blocks)
        return {**result, **paper}
    if document_type == "presentation":
        slide_count = _validate_presentation_structure(blocks)
        return {**result, "slide_count": slide_count}
    raise OwnerConflict("writing_document_type_invalid")


def _validate_paper_structure(
    blocks: tuple[str, ...]
) -> dict[str, object]:
    roles: list[str] = []
    headings: list[str] = []
    content_counts: list[int] = []
    for block in blocks[1:]:
        match = _PAPER_SECTION_MARKER.fullmatch(block)
        if match is not None:
            roles.append(match.group(1))
            headings.append(match.group(2)[3:].strip())
            content_counts.append(0)
        elif _STRUCTURE_MARKER.fullmatch(block) is not None:
            raise OwnerConflict("writing_paper_structure_invalid")
        elif not content_counts:
            raise OwnerConflict("writing_paper_structure_invalid")
        else:
            content_counts[-1] += 1
    core_roles = {
        "methods",
        "model",
        "evidence",
        "results",
        "analysis",
        "synthesis",
        "evaluation",
    }
    qualification_roles = {"discussion", "limitations", "implications"}
    conclusion_index = next(
        (index for index, role in enumerate(roles) if role == "conclusion"),
        -1,
    )
    if (
        not 5 <= len(roles) <= 24
        or roles[:2] != ["abstract", "framing"]
        or roles.count("abstract") != 1
        or roles.count("framing") != 1
        or roles.count("conclusion") != 1
        or conclusion_index < 0
        or "appendix" in roles[:conclusion_index]
        or any(role != "appendix" for role in roles[conclusion_index + 1 :])
        or not core_roles.intersection(roles)
        or not qualification_roles.intersection(roles)
        or len(set(headings)) != len(headings)
        or any(not heading for heading in headings)
        or not all(content_counts)
    ):
        raise OwnerConflict("writing_paper_structure_invalid")
    return {
        "section_count": len(roles),
        "section_roles": list(roles),
    }


def _validate_presentation_structure(blocks: tuple[str, ...]) -> int:
    headings: list[tuple[int, str]] = []
    content_counts: list[int] = []
    for block in blocks[1:]:
        structure = _STRUCTURE_MARKER.fullmatch(block)
        if structure is not None:
            match = _PRESENTATION_HEADING.fullmatch(structure.group(1))
            if match is None:
                raise OwnerConflict("writing_presentation_structure_invalid")
            headings.append((int(match.group(1)), match.group(2).strip()))
            content_counts.append(0)
        elif _PAPER_SECTION_MARKER.fullmatch(block) is not None:
            raise OwnerConflict("writing_presentation_structure_invalid")
        elif not content_counts:
            raise OwnerConflict("writing_presentation_structure_invalid")
        else:
            content_counts[-1] += 1
    if (
        not 2 <= len(headings) <= 40
        or tuple(number for number, _title in headings)
        != tuple(range(1, len(headings) + 1))
        or any(not title for _number, title in headings)
        or len({title for _number, title in headings}) != len(headings)
        or not all(content_counts)
    ):
        raise OwnerConflict("writing_presentation_structure_invalid")
    return len(headings)


def _normalize_claim_text(value: str) -> str:
    return " ".join(value.split())


@dataclass(frozen=True)
class WritingIntentBinding:
    intent_id: str
    quest_ref: str
    document_type: str
    intent: dict[str, object]
    intent_hash: str
    snapshot: dict[str, object]
    snapshot_ref: str
    snapshot_hash: str
    execution_budget: dict[str, object]
    draft_revision: int
    draft_hash: str
    preview_ref: str
    preview_hash: str
    confirmation: AcceptanceReceipt

    def as_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "quest_ref": self.quest_ref,
            "document_type": self.document_type,
            "intent": self.intent,
            "intent_hash": self.intent_hash,
            "snapshot": self.snapshot,
            "snapshot_ref": self.snapshot_ref,
            "snapshot_hash": self.snapshot_hash,
            "execution_budget": self.execution_budget,
            "draft_revision": self.draft_revision,
            "draft_hash": self.draft_hash,
            "preview_ref": self.preview_ref,
            "preview_hash": self.preview_hash,
            "confirmation": self.confirmation.as_public_dict(),
        }

    def validate(self) -> None:
        if normalize_writing_intent(self.document_type, self.intent) != self.intent:
            raise OwnerConflict("writing_intent_invalid")
        if canonical_hash(self.intent) != self.intent_hash:
            raise OwnerConflict("writing_intent_hash_mismatch")
        if self.snapshot.get("snapshot_ref") != self.snapshot_ref:
            raise OwnerConflict("writing_snapshot_binding_invalid")
        snapshot_without_hash = dict(self.snapshot)
        embedded_hash = snapshot_without_hash.pop("snapshot_hash", None)
        if (
            embedded_hash != self.snapshot_hash
            or canonical_hash(snapshot_without_hash) != self.snapshot_hash
        ):
            raise OwnerConflict("writing_snapshot_hash_mismatch")
        if self.snapshot.get("quest_ref") != self.quest_ref:
            raise OwnerConflict("writing_snapshot_quest_mismatch")
        validate_writing_execution_budget(self.execution_budget)
        _validate_writing_snapshot_shape(self.snapshot)
        for value in (
            self.intent_id,
            self.quest_ref,
            self.snapshot_ref,
            self.preview_ref,
            self.confirmation.receipt_ref,
        ):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise OwnerConflict("writing_intent_binding_invalid")
        for value in (
            self.intent_hash,
            self.snapshot_hash,
            self.draft_hash,
            self.preview_hash,
            self.confirmation.payload_hash,
        ):
            if len(value) != 64:
                raise OwnerConflict("writing_intent_binding_invalid")


def _validate_writing_snapshot_shape(snapshot: dict[str, object]) -> None:
    required = {
        "schema_ref",
        "quest_ref",
        "quest",
        "questions",
        "accepted_sources",
        "advancement",
        "owner_revisions",
        "snapshot_ref",
        "snapshot_hash",
    }
    revisions = snapshot.get("owner_revisions")
    if (
        set(snapshot) != required
        or snapshot.get("schema_ref") != WRITING_RESEARCH_SNAPSHOT_SCHEMA
        or not isinstance(snapshot.get("quest"), dict)
        or not isinstance(snapshot.get("questions"), list)
        or not isinstance(snapshot.get("accepted_sources"), list)
        or not isinstance(snapshot.get("advancement"), dict)
        or not isinstance(revisions, dict)
        or set(revisions)
        != {
            "research_graph",
            "research_memory",
            "advancement_engine",
        }
        or any(
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
            for revision in revisions.values()
        )
    ):
        raise OwnerConflict("writing_snapshot_invalid")


def validate_frozen_writing_snapshot(snapshot: dict[str, object]) -> None:
    """Validate one immutable Writing input cut without reading live research.

    Once captured, a Snapshot is intentionally independent of later Quest,
    Stage, source, or Owner revisions.  ``owner_revisions`` are capture-time
    lower-bound observations, not an atomic cross-Owner cut or a currentness
    gate.  The Snapshot's closed value and hashes remain authoritative for
    this Writing intent.
    """

    if type(snapshot) is not dict:
        raise OwnerConflict("writing_snapshot_invalid")
    _validate_writing_snapshot_shape(snapshot)
    document = dict(snapshot)
    snapshot_hash = document.pop("snapshot_hash", None)
    snapshot_ref = document.get("snapshot_ref")
    payload = dict(document)
    payload.pop("snapshot_ref", None)
    basis_hash = canonical_hash(payload)
    quest = snapshot.get("quest")
    if (
        type(snapshot_hash) is not str
        or len(snapshot_hash) != 64
        or canonical_hash(document) != snapshot_hash
        or snapshot_ref != f"writing_snapshot_{basis_hash[:32]}"
        or type(snapshot.get("quest_ref")) is not str
        or not snapshot["quest_ref"]
        or type(quest) is not dict
        or quest.get("quest_ref") != snapshot["quest_ref"]
    ):
        raise OwnerConflict("writing_snapshot_invalid")


class WritingConfirmationVerifier(Protocol):
    def verify_command_confirmation(
        self,
        *,
        intent_id: str,
        command_kind: str,
        draft_revision: int,
        draft_hash: str,
        preview_ref: str,
        preview_hash: str,
        receipt: AcceptanceReceipt,
    ) -> dict[str, object]: ...


class WritingCitationDecisionVerifier(Protocol):
    def verify_writing_citation_decision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        version_ref: str,
        feedback: tuple[str, ...],
        receipt: AcceptanceReceipt,
        expected_decision: str,
    ) -> None: ...

    def verify_broad_research_authorization(
        self, *, quest_ref: str
    ) -> dict[str, object]: ...


def normalize_writing_intent(
    document_type: str, value: dict[str, object]
) -> dict[str, object]:
    profile = writing_document_profile(document_type)
    if set(value) != {"schema_ref", "title", "audience", "purpose", "instructions"}:
        raise OwnerConflict("writing_intent_invalid")
    if value.get("schema_ref") != profile.intent_schema_ref:
        raise OwnerConflict("writing_intent_invalid")
    return {
        "schema_ref": profile.intent_schema_ref,
        "title": _text(value.get("title"), "writing_title_invalid", 512),
        "audience": _text(value.get("audience"), "writing_audience_invalid", 2000),
        "purpose": _text(value.get("purpose"), "writing_purpose_invalid", 4000),
        "instructions": _text(
            value.get("instructions"), "writing_instructions_invalid", 12000, empty=True
        ),
    }


def normalize_report_intent(value: dict[str, object]) -> dict[str, object]:
    """Compatibility wrapper retaining the report-v1 intent contract."""

    return normalize_writing_intent("report", value)


def _text(value: object, code: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise OwnerConflict(code)
    normalized = value.strip()
    if (not normalized and not empty) or len(normalized) > maximum:
        raise OwnerConflict(code)
    return normalized
