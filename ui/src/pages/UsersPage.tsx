import { useEffect, useState } from "react";
import {
  makeStyles,
  Text,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Badge,
  Spinner,
  Input,
  Button,
  Card,
  tokens,
  MessageBar,
  MessageBarBody,
  Dialog,
  DialogSurface,
  DialogTitle,
  DialogBody,
  DialogContent,
  DialogActions,
} from "@fluentui/react-components";
import {
  SearchRegular,
  PersonAddRegular,
} from "@fluentui/react-icons";
import { apiClient } from "../api/client";
import { createMagicLink } from "../api/auth";
import { useAuth } from "../auth/AuthProvider";
import type { UserInfo } from "../api/types";

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
    marginBottom: "20px",
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  searchRow: {
    marginBottom: "16px",
  },
  searchInput: {
    maxWidth: "320px",
  },
  tableCard: {
    overflow: "hidden",
  },
  avatarCell: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  avatar: {
    width: "32px",
    height: "32px",
    borderRadius: "50%",
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground1,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: "13px",
    fontWeight: tokens.fontWeightSemibold,
    flexShrink: 0,
  },
  nameText: {
    fontSize: "14px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  emailText: {
    fontSize: "13px",
    color: tokens.colorNeutralForeground2,
  },
  emptyRow: {
    textAlign: "center",
    padding: "32px",
    color: tokens.colorNeutralForeground3,
  },
  dialogContent: {
    display: "flex",
    flexDirection: "column",
    gap: "12px",
    minWidth: "360px",
  },
  linkRow: {
    display: "flex",
    gap: "8px",
    alignItems: "center",
  },
  linkInput: {
    flex: 1,
  },
});

function getInitials(name: string): string {
  return name
    .split(" ")
    .map((n) => n[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

export function UsersPage() {
  const styles = useStyles();
  const { token, isLoading: authLoading } = useAuth();
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  // Invite (magic link) dialog state.
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteSending, setInviteSending] = useState(false);
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);

  useEffect(() => {
    // Keep the spinner up while auth is still resolving. Once it has, fetch
    // with the token if we have one; otherwise let apiClient hit the 401 path
    // and redirect to /login, instead of leaving the page in perpetual loading.
    if (authLoading) return;
    apiClient("/users", { token: token || undefined })
      .then(setUsers)
      .finally(() => setLoading(false));
  }, [token, authLoading]);

  function openInvite() {
    setInviteEmail("");
    setInviteLink(null);
    setInviteError(null);
    setInviteOpen(true);
  }

  async function sendInvite() {
    setInviteSending(true);
    setInviteError(null);
    try {
      const result = await createMagicLink(inviteEmail, token || undefined);
      setInviteLink(result.link);
    } catch (err) {
      setInviteError(
        err instanceof Error ? err.message : "Failed to create the magic link.",
      );
    } finally {
      setInviteSending(false);
    }
  }

  const filtered = search
    ? users.filter(
        (u) =>
          u.display_name.toLowerCase().includes(search.toLowerCase()) ||
          u.email.toLowerCase().includes(search.toLowerCase()),
      )
    : users;

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
        <Text className={styles.title}>Users</Text>
        <Button
          appearance="primary"
          icon={<PersonAddRegular />}
          onClick={openInvite}
        >
          Invite User
        </Button>
      </div>

      <div className={styles.searchRow}>
        <Input
          className={styles.searchInput}
          contentBefore={<SearchRegular />}
          placeholder="Search users..."
          value={search}
          onChange={(_, d) => setSearch(d.value)}
        />
      </div>

      <Card className={styles.tableCard}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Name</TableHeaderCell>
              <TableHeaderCell>Email</TableHeaderCell>
              <TableHeaderCell>Role</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3}>
                  <div className={styles.emptyRow}>
                    {search ? "No users match your search." : "No users found."}
                  </div>
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>
                    <div className={styles.avatarCell}>
                      <div className={styles.avatar}>
                        {getInitials(u.display_name)}
                      </div>
                      <Text className={styles.nameText}>{u.display_name}</Text>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Text className={styles.emailText}>{u.email}</Text>
                  </TableCell>
                  <TableCell>
                    <Badge
                      color={
                        u.role === "admin"
                          ? "brand"
                          : u.role === "guest"
                            ? "important"
                            : "informative"
                      }
                      size="small"
                    >
                      {u.role}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </Card>

      <Dialog
        open={inviteOpen}
        onOpenChange={(_, data) => setInviteOpen(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Send magic link</DialogTitle>
            <DialogContent>
              <div className={styles.dialogContent}>
                {inviteLink ? (
                  <>
                    <Text>Share this link with the recipient:</Text>
                    <div className={styles.linkRow}>
                      <Input
                        className={styles.linkInput}
                        value={inviteLink}
                        readOnly
                      />
                      <Button
                        onClick={() => {
                          navigator.clipboard.writeText(inviteLink);
                        }}
                      >
                        Copy
                      </Button>
                    </div>
                  </>
                ) : (
                  <>
                    <Input
                      placeholder="Recipient email"
                      value={inviteEmail}
                      onChange={(_, d) => setInviteEmail(d.value)}
                      disabled={inviteSending}
                    />
                    {inviteError && (
                      <MessageBar intent="error">
                        <MessageBarBody>{inviteError}</MessageBarBody>
                      </MessageBar>
                    )}
                  </>
                )}
              </div>
            </DialogContent>
            <DialogActions>
              {inviteLink ? (
                <Button onClick={() => setInviteOpen(false)}>Close</Button>
              ) : (
                <>
                  <Button onClick={() => setInviteOpen(false)}>Cancel</Button>
                  <Button
                    appearance="primary"
                    onClick={sendInvite}
                    disabled={inviteSending || !inviteEmail}
                  >
                    {inviteSending ? "Sending..." : "Send"}
                  </Button>
                </>
              )}
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  );
}
