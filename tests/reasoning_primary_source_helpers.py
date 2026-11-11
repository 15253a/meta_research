def record_reasoning_primary(owner, run, **values):
    invocation = run.primary_invocation
    owner.begin_provider_unit(
        unit_ref=invocation.invocation_ref, operation_ref=invocation.operation_ref,
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        unit_kind="reasoning_primary",
    )
    try:
        return owner.record_reasoning_primary_draft(
            primary_provider_unit_ref=invocation.invocation_ref, **values)
    finally:
        owner.acknowledge_provider_safe_point(unit_ref=invocation.invocation_ref,
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref)
