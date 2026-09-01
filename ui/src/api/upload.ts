import { apiClient } from "./client";

export interface UploadResult {
  file_name: string;
  blob_name: string;
  blob_prefix: string;
  container: string;
}

/** Upload one file into the project's prefix in the shared blob container. */
export async function uploadDocument(
  file: File,
  projectId: number,
): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("project_id", String(projectId));
  return apiClient("/upload", { method: "POST", body: form });
}
