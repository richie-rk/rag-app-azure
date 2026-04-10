/**
 * Centralized fetch wrapper with auth token injection.
 * All endpoints from VITE_* env vars — no hardcoded URLs.
 */

const UTILS_API = import.meta.env.VITE_UTILS_API_URL || "/api";

interface FetchOptions extends RequestInit {
  token?: string;
}

export async function apiClient(
  path: string,
  options: FetchOptions = {},
): Promise<any> {
  const { token, headers: customHeaders, ...rest } = options;

  // Use stored token if not explicitly provided
  const authToken = token || localStorage.getItem("rag_auth_token") || "";

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
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
    throw new Error("Unauthorized");
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ message: response.statusText }));
    throw new Error(error.error || error.message || "Request failed");
  }

  // Handle binary responses (documents)
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    return response;
  }

  return response.json();
}
