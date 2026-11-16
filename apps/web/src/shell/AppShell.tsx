// The app shell (10-web-client.md sections 4.2 step 9, 4.6 and 6.3). It shows the Starting screen
// until `getCurrentUser` succeeds, because the API provisions the user's row on that first call and
// every other query waits for it; then the skip link, the top bar, the navigation drawer and the
// `main` landmark with the Session expired bar and the routed screen.

import { Link, makeStyles, tokens } from "@fluentui/react-components";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { Outlet } from "react-router";

import { apiErrorOf, clientError, isProblem } from "../api/problem";
import { meQuery, sessionsOf, sessionsQuery } from "../api/queryClient";
import type { ResponseOf } from "../api/operations";
import { LoadUserFailedScreen, StartingScreen } from "../auth/AccessScreens";
import { SessionExpiredBar } from "../auth/SessionExpiredBar";
import { useSession } from "../auth/session";
import { markShellRendered } from "../auth/tokens";
import { en } from "../copy/en";
import { hasActiveWork } from "../platform/activeWork";
import { readNavOpen, writeNavOpen } from "../platform/storage";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { WIDE_QUERY, useMediaQuery } from "../ui/media";
import { NavPanel, type RecentChatsState } from "./NavPanel";
import { TopBar } from "./TopBar";

export type CurrentUser = ResponseOf<"getCurrentUser">;

/** How long the account menu takes to close and hand focus back (see `requestSignOut`). */
const MENU_SETTLE_MS = 200;

/** Exported for component tests that render a screen part without the shell. */
export const CurrentUserContext = createContext<CurrentUser | null>(null);

/** `getCurrentUser`'s answer: `displayName`, `email`, `isAdmin` and `features`. Only inside the
 * shell, which renders its children only once the call has succeeded. */
export function useCurrentUser(): CurrentUser {
  const user = useContext(CurrentUserContext);
  if (user === null) throw new Error("useCurrentUser outside the app shell");
  return user;
}

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    height: "100vh",
    backgroundColor: tokens.colorNeutralBackground1,
    color: tokens.colorNeutralForeground1,
  },
  skipLink: {
    position: "absolute",
    top: tokens.spacingVerticalS,
    left: tokens.spacingHorizontalS,
    zIndex: 1000,
    padding: tokens.spacingHorizontalS,
    backgroundColor: tokens.colorNeutralBackground1,
    transform: "translateY(-200%)",
    ":focus": { transform: "none" },
    ":focus-visible": { transform: "none" },
  },
  body: { display: "flex", flexGrow: 1, minHeight: 0 },
  main: {
    flexGrow: 1,
    minWidth: 0,
    overflowY: "auto",
    outlineStyle: "none",
    padding: tokens.spacingHorizontalL,
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalM,
  },
});

/** Recent chats: the first six sessions of the same `['sessions']` query as the session panel. */
function useRecentChats(): RecentChatsState {
  const sessions = useInfiniteQuery(sessionsQuery());
  if (sessions.data !== undefined) return { status: "success", items: sessionsOf(sessions.data) };
  return sessions.isError ? { status: "error" } : { status: "pending" };
}

function ShellLayout({ user }: { readonly user: CurrentUser }) {
  const styles = useStyles();
  const session = useSession();
  const wide = useMediaQuery(WIDE_QUERY);
  const [inlineOpen, setInlineOpen] = useState(() => readNavOpen() ?? true);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const [confirmingSignOut, setConfirmingSignOut] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const recentChats = useRecentChats();
  const navOpen = wide ? inlineOpen : overlayOpen;

  useEffect(() => {
    markShellRendered();
  }, []);

  const setNavOpen = (open: boolean) => {
    if (wide) {
      setInlineOpen(open);
      writeNavOpen(open);
    } else {
      setOverlayOpen(open);
    }
  };

  const requestSignOut = () => {
    if (!hasActiveWork()) {
      void session.signOut();
      return;
    }
    // The account menu is still closing, and Fluent's focus manager returns focus to its trigger
    // on timers of up to 100 ms. A dialog that opens before then can be left marked aria-hidden,
    // so it opens once the menu has settled.
    window.setTimeout(() => {
      setConfirmingSignOut(true);
    }, MENU_SETTLE_MS);
  };

  return (
    <div className={styles.root}>
      <Link
        href="#main"
        className={styles.skipLink}
        onClick={(event) => {
          event.preventDefault();
          mainRef.current?.focus();
        }}
      >
        {en["shell.skipLink"]}
      </Link>
      <TopBar
        navOpen={navOpen}
        onToggleNav={() => {
          setNavOpen(!navOpen);
        }}
        displayName={user.displayName}
        email={user.email}
        onSignOut={requestSignOut}
      />
      <div className={styles.body}>
        <NavPanel
          open={navOpen}
          type={wide ? "inline" : "overlay"}
          isAdmin={user.isAdmin}
          recentChats={recentChats}
          onOpenChange={setNavOpen}
        />
        <main id="main" ref={mainRef} tabIndex={-1} className={styles.main}>
          <SessionExpiredBar />
          <Outlet />
        </main>
      </div>
      <ConfirmDialog
        open={confirmingSignOut}
        title={en["shell.signOut.title"]}
        body={en["shell.signOut.body"]}
        actionLabel={en["shell.signOut.confirm"]}
        cancelLabel={en["shell.signOut.cancel"]}
        onConfirm={() => {
          setConfirmingSignOut(false);
          void session.signOut();
        }}
        onCancel={() => {
          setConfirmingSignOut(false);
        }}
      />
    </div>
  );
}

export function AppShell() {
  const me = useQuery(meQuery());
  const session = useSession();
  if (me.data !== undefined) {
    return (
      <CurrentUserContext.Provider value={me.data}>
        <ShellLayout user={me.data} />
      </CurrentUserContext.Provider>
    );
  }
  if (me.isError) {
    const error = apiErrorOf(me.error) ?? clientError("network");
    // The app root has already replaced the page with Not assigned.
    if (isProblem(error, 403, "role_not_assigned")) return <StartingScreen />;
    return (
      <LoadUserFailedScreen
        error={error}
        onRetry={() => void me.refetch()}
        onSignOut={() => void session.signOut()}
      />
    );
  }
  return <StartingScreen />;
}
