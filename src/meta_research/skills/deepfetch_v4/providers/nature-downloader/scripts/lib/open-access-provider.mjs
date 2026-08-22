import { fetchAndSaveFullText } from "./direct-download.mjs";
import {
  findPmcCandidates,
  fetchSemanticScholarRecord,
  fetchUnpaywallRecord,
  parseSemanticScholarRecord,
  parseUnpaywallRecord,
  rankOaCandidates,
} from "./open-access.mjs";
import { STATUS } from "./status-codes.mjs";

function httpsForFtp(url = "") {
  return String(url).replace(/^ftp:\/\/ftp\.ncbi\.nlm\.nih\.gov\//i, "https://ftp.ncbi.nlm.nih.gov/");
}

function isOpenAccessLicense(value = "") {
  return /creativecommons\.org\/(?:licenses|publicdomain)\//i.test(String(value));
}

async function tryCandidates(article, candidates, {
  outDir,
  fetchImpl,
  sleepImpl,
  maxTransferAttempts = 2,
}) {
  const attempts = [];
  for (const candidate of rankOaCandidates(candidates)) {
    const url = httpsForFtp(candidate.url);
    const saved = await fetchAndSaveFullText(url, {
      outDir,
      title: article.title,
      doi: article.doi,
      source: url,
      fetchImpl,
      sleepImpl,
      maxAttempts: maxTransferAttempts,
    });
    attempts.push(...saved.transferAttempts.map((item) => ({
      source: candidate.source,
      url,
      ...item,
    })));
    if (!saved.ok) continue;
    return { downloaded: {
      status: saved.format === "html"
        ? STATUS.FULL_TEXT_HTML_AVAILABLE
        : saved.format === "pdf"
          ? STATUS.OPEN_ACCESS_DOWNLOADED
          : STATUS.NATIVE_FULLTEXT_DOWNLOADED,
      provider: "open_access",
      accessMode: "open_access",
      oaEvidence: candidate,
      ...saved,
      oaAttempts: attempts,
    }, attempts };
  }
  return { downloaded: null, attempts };
}

async function findPmcidByDoi(doi, fetchImpl) {
  if (!doi) return { pmcid: "", checked: false };
  const url = new URL("https://www.ebi.ac.uk/europepmc/webservices/rest/search");
  url.searchParams.set("query", `DOI:${doi}`);
  url.searchParams.set("format", "json");
  const response = await fetchImpl(url, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(30000) });
  if (!response.ok) return { pmcid: "", checked: false, error: `Europe PMC HTTP ${response.status}` };
  const record = (await response.json())?.resultList?.result?.[0];
  return { pmcid: record?.pmcid || "", checked: true };
}

export async function downloadOpenAccessArticle(article, {
  email = "",
  outDir,
  fetchImpl = fetch,
  sleepImpl = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
  const pmcLookup = article.pmcid
    ? { pmcid: article.pmcid, checked: true }
    : await findPmcidByDoi(article.doi, fetchImpl).catch((error) => ({ pmcid: "", checked: false, error: String(error?.message || error).slice(0, 120) }));
  const pmcid = pmcLookup.pmcid;
  const allAttempts = [];
  let candidateCount = 0;
  if (pmcid) {
    const pmc = await findPmcCandidates(pmcid, { fetchImpl }).catch(() => []);
    candidateCount += pmc.length;
    const attempt = await tryCandidates(article, pmc, { outDir, fetchImpl, sleepImpl });
    allAttempts.push(...attempt.attempts);
    if (attempt.downloaded) return attempt.downloaded;
  }

  let unpaywallRecord = null;
  let unpaywallChecked = false;
  let unpaywallError = "";
  if (article.doi && email) {
    try {
      unpaywallRecord = await fetchUnpaywallRecord(article.doi, email, { fetchImpl, sleepImpl });
      unpaywallChecked = true;
    } catch (error) {
      unpaywallError = String(error?.message || error).slice(0, 120);
    }
  }
  const unpaywall = parseUnpaywallRecord(unpaywallRecord);
  let semanticScholarRecord = null;
  let semanticScholarChecked = false;
  let semanticScholarError = "";
  if (article.doi) {
    try {
      semanticScholarRecord = await fetchSemanticScholarRecord(article.doi, { fetchImpl, sleepImpl });
      semanticScholarChecked = true;
    } catch (error) {
      semanticScholarError = String(error?.message || error).slice(0, 120);
    }
  }
  const semanticScholar = parseSemanticScholarRecord(semanticScholarRecord);
  const crossrefLicenseIsOa = isOpenAccessLicense(article.license);
  const publisher = article.publisherPdfUrl && crossrefLicenseIsOa
    ? [{ source: "publisher_oa", url: article.publisherPdfUrl, format: "pdf", version: "publishedVersion", license: article.license }]
    : [];
  const remainingCandidates = [...unpaywall, ...semanticScholar, ...publisher];
  candidateCount += remainingCandidates.length;
  const attempt = await tryCandidates(article, remainingCandidates, { outDir, fetchImpl, sleepImpl });
  allAttempts.push(...attempt.attempts);
  if (attempt.downloaded) return attempt.downloaded;
  const confirmedOa = Boolean(
    pmcid || unpaywallRecord?.is_oa || semanticScholarRecord?.isOpenAccess || crossrefLicenseIsOa
  );
  const confirmedClosed = Boolean(
    unpaywallChecked && unpaywallRecord?.is_oa === false
    && semanticScholarChecked && semanticScholarRecord?.isOpenAccess === false
  );
  return {
    status: candidateCount > 0 ? STATUS.FAILED_AFTER_RETRY : STATUS.OA_NOT_FOUND,
    provider: "open_access",
    doi: article.doi,
    title: article.title,
    oaAssessment: confirmedOa ? "confirmed_oa" : confirmedClosed ? "confirmed_closed" : "unknown",
    oaChecks: {
      pmc: { checked: pmcLookup.checked, pmcid: pmcid || "", error: pmcLookup.error || "" },
      unpaywall: { checked: unpaywallChecked, error: unpaywallError, is_oa: unpaywallRecord?.is_oa ?? null },
      semantic_scholar: {
        checked: semanticScholarChecked,
        error: semanticScholarError,
        is_oa: semanticScholarRecord?.isOpenAccess ?? null,
      },
      crossref_license: article.license || "",
    },
    oaAttempts: allAttempts,
    ...(candidateCount > 0 ? { reason: "lawful OA candidates were found but no transfer passed validation" } : {}),
  };
}
