/**
 * Centralized fetch wrapper with auth token injection.
 * Endpoint base URLs come from VITE_* env vars, never hardcoded.
 */

const UTILS_API = import.meta.env.VITE_UTILS_API_URL || "/api";

/** Error carrying the HTTP status and the backend's error `code`, when present. */
export class ApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

interface FetchOptions extends RequestInit {
  token?: string;
}

/**
 * Holder for the SSO token getter. AuthProvider registers a callback on
 * mount so apiClient can resolve the SSO bearer from React state without
 * every caller passing it explicitly. Magic-link users keep working through
 * the localStorage fallback below.
 */
let tokenGetter: (() => string | null) | null = null;

export function setTokenGetter(
  getter: (() => string | null) | null,
): void {
  tokenGetter = getter;
}

export async function apiClient(
  path: string,
  options: FetchOptions = {},
): Promise<any> {
  const { token, headers: customHeaders, ...rest } = options;

  // Token resolution order: explicit caller-provided, then the SSO getter
  // (React state), then magic-link's localStorage entry, then empty.
  const authToken =
    token ||
    tokenGetter?.() ||
    localStorage.getItem("rag_auth_token") ||
    "";

  // A FormData body sets its own multipart Content-Type (with boundary); only
  // force JSON otherwise.
  const isFormData = rest.body instanceof FormData;
  const headers: Record<string, string> = {
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(customHeaders as Record<string, string>),
  };

  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const url = path.startsWith("http") ? path : `${UTILS_API}${path}`;
  const response = await fetch(url, { ...rest, headers });

  if (response.status === 401) {
    localStorage.removeItem("rag_auth_token");
    window.location.href = "/login";
    throw new ApiError("Unauthorized", 401);
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: response.statusText }));
    throw new ApiError(
      error.error || error.message || "Request failed",
      response.status,
      error.code,
    );
  }

  // Handle binary responses (documents)
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    return response;
  }

  return response.json();
}
