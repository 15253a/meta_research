import { exactTitleMatch } from "./open-access.mjs";

const JSON_HEADERS = { Accept: "application/hal+json, application/json" };

export function isRepositoryHintUrl(value = "") {
  try {
    const url = new URL(value);
    return /(?:^|\.)ir\.|repository|scholaris|scholarship|eprints|\/(?:etd|handle|items|collections)\//i
      .test(`${url.hostname}${url.pathname}`);
  } catch (_) {
    return false;
  }
}

async function fetchJson(url, fetchImpl) {
  const response = await fetchImpl(url, {
    headers: JSON_HEADERS,
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) return null;
  return response.json();
}

function absoluteLinks(html, baseUrl) {
  const links = [];
  for (const match of String(html).matchAll(/\bhref=["']([^"']+)["']/gi)) {
    try {
      links.push(new URL(match[1].replace(/&amp;/g, "&"), baseUrl).toString());
    } catch (_) {}
  }
  return [...new Set(links)];
}

function embeddedList(value, key) {
  return value?._embedded?.[key] || [];
}

function bitstreamIsPdf(bitstream) {
  const mime = bitstream?.metadata?.["dc.format.mimetype"]?.[0]?.value || "";
  return /\.pdf$/i.test(bitstream?.name || "") || /^application\/pdf\b/i.test(mime);
}

async function dspaceCandidates(origin, title, fetchImpl) {
  const search = new URL("/server/api/discover/search/objects", origin);
  search.searchParams.set("query", title);
  search.searchParams.set("size", "5");
  const result = await fetchJson(search, fetchImpl).catch(() => null);
  const objects = result?._embedded?.searchResult?._embedded?.objects || [];
  const item = objects
    .map((entry) => entry?._embedded?.indexableObject || entry)
    .find((entry) => exactTitleMatch(entry?.name || "", title));
  if (!item) return [];

  const itemId = item.uuid || item.id || "";
  const bundlesUrl = item?._links?.bundles?.href
    || (itemId ? new URL(`/server/api/core/items/${itemId}/bundles`, origin).toString() : "");
  if (!bundlesUrl) return [];
  const bundles = embeddedList(await fetchJson(bundlesUrl, fetchImpl).catch(() => null), "bundles");
  const original = bundles.find((bundle) => String(bundle?.name).toUpperCase() === "ORIGINAL");
  const bitstreamsUrl = original?._links?.bitstreams?.href || "";
  if (!bitstreamsUrl) return [];
  const bitstreams = embeddedList(
    await fetchJson(bitstreamsUrl, fetchImpl).catch(() => null),
    "bitstreams"
  );
  return bitstreams
    .filter(bitstreamIsPdf)
    .map((bitstream) => bitstream?._links?.content?.href || "")
    .filter(Boolean)
    .map((url) => ({ url, resolver: "dspace_exact_title" }));
}

export async function findRepositoryPdfCandidates(sourceUrl, title, { fetchImpl = fetch } = {}) {
  if (!sourceUrl || !title) return [];
  const response = await fetchImpl(sourceUrl, {
    headers: { Accept: "text/html" },
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) return [];
  const html = await response.text();
  const baseUrl = response.url || sourceUrl;
  const links = absoluteLinks(html, baseUrl);
  const candidates = links
    .filter((url) => /\.pdf(?:[?#]|$)|\/bitstreams\/[^/]+\/content(?:[?#]|$)/i.test(url))
    .map((url) => ({ url, resolver: "repository_link" }));

  const repositoryOrigins = new Set([new URL(baseUrl).origin]);
  for (const link of links) {
    if (/\/(?:collections|items|handle)\//i.test(new URL(link).pathname)) {
      repositoryOrigins.add(new URL(link).origin);
    }
  }
  for (const origin of [...repositoryOrigins].slice(0, 4)) {
    candidates.push(...await dspaceCandidates(origin, title, fetchImpl));
  }
  return [...new Map(candidates.map((candidate) => [candidate.url, candidate])).values()];
}
