"""Agent-correctable Target declarations, classified where they are validated."""
from meta_research.owners.common import OwnerConflict


class TargetContentIssue(OwnerConflict):
    """A declaration error; host custody, evidence and receipt failures never use this type."""
