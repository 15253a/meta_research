# Institutional browser workflow

Load this reference only after applicable publisher-API and lawful OA routes fail and the affected paper needs an authenticated browser route.

## Reuse the authorized browser

Operate in the same controlled Chrome profile where the user completed institutional login. Reaching a library home page proves connectivity; reaching a subscribed publisher page that visibly identifies the institution, or retrieving a verified article body through that route, proves entitlement.

Use the configured library resource URL as the route origin. When that entry uses an `.ivpn.` host, the batch downloader rewrites DOI and publisher URLs into the same authenticated WebVPN namespace before browser transfer. Live portals, WebVPN-rewritten publisher hosts, resolver pages, and federation callbacks are more reliable than guessing from an institution name. A raw publisher Shibboleth/CARSI URL may require a separate login even while a WebVPN-rewritten publisher host is already authorized.

When Chrome exposes native CDP at `127.0.0.1:9222`, reuse it through `scripts/direct_cdp_proxy.mjs`; never launch a fresh profile for a run that depends on existing login state. Inspect visible page title, URL, access label, and article controls only. Keep cookies, credentials, local storage, saved passwords, and profile files outside agent context.

## Resolve one paper

1. Open the exact DOI/title result in the authenticated route.
2. Verify title or DOI before following a full-text control.
3. Prefer a publisher PDF or full-text HTML/XML control shown for that article.
4. Use the bundled batch route first. For one diagnosed browser response, `browser_pdf_downloader.mjs --help` and `browser_cdp_stream_downloader.mjs` expose the supported same-session transfer paths.
5. Validate the delivered article body under the delivery reference before returning it.

Process one or a few institutional tabs at a time. Close tabs created for completed or abandoned attempts.

## Authentication handoff

When the page reaches institutional SSO, CAS, CARSI/Shibboleth, OpenAthens, federation selection, database login, or WebVPN expiry, identify the visible page and pause the affected paper. Ask the user to complete login in that controlled browser, upload the article body, or abandon the paper. After the user confirms login, retry once from the same article route.

Pause immediately on CAPTCHA, QR login, SMS/OTP, push approval, password reset, security warnings, publisher bot challenges, or unclear consent. Do not type, read, copy, or submit identity-bearing secrets. A clearly credential-free institution selector may be chosen only when the user has authorized that exact choice.

## Terminal outcomes

- `obtained`: a verified PDF, HTML, or XML article body matches the requested paper.
- `institutional_login_required`: the user must restore the authenticated session.
- `library_no_permission`: the authorized route explicitly reports no institutional entitlement.
- `anti_bot_or_security_challenge`: manual user action is required; automation stops.
- `institutional_transfer_failed`: the authorized page was reached but no valid article body survived one focused retry.

Return the affected item promptly so unrelated OA, OpenAlex, and Reader work can continue.
