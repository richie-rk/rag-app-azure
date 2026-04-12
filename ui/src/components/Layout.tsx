import { Outlet, useNavigate, useLocation } from "react-router-dom";
import {
  makeStyles,
  tokens,
  Text,
  Button,
  Divider,
  mergeClasses,
} from "@fluentui/react-components";
import {
  ChatRegular,
  FolderRegular,
  ArrowUploadRegular,
  PeopleRegular,
  SettingsRegular,
  SignOutRegular,
  DocumentSearchRegular,
} from "@fluentui/react-icons";
import { useAuth } from "../auth/AuthProvider";

const useStyles = makeStyles({
  root: {
    display: "flex",
    height: "100vh",
    overflow: "hidden",
  },
  sidebar: {
    width: "240px",
    backgroundColor: tokens.colorNeutralBackground2,
    display: "flex",
    flexDirection: "column",
    borderRight: `1px solid ${tokens.colorNeutralStroke2}`,
    flexShrink: 0,
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "16px 16px",
    height: "56px",
    boxSizing: "border-box",
  },
  brandIcon: {
    color: tokens.colorBrandForeground1,
    fontSize: "22px",
  },
  brandText: {
    fontSize: "16px",
    fontWeight: tokens.fontWeightBold,
    color: tokens.colorNeutralForeground1,
  },
  nav: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    padding: "8px 12px",
    gap: "2px",
    overflowY: "auto",
  },
  navItem: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "0 12px",
    height: "40px",
    borderRadius: "6px",
    cursor: "pointer",
    color: tokens.colorNeutralForeground2,
    fontSize: "14px",
    fontWeight: tokens.fontWeightRegular,
    border: "none",
    backgroundColor: "transparent",
    width: "100%",
    textAlign: "left",
    position: "relative",
    transitionProperty: "background-color, color",
    transitionDuration: "0.15s",
    "&:hover": {
      backgroundColor: tokens.colorNeutralBackground3Hover,
      color: tokens.colorNeutralForeground1,
    },
  },
  navItemActive: {
    backgroundColor: tokens.colorNeutralBackground3Selected,
    color: tokens.colorBrandForeground1,
    fontWeight: tokens.fontWeightSemibold,
    "&::before": {
      content: '""',
      position: "absolute",
      left: "0",
      top: "8px",
      bottom: "8px",
      width: "3px",
      borderRadius: "2px",
      backgroundColor: tokens.colorBrandForeground1,
    },
  },
  navIcon: {
    fontSize: "20px",
    flexShrink: 0,
  },
  footer: {
    padding: "12px 16px",
  },
  userInfo: {
    display: "flex",
    flexDirection: "column",
    gap: "2px",
    marginBottom: "4px",
  },
  userName: {
    fontSize: "14px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  userEmail: {
    fontSize: "12px",
    color: tokens.colorNeutralForeground3,
  },
  content: {
    flex: 1,
    overflow: "auto",
    backgroundColor: tokens.colorNeutralBackground1,
  },
});

export function Layout() {
  const styles = useStyles();
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, role } = useAuth();

  const navItems = [
    { path: "/chat", label: "Chat", icon: <ChatRegular className={styles.navIcon} /> },
    { path: "/projects", label: "Projects", icon: <FolderRegular className={styles.navIcon} /> },
    { path: "/dataloader", label: "Data Loader", icon: <ArrowUploadRegular className={styles.navIcon} /> },
    ...(role === "admin"
      ? [{ path: "/users", label: "Users", icon: <PeopleRegular className={styles.navIcon} /> }]
      : []),
    { path: "/settings", label: "Settings", icon: <SettingsRegular className={styles.navIcon} /> },
  ];

  const currentPath = "/" + location.pathname.split("/")[1];

  return (
    <div className={styles.root}>
      <div className={styles.sidebar}>
        <div className={styles.brand}>
          <DocumentSearchRegular className={styles.brandIcon} />
          <span className={styles.brandText}>RAG App</span>
        </div>
        <Divider />
        <nav className={styles.nav}>
          {navItems.map((item) => (
            <button
              key={item.path}
              className={mergeClasses(
                styles.navItem,
                currentPath === item.path && styles.navItemActive,
              )}
              onClick={() => navigate(item.path)}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </nav>
        <Divider />
        <div className={styles.footer}>
          <div className={styles.userInfo}>
            <Text className={styles.userName}>
              {user?.displayName || "Guest"}
            </Text>
            <Text className={styles.userEmail}>
              {user?.mail || ""}
            </Text>
          </div>
          <Button
            appearance="subtle"
            icon={<SignOutRegular />}
            onClick={logout}
            size="small"
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
