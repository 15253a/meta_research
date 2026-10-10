WORK_MATERIAL_OPERATION_IDS = ("research_workspace.materials.discover", "research_workspace.materials.read")
CREATION_MATERIAL_OPERATION_IDS = (*WORK_MATERIAL_OPERATION_IDS,
    "research_workspace.materials.copy", "research_workspace.materials.copy.reconcile")
PROCESSING_OPERATION_IDS = (
    "research_workspace.materials.feedback", "research_workspace.materials.feedback.reconcile",
    "research_workspace.materials.acquire", "research_workspace.materials.acquire.reconcile",
)
RUNTIME_MATERIAL_OPERATION_IDS = (*CREATION_MATERIAL_OPERATION_IDS, *PROCESSING_OPERATION_IDS)
