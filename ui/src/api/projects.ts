import { apiClient } from "./client";
import type { Project } from "./types";

export async function getProjects(userId?: number): Promise<Project[]> {
  const params = userId ? `?user_id=${userId}` : "";
  return apiClient(`/projects${params}`);
}

export async function createProject(data: Partial<Project>): Promise<any> {
  return apiClient("/projects", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateProject(
  projectId: number,
  data: Partial<Project>,
): Promise<any> {
  return apiClient(`/projects/${projectId}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}
