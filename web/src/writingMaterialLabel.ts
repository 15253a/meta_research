import type { ResearchAssetItem } from "./api";

// A display hint for Writing only; accepted version bindings are never changed.
export function writingMaterialLabel(item: ResearchAssetItem): string {
  const fallback = item.display_name;
  if (!/^target-root-artifact-\d{4,}$/.test(fallback)) return fallback;
  const provenance = item.provenance;
  if (!provenance || typeof provenance !== "object" || Array.isArray(provenance)
    || provenance.schema_ref !== "meta-research/target-root-artifact-provenance/v1"
    || item.receipt?.issuer !== "research_memory" || item.receipt.kind !== "asset_acceptance"
    || item.receipt.subject_ref !== item.version_ref) return fallback;
  const { ordinal, artifact_kind: kind, declared_relative_path: path } = provenance;
  if (typeof ordinal !== "number" || !Number.isSafeInteger(ordinal) || ordinal < 0
    || fallback !== `target-root-artifact-${String(ordinal).padStart(4, "0")}`
    || (kind !== "file" && kind !== "directory") || item.source_kind !== kind
    || provenance.content_hash !== item.content_hash) return fallback;
  if (typeof path !== "string" || !path.trim() || path.includes("\\")
    || /^[A-Za-z]:/.test(path) || /[\u0000-\u001f\u007f]/.test(path)
    || path.split("/").some(part => !part || part === "." || part === "..")) return fallback;
  return path;
}
