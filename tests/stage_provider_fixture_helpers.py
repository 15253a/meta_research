"""Real primary guidance preparation for controlled Stage provider fixtures."""

from meta_research.human_guidance import GuidanceRuntimeScope, StageGuidanceOperation
from meta_research.owners.common import canonical_hash


class ReasoningPrimaryGuidance:
    _human_guidance_authority = None

    def bind_human_guidance_authority(self, authority) -> None:
        self._human_guidance_authority = authority

    def _freeze_primary_guidance(self, request) -> None:
        # Composition binds standalone providers. A real transport can instead
        # use a fixture only as a document generator and prepare its own cut.
        if self._human_guidance_authority is None:
            return
        self._human_guidance_authority.freeze_operation_guidance(
            StageGuidanceOperation(
                GuidanceRuntimeScope(
                    "reasoning", request.run_ref, request.attempt_ref,
                    request.root_session_ref, request.fence_ref,
                    canonical_hash(request.runtime_binding.as_dict()),
                ),
                request.job_ref, "primary",
            )
        )
