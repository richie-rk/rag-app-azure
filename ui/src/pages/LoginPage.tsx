import { useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Text,
  Button,
  Input,
  Card,
  CardHeader,
  Field,
  Divider,
  Spinner,
} from "@fluentui/react-components";
import { useAuth } from "../hooks/useAuth";
import { apiClient } from "../api/client";

const useStyles = makeStyles({
  root: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    height: "100vh",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  card: { width: "400px", padding: "24px" },
  form: { display: "flex", flexDirection: "column", gap: "16px", marginTop: "16px" },
  divider: { margin: "16px 0" },
});

export function LoginPage() {
  const styles = useStyles();
  const { login } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [verifying, setVerifying] = useState(false);

  // Handle magic link verification
  const token = searchParams.get("token");
  if (token && !verifying) {
    setVerifying(true);
    apiClient(`/auth/verify?token=${token}`)
      .then((result) => {
        if (result.token) {
          localStorage.setItem("rag_auth_token", result.token);
          navigate("/chat");
        }
      })
      .catch(() => setVerifying(false));
  }

  if (verifying) {
    return (
      <div className={styles.root}>
        <Spinner label="Verifying your link..." />
      </div>
    );
  }

  const handleMagicLink = async () => {
    if (!email) return;
    await apiClient("/auth/magic-link", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    setSent(true);
  };

  return (
    <div className={styles.root}>
      <Card className={styles.card}>
        <CardHeader
          header={
            <Text size={600} weight="semibold">
              RAG App Azure
            </Text>
          }
        />

        <div className={styles.form}>
          <Button appearance="primary" onClick={login} size="large">
            Sign in with Microsoft
          </Button>

          <Divider className={styles.divider}>or</Divider>

          {sent ? (
            <Text align="center">
              Check your email for a sign-in link.
            </Text>
          ) : (
            <>
              <Field label="Guest access via email">
                <Input
                  type="email"
                  value={email}
                  onChange={(_, d) => setEmail(d.value)}
                  placeholder="your@email.com"
                />
              </Field>
              <Button
                appearance="secondary"
                onClick={handleMagicLink}
                disabled={!email}
              >
                Send Magic Link
              </Button>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
