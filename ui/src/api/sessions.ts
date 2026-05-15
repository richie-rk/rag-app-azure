import { apiClient } from "./client";
import type { Session } from "./types";

export async function getSessions(username: string): Promise<Session[]> {
  return apiClient(`/sessions?username=${encodeURIComponent(username)}`);
}

export async function getSessionMessages(sessionId: string): Promise<any[]> {
  return apiClient(`/sessions?session_id=${encodeURIComponent(sessionId)}`);
}

export async function saveSession(data: Record<string, string>): Promise<any> {
  return apiClient("/sessions", { method: "POST", body: JSON.stringify(data) });
}

export async function deleteSession(sessionId: string): Promise<any> {
  return apiClient(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}
