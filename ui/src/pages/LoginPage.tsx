import { useState, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Text,
  Button,
  Spinner,
} from "@fluentui/react-components";
import {
  DocumentSearchRegular,
  ShieldKeyholeRegular,
} from "@fluentui/react-icons";
import { useAuth } from "../hooks/useAuth";
import { apiClient } from "../api/client";

const useStyles = makeStyles({
  root: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    minHeight: "100vh",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  card: {
    width: "420px",
    padding: "40px",
    backgroundColor: tokens.colorNeutralBackground1,
    borderRadius: "12px",
    boxShadow: tokens.shadow16,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
  },
  iconContainer: {
    width: "64px",
    height: "64px",
    borderRadius: "16px",
    backgroundColor: tokens.colorBrandBackground2,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    marginBottom: "20px",
  },
  icon: {
    fontSize: "32px",
    color: tokens.colorBrandForeground1,
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "4px",
  },
  subtitle: {
    fontSize: "14px",
    color: tokens.colorNeutralForeground3,
    marginBottom: "28px",
  },
  msButton: {
    width: "100%",
    height: "44px",
    fontSize: "15px",
    fontWeight: tokens.fontWeightSemibold,
  },
  guestNote: {
    width: "100%",
    textAlign: "center",
    marginTop: "20px",
    padding: "12px 16px",
    backgroundColor: tokens.colorNeutralBackground3,
    borderRadius: "8px",
    color: tokens.colorNeutralForeground3,
    fontSize: "13px",
    lineHeight: "1.5",
  },
  footer: {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    marginTop: "28px",
    color: tokens.colorNeutralForeground4,
    fontSize: "12px",
  },
  footerIcon: {
    fontSize: "14px",
  },
});

export function LoginPage() {
  const styles = useStyles();
  const { login } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [verifying, setVerifying] = useState(false);

  // Handle magic link verification. The token goes in a POST body (never a
  // query string on the API call, which would land in server/proxy logs), and
  // the navigation replaces this history entry so the token-bearing URL does
  // not stay reachable via the back button.
  const token = searchParams.get("token");
  useEffect(() => {
    if (token && !verifying) {
      setVerifying(true);
      apiClient("/auth/verify", {
        method: "POST",
        body: JSON.stringify({ token }),
      })
        .then((result) => {
          if (result.token) {
            localStorage.setItem("rag_auth_token", result.token);
            navigate("/chat", { replace: true });
          }
        })
        .catch(() => setVerifying(false));
    }
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  if (verifying) {
    return (
      <div className={styles.root}>
        <Spinner label="Verifying your link..." size="large" />
      </div>
    );
  }

  return (
    <div className={styles.root}>
      <div className={styles.card}>
        <div className={styles.iconContainer}>
          <DocumentSearchRegular className={styles.icon} />
        </div>
        <Text className={styles.title}>rag-app-azure</Text>
        <Text className={styles.subtitle}>Enterprise RAG Platform</Text>

        <Button
          appearance="primary"
          className={styles.msButton}
          onClick={login}
          size="large"
          icon={
            <svg width="20" height="20" viewBox="0 0 21 21" fill="none">
              <rect x="1" y="1" width="9" height="9" fill="#F25022" />
              <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
              <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
              <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
            </svg>
          }
        >
          Sign in with Microsoft
        </Button>

        {/* Magic links are admin-issued invitations (the backend endpoint is
            admin-only); there is deliberately no self-service sender here. */}
        <div className={styles.guestNote}>
          Guest access is by invitation. Ask an administrator to send you a
          sign-in link.
        </div>

        <div className={styles.footer}>
          <ShieldKeyholeRegular className={styles.footerIcon} />
          <Text size={100} style={{ color: tokens.colorNeutralForeground4 }}>
            Secured by Microsoft Entra ID
          </Text>
        </div>
      </div>
    </div>
  );
}
