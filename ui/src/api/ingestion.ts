import { apiClient } from "./client";

export async function triggerIngestion(data: {
  project_name: string;
  index_name: string;
  container_name: string;
  project_id: number;
  blob_prefix?: string;
  chunking_strategy?: string;
}): Promise<any> {
  return apiClient("/ingest", { method: "POST", body: JSON.stringify(data) });
}

export async function getAuditInfo(projectId: number): Promise<any[]> {
  return apiClient(`/audit/${projectId}`);
}
