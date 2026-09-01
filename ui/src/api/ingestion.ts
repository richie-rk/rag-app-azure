import { apiClient } from "./client";

/**
 * The ingestion Durable Function App is a separate host from the utils app,
 * so /ingest must resolve against VITE_INGESTION_API_URL (set by
 * infra/configure-settings.sh), not the default utils base. Everything the
 * server needs beyond project_id (index name, container, prefix, chunking
 * strategy) is derived server-side from the project row.
 */
const INGESTION_API = import.meta.env.VITE_INGESTION_API_URL || "";

export async function triggerIngestion(data: { project_id: number }): Promise<any> {
  if (!INGESTION_API) {
    throw new Error(
      "VITE_INGESTION_API_URL is not configured; cannot reach the ingestion service.",
    );
  }
  // Absolute URL: apiClient passes through URLs that start with "http".
  return apiClient(`${INGESTION_API}/ingest`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function getAuditInfo(projectId: number): Promise<any[]> {
  return apiClient(`/audit/${projectId}`);
}
