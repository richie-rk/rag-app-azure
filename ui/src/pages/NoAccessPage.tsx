import { makeStyles, tokens, Text, Button } from "@fluentui/react-components";
import { LockClosedRegular } from "@fluentui/react-icons";
import { useAuth } from "../auth/AuthProvider";

const useStyles = makeStyles({
  root: {
    height: "100vh",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: "12px",
    padding: "24px",
    textAlign: "center",
    backgroundColor: tokens.colorNeutralBackground1,
  },
  icon: {
    fontSize: "48px",
    color: tokens.colorNeutralForeground3,
  },
  title: {
    fontSize: "20px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  message: {
    fontSize: "14px",
    color: tokens.colorNeutralForeground3,
    maxWidth: "440px",
    lineHeight: "1.5",
  },
});

export function NoAccessPage() {
  const styles = useStyles();
  const { logout } = useAuth();

  return (
    <div className={styles.root}>
      <LockClosedRegular className={styles.icon} />
      <Text className={styles.title}>Access not granted</Text>
      <Text className={styles.message}>
        Your sign-in succeeded, but your account is not a member of a group
        that grants access to this application. Contact your administrator to
        request access. If you were sent a magic link, sign out first, then
        open it.
      </Text>
      <Button appearance="primary" onClick={logout}>
        Sign out
      </Button>
    </div>
  );
}
