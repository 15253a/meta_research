import fs from "node:fs";
import path from "node:path";

const SECRET_KEY_RE = /(?:api[_-]?key|secret|password|passwd|cookie|session|token|authorization|credential|signature|awsaccesskeyid|client[_-]?assertion|code[_-]?verifier)/i;
const SECRET_QUERY_RE = /([?&#](?:api[_-]?key|apikey|secret|password|passwd|auth[_-]?token|insttoken|access[_-]?token|authorization|token|session|sessionid|jsessionid|ticket|cookie|otp|samlresponse|samlart|code|credential|signature|sig|awsaccesskeyid|x-amz-credential|x-amz-signature|client[_-]?assertion|code[_-]?verifier)=)[^&#\s"']*/gi;
const URL_USERINFO_RE = /\b([a-z][a-z0-9+.-]*:\/\/)[^/@\s]+@/gi;
const SENSITIVE_HEADER_RE = /(\b(?:authorization|proxy-authorization|cookie|set-cookie|api-key|x-api-key|x-auth-token|x-access-token|x-amz-security-token|x-csrf-token)\s*:\s*)[^\r\n]*/gi;
const SECRET_ASSIGNMENT_RE = /(\b(?:[a-z0-9_-]*(?:secret|token|password|passwd|cookie|session[_-]?id|private[_-]?key|api[_-]?key|credential|signature)|session|ticket|otp|samlresponse|samlart|sig|awsaccesskeyid|client[_-]?assertion|code[_-]?verifier)\b\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^&#\s,;]+)/gi;

function redactString(value) {
  return value
    .replace(URL_USERINFO_RE, "$1[REDACTED]@")
    .replace(SECRET_QUERY_RE, "$1[REDACTED]")
    .replace(SENSITIVE_HEADER_RE, "$1[REDACTED]")
    .replace(/\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]")
    .replace(SECRET_ASSIGNMENT_RE, "$1[REDACTED]");
}

function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (typeof value === "string") return redactString(value);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !SECRET_KEY_RE.test(key))
      .map(([key, child]) => [key, redact(child)])
  );
}

export function writeManifest(outDir, manifest) {
  fs.mkdirSync(outDir, { recursive: true });
  const file = path.join(outDir, "manifest.json");
  const temp = `${file}.tmp-${process.pid}`;
  const payload = {
    version: 2,
    generated_at: new Date().toISOString(),
    ...redact(manifest),
  };
  fs.writeFileSync(temp, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(temp, file);
  return file;
}
