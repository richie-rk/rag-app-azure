import { makeStyles, Text, Card, CardHeader } from "@fluentui/react-components";
import { useAuth } from "../hooks/useAuth";

const useStyles = makeStyles({
  root: { padding: "24px", maxWidth: "640px", margin: "0 auto" },
  card: { marginTop: "16px" },
  info: { padding: "16px", display: "flex", flexDirection: "column", gap: "8px" },
});

export function SettingsPage() {
  const styles = useStyles();
  const { user, role } = useAuth();

  return (
    <div className={styles.root}>
      <Text size={600} weight="semibold">
        Settings
      </Text>

      <Card className={styles.card}>
        <CardHeader header={<Text weight="semibold">Profile</Text>} />
        <div className={styles.info}>
          <Text>
            <strong>Name:</strong> {user?.displayName || "N/A"}
          </Text>
          <Text>
            <strong>Email:</strong> {user?.mail || "N/A"}
          </Text>
          <Text>
            <strong>Role:</strong> {role}
          </Text>
        </div>
      </Card>

      <Card className={styles.card}>
        <CardHeader header={<Text weight="semibold">About</Text>} />
        <div className={styles.info}>
          <Text>rag-app-azure v1.0.0</Text>
          <Text size={200}>
            A production-grade RAG application on Azure with hybrid search,
            NDJSON streaming, and MSAL SSO authentication.
          </Text>
        </div>
      </Card>
    </div>
  );
}
