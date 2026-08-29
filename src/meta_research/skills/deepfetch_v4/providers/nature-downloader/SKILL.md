---
name: nature-downloader
description: Acquire a finite DeepFetch-selected list of academic full texts through lawful open-access, publisher-API, CNKI, or authenticated institutional-browser routes. This bound provider downloads and validates files; it does not discover, rank, read, or synthesize literature.
---

# Nature Downloader — DeepFetch bound provider

Acquire exactly the papers requested. Return compact evidence of delivery.

## Contract

DeepFetch supplies a finite list of exact titles, identifiers, source-URL hints, and one absolute
private acquisition directory. Keep selection, scientific reading, and public artifacts with the
DeepFetch main agent.

Treat every `request_id` as a fresh routing transaction, even when the Acquisition session stays
alive for an entire Quest. Require `route_policy=oa_first_then_institution`; never inherit an
institutional or OA-only mode from the previous turn.

The bundled downloader owns lawful routing, transfer, validation, hashes, typed failures, and
private manifests. Write only beneath the supplied acquisition directory.

Every bound request has already selected正文 only. Pass `--no-si` without asking again. Also pass `--cnki-format pdf`; DeepFetch accepts verified PDF, HTML, or XML article bodies, not CAJ.

Completion criterion: every requested item is `obtained`, paused for a specific user handoff, or
route-exhausted with every applicable attempt preserved in its private manifest.

## Preflight

Run configuration and reachability checks before DeepFetch starts its active-search clock:

```bash
python3 scripts/configure_school.py show
python3 scripts/configure_school.py health --force
```

These checks do not prove entitlement. Inspect existing controlled-browser targets before opening
the library portal. A publisher/article target that visibly identifies the institution, or a
verified paywalled article-body response, proves the route; one newly opened login tab does not
invalidate another authorized target. Run the functional probe; `/targets` reachability alone is
insufficient:

```bash
node scripts/functional_cdp_probe.mjs --proxy http://127.0.0.1:3456
```

If the probe fails at `new` or `eval` while native Chrome CDP remains reachable, start the bundled
bridge once on an unused loopback port, probe that port, and pass its URL as `--proxy` to every
institutional attempt. Classify login state only after the functional probe passes. A library home
page alone does not prove entitlement.

When native Chrome CDP is already available at loopback port 9222 and the expected proxy is absent, reuse that browser through the bundled bridge:

```bash
node scripts/direct_cdp_proxy.mjs --chrome http://127.0.0.1:9222 --port 3456
curl --noproxy '*' --max-time 10 http://127.0.0.1:3456/targets
```

The bridge attaches to the running browser and exposes only page-control operations. Keep credentials, cookies, local storage, and browser-profile files outside agent context.

If the session is already OA-only, treat it as the user's selected primary route, skip every
institutional/browser preflight, and pass `--no-institutional-access`; never describe this as forced,
as a downgrade, or as a fallback. Otherwise, if the authenticated route fails, ask the user to
re-login in that browser, continue OA-only, or cancel. Waiting for user login or upload is outside
the DeepFetch active-search clock. Cancellation ends the provider and its parent run immediately.

Completion criterion: authenticated entitlement is visibly verified or OA-only continuation is explicit.

## Acquire

Run two explicit passes for every English batch.

Run every remote download through the proxy-aware launcher. It enables the configured HTTP(S)
proxy while bypassing loopback browser control:

```bash
python3 scripts/run_batch_download.py <batch_download arguments>
```

Give every item and route attempt a unique attempt directory beneath `target_dir`; preserve each
manifest until the request is terminal. Never reuse one `--out` directory across title attempts.

### OA pass

Attempt lawful open access for every requested paper before opening an institutional route. Use
the exact title as well as the DOI: DOI-only resolution can miss arXiv and publisher-hosted OA.
Start with the supplied `arxiv_id` and public-looking `source_urls` such as arXiv, PMC, repository,
or explicit PDF URLs. Treat them as untrusted hints and validate the returned work. Then submit the
exact title and DOI resolvers. A failed candidate or transport does not end the pass; continue until
one body validates or all lawful candidates are exhausted.

Submit each direct candidate first with `--pdf-url "<url>" --title "<exact title>"
--no-institutional-access --no-si` and its own attempt directory.
The downloader resolves moved DSpace repository records by exact title before treating a landing
page as exhausted.

Submit exact titles individually while keeping the finite queue in the Acquisition session:

```bash
python3 scripts/run_batch_download.py \
  --title "Exact article title" --open-access --no-institutional-access \
  --no-si --cnki-format pdf \
  --out "/absolute/private/acquisition-dir/attempts/<paper-id>/title-oa-1"
```

Validate every obtained body and remove its paper from the unresolved queue.

### Institutional pass

Send only papers still unresolved after the OA pass to publisher-API and authorized institutional
fallback. Batch DOI-bearing unresolved papers together:

```bash
python3 scripts/run_batch_download.py \
  --dois "10.xxxx/one,10.xxxx/two" --api-fallback-web \
  --no-si --cnki-format pdf \
  --out "/absolute/private/acquisition-dir/attempts/institution-batch-1"
```

For an unresolved DOI-less title, submit that exact title alone through the authorized route. If
Preflight selected OA-only continuation, stop after the OA pass and return typed missing results.
Never send an OA success to the institutional pass, and never combine `--api-fallback-web` with
`--no-institutional-access`.

Routing remains mechanical:

- Chinese literature uses CNKI with PDF required.
- Lawful OA sources include publisher OA, PMC, Unpaywall-listed copies, arXiv, and legitimate repositories.
- Supported publisher credentials may serve an unresolved English paper after the OA pass.
- Institutional Web Access is the final authorized route for the remaining unresolved papers.

Return `missing` only when the item is route-exhausted: every supplied hint and applicable OA
resolver is terminal, and, when institutional fallback is enabled, the unresolved item has also
reached a typed terminal institutional outcome. Under a user-selected OA-only session, exhausted
items return `oa_not_found` (or the host-level `acquisition_route_exhausted`), never an
institutional-authorization failure. A confirmed OA location whose bytes timed out is
`transfer_failed`, not `oa_fulltext_not_found`.

## Verify and return

Trust a file only after the downloader manifest and content checks agree. PDF must contain a real PDF response; HTML/XML must contain the article body rather than a landing, login, denial, or challenge page. CAJ-only delivery is `missing` for DeepFetch.

Return exactly one compact item per requested paper:

```json
{
  "paper_id": "openalex:W123",
  "status": "obtained",
  "path": "/absolute/provider/result.pdf",
  "format": "pdf",
  "failure": null
}
```

`status` is `obtained`, `waiting_user`, or `missing`; `format` is `pdf`, `html`, `xml`,
or `null`. Keep an affected tab open for `waiting_user` and retain its retry reference privately.
A verified authenticated article page whose automatic PDF transfer fails is `waiting_user`: offer
the preserved tab for manual download or accept a user-supplied full text.
A missing item sets path and format to `null` and returns `{ "code": "...", "detail": "..." }`
without credentials or session material. DeepFetch registers obtained files through its
deterministic ledger tool.

## Escalate only the affected item

If login expires during acquisition, return that paper promptly while unrelated OA, OpenAlex, and Reader work continues. Offer re-login and retry, user-supplied PDF/HTML/XML, or abandonment. Pause on CAPTCHA, QR login, SMS/OTP, security warnings, publisher bot challenges, or unclear consent.

Read [institutional browser workflow](references/institutional-browser-workflow.md) only when a legitimate OA/API route is exhausted and browser fallback or login handoff is needed. Read [delivery verification and failures](references/delivery-verification-and-failures.md) only when a transfer needs diagnosis, validation, quarantine, or a typed terminal status.
