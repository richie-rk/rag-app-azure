import { Outlet, useNavigate, useLocation } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Tab,
  TabList,
  Button,
  Text,
  Divider,
} from "@fluentui/react-components";
import {
  ChatRegular,
  FolderRegular,
  ArrowUploadRegular,
  PeopleRegular,
  SettingsRegular,
  SignOutRegular,
} from "@fluentui/react-icons";
import { useAuth } from "../auth/AuthProvider";

const useStyles = makeStyles({
  root: {
    display: "flex",
    height: "100vh",
    overflow: "hidden",
  },
  sidebar: {
    width: "220px",
    backgroundColor: tokens.colorNeutralBackground2,
    display: "flex",
    flexDirection: "column",
    borderRight: `1px solid ${tokens.colorNeutralStroke1}`,
  },
  logo: {
    padding: "16px",
    fontWeight: tokens.fontWeightSemibold,
    fontSize: tokens.fontSizeBase500,
  },
  nav: {
    flex: 1,
    padding: "8px",
  },
  footer: {
    padding: "12px 16px",
  },
  content: {
    flex: 1,
    overflow: "auto",
  },
});

export function Layout() {
  const styles = useStyles();
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, role } = useAuth();

  const navItems = [
    { path: "/chat", label: "Chat", icon: <ChatRegular /> },
    { path: "/projects", label: "Projects", icon: <FolderRegular /> },
    { path: "/dataloader", label: "Data Loader", icon: <ArrowUploadRegular /> },
    ...(role === "admin"
      ? [{ path: "/users", label: "Users", icon: <PeopleRegular /> }]
      : []),
    { path: "/settings", label: "Settings", icon: <SettingsRegular /> },
  ];

  const currentPath = "/" + location.pathname.split("/")[1];

  return (
    <div className={styles.root}>
      <div className={styles.sidebar}>
        <div className={styles.logo}>RAG App</div>
        <Divider />
        <div className={styles.nav}>
          <TabList
            vertical
            selectedValue={currentPath}
            onTabSelect={(_, data) => navigate(data.value as string)}
          >
            {navItems.map((item) => (
              <Tab key={item.path} value={item.path} icon={item.icon}>
                {item.label}
              </Tab>
            ))}
          </TabList>
        </div>
        <Divider />
        <div className={styles.footer}>
          <Text size={200} block>
            {user?.displayName || "Guest"}
          </Text>
          <Text size={100} block style={{ color: tokens.colorNeutralForeground3 }}>
            {user?.mail || ""}
          </Text>
          <Button
            appearance="subtle"
            icon={<SignOutRegular />}
            onClick={logout}
            size="small"
            style={{ marginTop: 8 }}
          >
            Sign out
          </Button>
        </div>
      </div>
      <div className={styles.content}>
        <Outlet />
      </div>
    </div>
  );
}
