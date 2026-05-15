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
  tokens,
  Card,
} from "@fluentui/react-components";
import { FileUpload } from "../components/FileUpload";
import { ProjectSelector } from "../components/ProjectSelector";
import { useProjects } from "../hooks/useProjects";
import { triggerIngestion, getAuditInfo } from "../api/ingestion";

const useStyles = makeStyles({
  root: {
    padding: "32px",
    maxWidth: "1000px",
    margin: "0 auto",
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "24px",
    display: "block",
  },
  projectRow: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    marginBottom: "20px",
  },
  projectLabel: {
    fontSize: "14px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground2,
  },
  section: {
    marginTop: "32px",
  },
  sectionTitle: {
    fontSize: "16px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "12px",
    display: "block",
  },
  tableCard: {
    marginTop: "8px",
    overflow: "hidden",
  },
  statusBadge: {
    textTransform: "capitalize" as const,
  },
  emptyTable: {
    textAlign: "center",
    padding: "32px",
    color: tokens.colorNeutralForeground3,
  },
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
      <Text className={styles.title}>Data Loader</Text>

      <div className={styles.projectRow}>
        <Text className={styles.projectLabel}>Project</Text>
        <ProjectSelector
          projects={projects}
          selected={selectedProject}
          onSelect={setSelectedProject}
        />
      </div>

      <FileUpload onUpload={handleUpload} />

      <div className={styles.section}>
        <Text className={styles.sectionTitle}>Ingestion Status</Text>

        {loading ? (
          <Spinner size="small" style={{ padding: 24 }} />
        ) : (
          <Card className={styles.tableCard}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>File</TableHeaderCell>
                  <TableHeaderCell>Status</TableHeaderCell>
                  <TableHeaderCell>Chunks</TableHeaderCell>
                  <TableHeaderCell>Date</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {audit.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4}>
                      <div className={styles.emptyTable}>
                        No ingestion records yet.
                      </div>
                    </TableCell>
                  </TableRow>
                ) : (
                  audit.map((row, i) => (
                    <TableRow key={i}>
                      <TableCell>{row.source_file}</TableCell>
                      <TableCell>
                        <Badge
                          className={styles.statusBadge}
                          color={
                            row.status === "completed"
                              ? "success"
                              : row.status === "failed"
                                ? "danger"
                                : "warning"
                          }
                          size="small"
                        >
                          {row.status}
                        </Badge>
                      </TableCell>
                      <TableCell>{row.chunk_count}</TableCell>
                      <TableCell>{row.created_at}</TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>
    </div>
  );
}
