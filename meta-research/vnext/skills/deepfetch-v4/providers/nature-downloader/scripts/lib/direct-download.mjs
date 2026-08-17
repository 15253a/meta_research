import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { classifyFullTextContent, isTransientHttpStatus } from "./provider-utils.mjs";
import { findRepositoryPdfCandidates, isRepositoryHintUrl } from "./repository-resolver.mjs";

export function safeArticleBasename(title = "", doi = "") {
  const base = String(title || doi || "article")
    .trim()
    .replace(/[\/:*?"<>|]+/g, "")
    .replace(/\s+/g, "_")
    .slice(0, 140);
  return base || "article";
}

export async function saveFullTextResponse(response, { outDir, title = "", doi = "", source = "" } = {}) {
  if (!response.ok) return { ok: false, httpStatus: response.status };
  const body = Buffer.from(await response.arrayBuffer());
  const contentType = response.headers.get("content-type") || "";
  const classification = classifyFullTextContent({ contentType, head: body.subarray(0, 65536) });
  if (!classification.valid) return { ok: false, httpStatus: response.status, contentType, ...classification };
  const folder = classification.format === "pdf" ? "PDFs" : "FullText";
  const file = path.join(outDir, folder, `${safeArticleBasename(title, doi)}${classification.extension}`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.part-${process.pid}`;
  fs.writeFileSync(temp, body);
  fs.renameSync(temp, file);
  return {
    ok: true,
    file,
    bytes: body.length,
    sha256: crypto.createHash("sha256").update(body).digest("hex"),
    contentType,
    format: classification.format,
    source: source || response.url || "",
  };
}

export async function fetchAndSaveFullText(url, {
  outDir,
  title = "",
  doi = "",
  source = url,
  fetchImpl = fetch,
  sleepImpl = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  maxAttempts = 2,
  repositoryFallback = true,
} = {}) {
  const transferAttempts = [];
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    let response;
    try {
      response = await fetchImpl(url, {
        headers: { Accept: "application/pdf, text/html, application/xml" },
        signal: AbortSignal.timeout(60000),
      });
    } catch (error) {
      transferAttempts.push({
        status: "request_failed",
        transfer_attempt: attempt,
        reason: String(error?.message || error).slice(0, 120),
      });
      if (attempt < maxAttempts) {
        await sleepImpl(250 * attempt);
        continue;
      }
      return { ok: false, reason: transferAttempts.at(-1).reason, transferAttempts };
    }
    if (isTransientHttpStatus(response.status) && attempt < maxAttempts) {
      transferAttempts.push({
        status: "transient_http_failure",
        transfer_attempt: attempt,
        http_status: response.status,
      });
      await sleepImpl(250 * attempt);
      continue;
    }
    const saved = await saveFullTextResponse(response, { outDir, title, doi, source });
    if (saved.ok) return { ...saved, transferAttempts };
    transferAttempts.push({
      status: "invalid_or_unavailable",
      transfer_attempt: attempt,
      http_status: saved.httpStatus,
      reason: saved.reason || "invalid content",
    });
    if (
      repositoryFallback
      && saved.reason === "not_fulltext_html"
      && title
      && isRepositoryHintUrl(url)
    ) {
      const candidates = await findRepositoryPdfCandidates(url, title, { fetchImpl }).catch(() => []);
      for (const candidate of candidates) {
        const recovered = await fetchAndSaveFullText(candidate.url, {
          outDir,
          title,
          doi,
          source: candidate.url,
          fetchImpl,
          sleepImpl,
          maxAttempts,
          repositoryFallback: false,
        });
        transferAttempts.push(...recovered.transferAttempts.map((item) => ({
          repository_resolver: candidate.resolver,
          url: candidate.url,
          ...item,
        })));
        if (recovered.ok) {
          return {
            ...recovered,
            transferAttempts,
            repositoryResolver: candidate.resolver,
            repositorySource: url,
          };
        }
      }
    }
    return { ...saved, transferAttempts };
  }
  return { ok: false, reason: "request failed", transferAttempts };
}
