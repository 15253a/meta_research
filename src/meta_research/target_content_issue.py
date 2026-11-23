"""Agent-correctable Target declarations, classified where they are validated."""
from meta_research.owners.common import OwnerConflict


class TargetContentIssue(OwnerConflict):
    """A declaration error; host custody, evidence and receipt failures never use this type."""


def target_content_issue(
    code: str,
    feedback: str | None = None,
    *,
    verify_only: bool = False,
) -> OwnerConflict:
    """Return TargetContentIssue on live writes, or plain OwnerConflict during verify_only."""
    if verify_only:
        return OwnerConflict(code)
    conflict = TargetContentIssue(code)
    if feedback:
        conflict.feedback = feedback
    return conflict


def check_copied_target_identity(content: dict, field: str, expected: object, *,
                                 target_ref: str | None = None) -> None:
    """Omission inherits the accepted identity. Reject an explicit conflict."""
    if field in content and content[field] != expected:
        scope = f"Target {target_ref!r}" if target_ref is not None else "this Target"
        conflict = TargetContentIssue("target_root_commit_domain_invalid")
        conflict.feedback = (
            f"$.{field} supplied {content[field]!r}, but the accepted source "
            f"for {scope} records {expected!r}. Correct or omit that copied identity "
            "in the result document and finish another normal turn in this Root. "
            "The system carries the accepted identity; preserve the metrics, artifacts "
            "and actual work, which need no rerun for this conflict."
        )
        raise conflict
