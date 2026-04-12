import { useNavigate } from "react-router-dom";
import {
  makeStyles,
  Text,
  Button,
  Card,
  CardHeader,
  Badge,
} from "@fluentui/react-components";
import { AddRegular, EditRegular } from "@fluentui/react-icons";
import { useProjects } from "../hooks/useProjects";

const useStyles = makeStyles({
  root: { padding: "24px", maxWidth: "960px", margin: "0 auto" },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "16px",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
    gap: "16px",
  },
  card: { cursor: "pointer" },
  meta: {
    display: "flex",
    gap: "8px",
    marginTop: "8px",
    flexWrap: "wrap",
  },
});

export function ProjectsPage() {
  const styles = useStyles();
  const navigate = useNavigate();
  const { projects, loading: _loading } = useProjects();

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <Text size={600} weight="semibold">
          Projects
        </Text>
        <Button
          appearance="primary"
          icon={<AddRegular />}
          onClick={() => navigate("/projects/new")}
        >
          New Project
        </Button>
      </div>

      <div className={styles.grid}>
        {projects.map((p) => (
          <Card key={p.id} className={styles.card}>
            <CardHeader
              header={<Text weight="semibold">{p.display_name || p.name}</Text>}
              description={p.department}
              action={
                <Button
                  appearance="subtle"
                  icon={<EditRegular />}
                  onClick={() => navigate(`/projects/${p.id}/edit`)}
                />
              }
            />
            <div className={styles.meta}>
              <Badge appearance="outline">{p.chunking_strategy}</Badge>
              <Badge appearance="outline">{p.llm_deployment}</Badge>
              {p.is_default && <Badge color="brand">Default</Badge>}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
