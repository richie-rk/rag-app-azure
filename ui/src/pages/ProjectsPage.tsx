import { useNavigate } from "react-router-dom";
import {
  makeStyles,
  Text,
  Button,
  Card,
  CardHeader,
  Badge,
  tokens,
  Spinner,
} from "@fluentui/react-components";
import { AddRegular, EditRegular, FolderRegular } from "@fluentui/react-icons";
import { useProjects } from "../hooks/useProjects";

const useStyles = makeStyles({
  root: {
    padding: "32px",
    maxWidth: "1000px",
    margin: "0 auto",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "24px",
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(3, 1fr)",
    gap: "16px",
  },
  card: {
    cursor: "pointer",
    transitionProperty: "box-shadow",
    transitionDuration: "0.2s",
    "&:hover": {
      boxShadow: tokens.shadow8,
    },
  },
  cardContent: {
    padding: "4px 16px 16px",
  },
  projectName: {
    fontSize: "15px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  department: {
    fontSize: "13px",
    color: tokens.colorNeutralForeground3,
  },
  badges: {
    display: "flex",
    gap: "6px",
    marginTop: "12px",
    flexWrap: "wrap",
  },
  emptyState: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    padding: "64px 0",
    gap: "12px",
    color: tokens.colorNeutralForeground3,
  },
  emptyIcon: {
    fontSize: "48px",
    color: tokens.colorNeutralForeground4,
  },
});

export function ProjectsPage() {
  const styles = useStyles();
  const navigate = useNavigate();
  const { projects, loading } = useProjects();

  if (loading) {
    return (
      <div className={styles.root}>
        <Spinner style={{ padding: 48 }} />
      </div>
    );
  }

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <Text className={styles.title}>Projects</Text>
        <Button
          appearance="primary"
          icon={<AddRegular />}
          onClick={() => navigate("/projects/new")}
        >
          New Project
        </Button>
      </div>

      {projects.length === 0 ? (
        <div className={styles.emptyState}>
          <FolderRegular className={styles.emptyIcon} />
          <Text size={400} weight="semibold">No projects yet</Text>
          <Text size={300}>Create your first project to get started.</Text>
          <Button
            appearance="primary"
            icon={<AddRegular />}
            onClick={() => navigate("/projects/new")}
            style={{ marginTop: 8 }}
          >
            New Project
          </Button>
        </div>
      ) : (
        <div className={styles.grid}>
          {projects.map((p) => (
            <Card
              key={p.id}
              className={styles.card}
              onClick={() => navigate(`/projects/${p.id}/edit`)}
            >
              <CardHeader
                header={<Text className={styles.projectName}>{p.display_name || p.name}</Text>}
                description={<Text className={styles.department}>{p.department}</Text>}
                action={
                  <Button
                    appearance="subtle"
                    icon={<EditRegular />}
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      navigate(`/projects/${p.id}/edit`);
                    }}
                  />
                }
              />
              <div className={styles.cardContent}>
                <div className={styles.badges}>
                  <Badge appearance="outline" size="small">
                    {p.chunking_strategy}
                  </Badge>
                  <Badge appearance="outline" size="small">
                    {p.llm_deployment}
                  </Badge>
                  {p.is_default && (
                    <Badge color="brand" size="small">
                      Default
                    </Badge>
                  )}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
