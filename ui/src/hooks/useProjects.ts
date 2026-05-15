import { useCallback, useEffect, useState } from "react";
import { getProjects } from "../api/projects";
import type { Project } from "../api/types";

export function useProjects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getProjects();
      setProjects(data);
      if (!selectedProject && data.length > 0) {
        setSelectedProject(data[0]);
      }
    } catch (err) {
      console.error("Failed to load projects:", err);
    } finally {
      setLoading(false);
    }
  }, [selectedProject]);

  useEffect(() => {
    loadProjects();
  }, []);

  return {
    projects,
    selectedProject,
    setSelectedProject,
    loading,
    refresh: loadProjects,
  };
}
