import assert from "node:assert/strict";
import test from "node:test";
import { writingMaterialLabel } from "../src/writingMaterialLabel.ts";

const artifact = () => ({
  display_name: "target-root-artifact-0007", version_ref: "accepted-v2", version_number: 2,
  content_hash: "a".repeat(64), source_kind: "file",
  receipt: { issuer: "research_memory", kind: "asset_acceptance", subject_ref: "accepted-v2" },
  provenance: { schema_ref: "meta-research/target-root-artifact-provenance/v1", ordinal: 7,
    artifact_kind: "file", declared_relative_path: "outputs/analysis/研究说明.md", content_hash: "a".repeat(64) },
});

test("Writing labels keep the complete accepted path and do not mutate an asset", () => {
  const item = artifact(), before = structuredClone(item);
  assert.equal(writingMaterialLabel(item), "outputs/analysis/研究说明.md");
  const sameBasename = artifact();
  sameBasename.provenance.declared_relative_path = "handoff/研究说明.md";
  assert.equal(writingMaterialLabel(sameBasename), "handoff/研究说明.md");
  assert.notEqual(writingMaterialLabel(item), writingMaterialLabel(sameBasename));
  assert.deepEqual(item, before);
});

test("matching directory artifacts can use their complete accepted path", () => {
  const item = artifact();
  item.source_kind = item.provenance.artifact_kind = "directory";
  item.provenance.declared_relative_path = "outputs/data";
  assert.equal(writingMaterialLabel(item), "outputs/data");
});

test("custom display names and names outside the generated format stay unchanged", () => {
  for (const name of ["人工命名研究说明", "target-root-artifact-007", "target-root-artifact-0007-custom"]) {
    const item = { ...artifact(), display_name: name };
    assert.equal(writingMaterialLabel(item), name);
  }
});

test("missing, malformed, unrelated or mismatched provenance keeps the existing label", () => {
  const item = artifact();
  for (const provenance of [undefined, null, [], "legacy", {},
    { ...item.provenance, schema_ref: "unrelated" },
    { ...item.provenance, ordinal: "7" }, { ...item.provenance, ordinal: 8 },
    { ...item.provenance, ordinal: 7.5 }, { ...item.provenance, artifact_kind: "text" },
    { ...item.provenance, artifact_kind: "directory" },
    { ...item.provenance, content_hash: "b".repeat(64) },
    { ...item.provenance, declared_relative_path: 7 }]) {
    assert.equal(writingMaterialLabel({ ...item, provenance }), item.display_name, JSON.stringify(provenance));
  }
});

test("a label hint must come from this version's RM acceptance", () => {
  const item = artifact();
  for (const receipt of [undefined, {}, { ...item.receipt, issuer: "other" },
    { ...item.receipt, kind: "pending" }, { ...item.receipt, subject_ref: "different-v3" }]) {
    assert.equal(writingMaterialLabel({ ...item, receipt }), item.display_name);
  }
});

test("absolute, traversing and malformed paths fall back without normalizing metadata", () => {
  const item = artifact();
  for (const path of ["", "  ", "/absolute/note.md", "C:/absolute/note.md", "outputs\\note.md",
    "../note.md", "outputs/../note.md", "outputs/./note.md", "outputs//note.md", "outputs/note.md\n"]) {
    const candidate = { ...item, provenance: { ...item.provenance, declared_relative_path: path } };
    assert.equal(writingMaterialLabel(candidate), item.display_name, JSON.stringify(path));
    assert.equal(candidate.provenance.declared_relative_path, path);
  }
});
