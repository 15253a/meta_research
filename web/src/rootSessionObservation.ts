import type { RootSession, RootSessions } from "./rootSessionsApi";

const providerReasonKinds = new Map<string, RootSession["kind"]>([
  ["deepfetch_root_operation_unavailable", "deepfetch"],
  ["deepfetch_root_observation_unavailable", "deepfetch"],
  ["acquisition_root_operation_unavailable", "acquisition"],
  ["acquisition_root_observation_unavailable", "acquisition"],
  ["acquisition_session_chain_invalid", "acquisition"],
]);

/** Completeness of the whole index is separate from a particular observed state. */
export function rootSessionStatusUncertain(catalog: RootSessions | null | undefined, session: RootSession | null | undefined): boolean {
  if (!catalog || !session || typeof catalog.limited !== "boolean"
    || !catalog.sessions.some(item => item.session_ref === session.session_ref)
    || session.limited !== undefined && typeof session.limited !== "boolean") return true;
  const nativeRoot = session.kind === "stage" || session.kind === "target";
  // Native observers verify the bound live process for each executing operation.
  // Missing older operations or child observations do not undo that positive fact.
  const executionObserved = nativeRoot && session.status === "executing" && session.is_executing === true
    && catalog.active_session_refs.includes(session.session_ref)
    && session.operations.some(operation => operation.status === "executing");
  if (session.limited === true && !executionObserved) return true;
  if (!catalog.limited) return false;
  if (!Array.isArray(catalog.reasons) || catalog.reasons.length === 0) return true;
  return catalog.reasons.some(reason => {
    if (!reason || typeof reason !== "object" || Array.isArray(reason)) return true;
    const { code, session_ref: reasonSession } = reason as Record<string, unknown>;
    if (typeof code !== "string" || reasonSession !== undefined && (typeof reasonSession !== "string" || !reasonSession)) return true;
    const providerKind = providerReasonKinds.get(code);
    // Unknown/index-wide failures cannot be narrowed by guessing from a name.
    if (!providerKind && code !== "root_session_operations_limited") return true;
    if (reasonSession !== undefined && reasonSession !== session.session_ref) return false;
    if (providerKind) return session.kind === providerKind;
    // The native producer emits this aggregate code for per-session `limited`.
    // Provider roots do not expose that flag, so their missing history stays unknown.
    return !(executionObserved || nativeRoot && reasonSession === undefined && session.limited === false);
  });
}
