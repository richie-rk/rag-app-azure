import { useCallback, useEffect, useState } from "react";
import {
  makeStyles,
  Text,
  Badge,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Spinner,
} from "@fluentui/react-components";
import { FileUpload } from "../components/FileUpload";
import { ProjectSelector } from "../components/ProjectSelector";
import { useProjects } from "../hooks/useProjects";
import { triggerIngestion, getAuditInfo } from "../api/ingestion";

const useStyles = makeStyles({
  root: { padding: "24px", maxWidth: "960px", margin: "0 auto" },
  section: { marginTop: "24px" },
  toolbar: { display: "flex", gap: "12px", alignItems: "center", marginBottom: "16px" },
});

export function DataLoaderPage() {
  const styles = useStyles();
  const { projects, selectedProject, setSelectedProject } = useProjects();
  const [audit, setAudit] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const loadAudit = useCallback(async () => {
    if (!selectedProject) return;
    setLoading(true);
    try {
      const data = await getAuditInfo(selectedProject.id);
      setAudit(data);
    } finally {
      setLoading(false);
    }
  }, [selectedProject]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  const handleUpload = async (_files: File[]) => {
    if (!selectedProject) return;
    await triggerIngestion({
      project_name: selectedProject.name,
      index_name: selectedProject.index_name,
      container_name: selectedProject.name.toLowerCase().replace(/\s/g, "-"),
      project_id: selectedProject.id,
      chunking_strategy: selectedProject.chunking_strategy,
    });
    loadAudit();
  };

  return (
    <div className={styles.root}>
      <Text size={600} weight="semibold">
        Data Loader
      </Text>

      <div className={styles.toolbar}>
        <ProjectSelector
          projects={projects}
          selected={selectedProject}
          onSelect={setSelectedProject}
        />
      </div>

      <FileUpload onUpload={handleUpload} />

      <div className={styles.section}>
        <Text size={500} weight="semibold">
          Ingestion Status
        </Text>
        {loading ? (
          <Spinner size="small" style={{ marginTop: 16 }} />
        ) : (
          <Table style={{ marginTop: 8 }}>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>File</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell>Chunks</TableHeaderCell>
                <TableHeaderCell>Date</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {audit.map((row, i) => (
                <TableRow key={i}>
                  <TableCell>{row.source_file}</TableCell>
                  <TableCell>
                    <Badge
                      color={
                        row.status === "completed"
                          ? "success"
                          : row.status === "failed"
                            ? "danger"
                            : "warning"
                      }
                    >
                      {row.status}
                    </Badge>
                  </TableCell>
                  <TableCell>{row.chunk_count}</TableCell>
                  <TableCell>{row.created_at}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
