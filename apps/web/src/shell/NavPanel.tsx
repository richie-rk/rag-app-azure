// The navigation drawer (10-web-client.md section 6.3): the admin section for admins only, Chat
// and Memory, then Recent chats with the six newest sessions and New chat. The item matching the
// route is selected. The drawer is inline from 1,280 px wide and an overlay below.

import {
  NavDivider,
  NavDrawer,
  NavDrawerBody,
  NavItem,
  NavSectionHeader,
  Skeleton,
  SkeletonItem,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import {
  Add20Regular,
  Chat20Regular,
  DocumentFolder20Regular,
  History20Regular,
  Library20Regular,
  Person20Regular,
  TextBulletListSquare20Regular,
} from "@fluentui/react-icons";
import type { MouseEvent, ReactElement, ReactNode } from "react";
import { useLocation, useNavigate } from "react-router";

import { en } from "../copy/en";
import { RECENT_CHATS } from "../platform/constants";
import { isModifiedClick } from "../ui/RouterLink";

export interface RecentChat {
  readonly sessionId: string;
  readonly title: string;
}

/** The Recent chats group's data: the first six items of `['sessions']`. */
export type RecentChatsState =
  | { readonly status: "pending" }
  | { readonly status: "error" }
  | { readonly status: "success"; readonly items: readonly RecentChat[] };

const useStyles = makeStyles({
  note: {
    display: "block",
    paddingTop: tokens.spacingVerticalXS,
    paddingBottom: tokens.spacingVerticalXS,
    paddingLeft: tokens.spacingHorizontalM,
    paddingRight: tokens.spacingHorizontalM,
    color: tokens.colorNeutralForeground3,
  },
  skeleton: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
    paddingLeft: tokens.spacingHorizontalM,
    paddingRight: tokens.spacingHorizontalM,
  },
  title: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
});

/** The nav item value the route selects. */
export function selectedNavValue(
  pathname: string,
  recent: readonly RecentChat[],
): string | undefined {
  if (pathname.startsWith("/admin/projects")) return "projects";
  if (pathname.startsWith("/admin/indexes")) return "indexes";
  if (pathname.startsWith("/admin/runs")) return "runs";
  if (pathname.startsWith("/memory")) return "memory";
  const session = /^\/chat\/([^/]+)$/.exec(pathname)?.[1];
  if (session !== undefined && recent.some((item) => item.sessionId === session)) {
    return `session:${session}`;
  }
  if (pathname.startsWith("/chat")) return "chat";
  return undefined;
}

interface LinkItemProps {
  readonly to: string;
  readonly value: string;
  readonly icon: ReactElement;
  readonly children: ReactNode;
  readonly onNavigate: () => void;
}

function LinkItem({ to, value, icon, children, onNavigate }: LinkItemProps) {
  const navigate = useNavigate();
  return (
    <NavItem
      href={to}
      value={value}
      icon={icon}
      onClick={(event: MouseEvent<HTMLAnchorElement | HTMLButtonElement>) => {
        if (isModifiedClick(event)) return;
        event.preventDefault();
        onNavigate();
        void navigate(to);
      }}
    >
      {children}
    </NavItem>
  );
}

/** Recent chats: a skeleton while loading, `shell.nav.noChats` when empty, `shell.nav.recentError`
 * on failure (the chat screen offers Retry), else the six newest sessions. */
export function RecentChatsList({
  state,
  onNavigate,
}: {
  readonly state: RecentChatsState;
  readonly onNavigate: () => void;
}) {
  const styles = useStyles();
  if (state.status === "pending") {
    return (
      <Skeleton className={styles.skeleton}>
        <SkeletonItem />
        <SkeletonItem />
        <SkeletonItem />
      </Skeleton>
    );
  }
  if (state.status === "error") {
    return <Text className={styles.note}>{en["shell.nav.recentError"]}</Text>;
  }
  if (state.items.length === 0) {
    return <Text className={styles.note}>{en["shell.nav.noChats"]}</Text>;
  }
  return (
    <>
      {state.items.slice(0, RECENT_CHATS).map((item) => (
        <LinkItem
          key={item.sessionId}
          to={`/chat/${item.sessionId}`}
          value={`session:${item.sessionId}`}
          icon={<History20Regular />}
          onNavigate={onNavigate}
        >
          <span className={styles.title}>{item.title}</span>
        </LinkItem>
      ))}
    </>
  );
}

export interface NavPanelProps {
  readonly open: boolean;
  /** Inline from 1,280 px wide, an overlay below. */
  readonly type: "inline" | "overlay";
  readonly isAdmin: boolean;
  readonly recentChats: RecentChatsState;
  readonly onOpenChange: (open: boolean) => void;
}

export function NavPanel({ open, type, isAdmin, recentChats, onOpenChange }: NavPanelProps) {
  const { pathname, search } = useLocation();
  const recentItems =
    recentChats.status === "success" ? recentChats.items.slice(0, RECENT_CHATS) : [];
  // An overlay closes once the user has picked where to go.
  const afterNavigate = () => {
    if (type === "overlay") onOpenChange(false);
  };
  const project = new URLSearchParams(search).get("project");
  const newChatTarget = project === null ? "/chat" : `/chat?project=${encodeURIComponent(project)}`;
  const selected = selectedNavValue(pathname, recentItems);
  return (
    <NavDrawer
      open={open}
      type={type}
      {...(selected === undefined ? {} : { selectedValue: selected })}
      aria-label={en["shell.nav.label"]}
      // The overlay is a Fluent dialog surface given the navigation role, and the surface adds
      // aria-modal, which a navigation landmark doesn't allow. The overlay still traps focus and
      // hides the page behind it from assistive technology, so the attribute adds nothing.
      {...(type === "overlay" ? { "aria-modal": undefined } : {})}
      onOpenChange={(_, data) => {
        onOpenChange(data.open);
      }}
    >
      <NavDrawerBody>
        {isAdmin ? (
          <>
            <NavSectionHeader>{en["shell.nav.admin"]}</NavSectionHeader>
            <LinkItem
              to="/admin/projects"
              value="projects"
              icon={<DocumentFolder20Regular />}
              onNavigate={afterNavigate}
            >
              {en["shell.nav.projects"]}
            </LinkItem>
            <LinkItem
              to="/admin/indexes"
              value="indexes"
              icon={<Library20Regular />}
              onNavigate={afterNavigate}
            >
              {en["shell.nav.indexes"]}
            </LinkItem>
            <LinkItem
              to="/admin/runs"
              value="runs"
              icon={<TextBulletListSquare20Regular />}
              onNavigate={afterNavigate}
            >
              {en["shell.nav.runs"]}
            </LinkItem>
            <NavDivider />
          </>
        ) : null}
        <LinkItem to="/chat" value="chat" icon={<Chat20Regular />} onNavigate={afterNavigate}>
          {en["shell.nav.chat"]}
        </LinkItem>
        <LinkItem to="/memory" value="memory" icon={<Person20Regular />} onNavigate={afterNavigate}>
          {en["shell.nav.memory"]}
        </LinkItem>
        <NavSectionHeader>{en["shell.nav.recent"]}</NavSectionHeader>
        <RecentChatsList state={recentChats} onNavigate={afterNavigate} />
        <LinkItem
          to={newChatTarget}
          value="new-chat"
          icon={<Add20Regular />}
          onNavigate={afterNavigate}
        >
          {en["shell.nav.newChat"]}
        </LinkItem>
      </NavDrawerBody>
    </NavDrawer>
  );
}
