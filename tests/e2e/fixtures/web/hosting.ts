// The response headers of the site, word for word from 10-web-client.md sections 3.2 to 3.5, for
// the hosting headers browser test (T-347) and the deployed smoke tests (T-348).

export const PERMISSIONS_POLICY =
  "accelerometer=(), autoplay=(), camera=(), clipboard-read=(), clipboard-write=(self), " +
  "display-capture=(), encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), hid=(), " +
  "idle-detection=(), magnetometer=(), microphone=(), midi=(), payment=(), picture-in-picture=(), " +
  "publickey-credentials-get=(), screen-wake-lock=(), serial=(), usb=(), web-share=(), " +
  "xr-spatial-tracking=()";

export function contentSecurityPolicy(apiOrigin: string): string {
  return (
    "default-src 'none'; script-src 'self'; " +
    "style-src 'self' 'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='; " +
    "img-src 'self' data: blob:; font-src 'self'; " +
    `connect-src 'self' ${apiOrigin} https://login.microsoftonline.com; ` +
    "frame-src blob:; object-src 'none'; base-uri 'self'; form-action 'none'; " +
    "frame-ancestors 'none'"
  );
}

export const ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable";

/** The global headers every response carries (section 3.2), lower-cased names. */
export function globalHeaders(apiOrigin: string): Readonly<Record<string, string>> {
  return {
    "content-security-policy": contentSecurityPolicy(apiOrigin),
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": PERMISSIONS_POLICY,
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-robots-tag": "noindex, nofollow",
  };
}

/** Each way `headers` (lower-cased names) differs from what `path` must carry. */
export function headerMismatches(
  path: string,
  headers: Readonly<Record<string, string>>,
  apiOrigin: string,
): string[] {
  const expected: Record<string, string> = {
    ...globalHeaders(apiOrigin),
    "cache-control": path.startsWith("/assets/") ? ASSET_CACHE_CONTROL : "no-store",
  };
  const mismatches = Object.entries(expected)
    .filter(([name, value]) => headers[name] !== value)
    .map(([name, value]) => `${path}: ${name} is ${String(headers[name])}, not ${value}`);
  // Violation reports would be client telemetry (section 3.4), so no reporting is configured.
  for (const name of ["report-to", "reporting-endpoints"]) {
    if (headers[name] !== undefined) mismatches.push(`${path}: carries ${name}`);
  }
  const policy = headers["content-security-policy"] ?? "";
  if (/report-uri|report-to/.test(policy)) mismatches.push(`${path}: CSP names a report endpoint`);
  return mismatches;
}

/** The paths that answer `404` with the 404 page, the `.auth` ones with no redirect or cookie. */
export const BLOCKED_PATHS = ["/.auth/login/aad", "/.auth/login/github", "/.auth/me"] as const;

export const NOT_FOUND_TITLE = "Page not found · DocChat";
