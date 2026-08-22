# Delivery verification and failures

Load this reference when a transfer needs content validation, quarantine, diagnosis, retry, or a typed terminal status. Bound DeepFetch requests always pass `--no-si`; Supporting Information is outside this provider contract.

## Verify identity and content

An `obtained` result satisfies all applicable checks:

1. requested DOI or normalized title matches metadata and the delivered article;
2. the response is the article body rather than a landing page, login page, denial, consent screen, abstract-only page, or security challenge;
3. PDF begins with valid PDF content and parses as a non-trivial document; HTML/XML contains substantive article sections and identifying metadata;
4. the file path, MIME/format, byte count, and SHA-256 agree with `manifest.json`;
5. the file is inside the caller-supplied private acquisition directory.

CAJ, Supporting Information alone, a publisher landing page, and an unrelated paper are not DeepFetch full text. Preserve any questionable transfer privately for diagnosis; only verified PDF, HTML, or XML may be registered into the public ledger.

## Retry narrowly

Retry a transient network, incomplete transfer, or expiring signed URL once through the same lawful route. If direct shell transfer returns a login/denial page while the authenticated browser visibly holds access, use the same-browser transfer path under the institutional workflow. Stop route retries when identity mismatches, entitlement is denied, authentication expires, or a security challenge appears.

## Typed failures

Use the most specific observable code:

- `metadata_not_found`: DOI/title could not be resolved confidently.
- `oa_fulltext_not_found`: lawful OA checks found no article body.
- `institutional_login_required`: the authenticated session is absent or expired.
- `library_no_permission`: the institution explicitly lacks full-text entitlement.
- `anti_bot_or_security_challenge`: CAPTCHA, QR, OTP, bot check, or security prompt blocks automation.
- `no_authorized_pdf_found`: a PDF-only route, including bound CNKI, yielded no valid PDF.
- `full_text_html_available`: a verified HTML article exists where no PDF is available; return it as obtained HTML.
- `file_validation_failed`: bytes, MIME, parse, or substantive-content checks failed.
- `paper_mismatch`: the delivered body belongs to another work.
- `transfer_failed`: a verified route still failed after one focused retry.

Failure detail is concise, observable, and free of credentials, cookies, tokens, signed query strings, or browser state. Missing full text is not evidence that the paper is unimportant or false.

## Compact result

For success, return `paper_id`, `status=obtained`, absolute private path, `format=pdf|html|xml`, and `failure=null`. For failure, return `status=missing`, null file fields, and `{ "code": "...", "detail": "..." }`. The provider manifest remains private; DeepFetch records only material public limitations and registers verified files through `papers.py`.
