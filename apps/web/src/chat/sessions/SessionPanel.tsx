// The session panel (10-web-client.md section 8.2): New chat above the list of the caller's
// sessions, newest first, each a link with its title and, on a second line, its project and last
// activity. A session with an answer streaming in this tab shows a spinner. Pages load with Show
// more chats and are de-duplicated by `sessionId`.

import {
  Button,
  Skeleton,
  SkeletonItem,
  Spinner,
  Text,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components";
import { Add20Regular } from "@fluentui/react-icons";
import type { UseInfiniteQueryResult, InfiniteData } from "@tanstack/react-query";
import { useNavigate } from "react-router";

import { apiErrorOf, clientError } from "../../api/problem";
import { sessionsOf, type ProjectSummary, type SessionPage } from "../../api/queryClient";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { formatLastActivity } from "../../ui/format";
import { RouterLink } from "../../ui/RouterLink";

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
    minHeight: 0,
    height: "100%",
  },
  inline: {
    width: "280px",
    flexShrink: 0,
    borderRight: `${tokens.strokeWidthThin} solid ${tokens.colorNeutralStroke2}`,
    paddingRight: tokens.spacingHorizontalS,
    boxSizing: "border-box",
  },
  list: {
    listStyleType: "none",
    margin: "0",
    padding: "0",
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalXXS,
  },
  link: {
    display: "flex",
    flexDirection: "column",
    padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalS}`,
    borderRadius: tokens.borderRadiusMedium,
    textDecorationLine: "none",
    color: tokens.colorNeutralForeground1,
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover, textDecorationLine: "none" },
  },
  current: { backgroundColor: tokens.colorNeutralBackground1Selected },
  titleRow: {
    display: "flex",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalXS,
    minWidth: 0,
  },
  title: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 },
  detail: { color: tokens.colorNeutralForeground3 },
  detailRow: {
    display: "flex",
    columnGap: tokens.spacingHorizontalS,
    minWidth: 0,
    whiteSpace: "nowrap",
  },
  skeleton: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
});

export interface SessionPanelProps {
  readonly sessions: UseInfiniteQueryResult<InfiniteData<SessionPage>>;
  readonly projects: readonly ProjectSummary[];
  readonly currentSessionId: string | null;
  /** Sessions with an answer streaming in this tab. */
  readonly answering: ReadonlySet<string>;
  /** The chosen project, which New chat keeps. */
  readonly projectId: string | null;
  /** Inline beside the conversation, or inside the narrow layout's overlay. */
  readonly inline: boolean;
  /** Called after a navigation, so an overlay can close. */
  readonly onNavigate?: () => void;
}

export function SessionPanel({
  sessions,
  projects,
  currentSessionId,
  answering,
  projectId,
  inline,
  onNavigate,
}: SessionPanelProps) {
  const styles = useStyles();
  const navigate = useNavigate();
  const names = new Map(projects.map((project) => [project.projectId, project.name]));
  const items = sessionsOf(sessions.data);

  let list;
  if (sessions.data === undefined && sessions.isError) {
    list = (
      <ApiErrorBar
        error={apiErrorOf(sessions.error) ?? clientError("network")}
        lines={[en["chat.sessions.error"]]}
        onRetry={() => void sessions.refetch()}
      />
    );
  } else if (sessions.data === undefined) {
    list = (
      <Skeleton className={styles.skeleton}>
        {[0, 1, 2, 3, 4, 5].map((index) => (
          <SkeletonItem key={index} size={40} />
        ))}
      </Skeleton>
    );
  } else if (items.length === 0) {
    list = <Text className={styles.detail}>{en["chat.sessions.empty"]}</Text>;
  } else {
    list = (
      <ul className={styles.list}>
        {items.map((session) => {
          const current = session.sessionId === currentSessionId;
          const project = names.get(session.projectId);
          const when = formatLastActivity(session.lastTurnAt);
          return (
            <li key={session.sessionId}>
              <RouterLink
                to={`/chat/${session.sessionId}`}
                className={mergeClasses(styles.link, current && styles.current)}
                {...(current ? { "aria-current": "page" as const } : {})}
                onNavigate={onNavigate}
              >
                <span className={styles.titleRow}>
                  <span className={styles.title}>{session.title}</span>
                  {answering.has(session.sessionId) ? (
                    <Spinner size="tiny" aria-label={en["chat.sessions.answering"]} />
                  ) : null}
                </span>
                <span className={styles.detailRow}>
                  {project === undefined ? null : (
                    <Text size={200} className={mergeClasses(styles.detail, styles.title)}>
                      {project}
                    </Text>
                  )}
                  <Text size={200} className={styles.detail}>
                    {when}
                  </Text>
                </span>
              </RouterLink>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <aside
      aria-label={en["chat.sessions.label"]}
      className={mergeClasses(styles.root, inline && styles.inline)}
    >
      <div>
        <Button
          icon={<Add20Regular />}
          onClick={() => {
            void navigate(projectId === null ? "/chat" : `/chat?project=${projectId}`);
            onNavigate?.();
          }}
        >
          {en["chat.sessions.new"]}
        </Button>
      </div>
      {list}
      {sessions.hasNextPage ? (
        <div>
          <Button
            appearance="subtle"
            disabled={sessions.isFetchingNextPage}
            onClick={() => void sessions.fetchNextPage()}
          >
            {en["chat.sessions.more"]}
          </Button>
        </div>
      ) : null}
      {sessions.isFetchNextPageError ? (
        <ApiErrorBar
          error={apiErrorOf(sessions.error) ?? clientError("network")}
          onRetry={() => void sessions.fetchNextPage()}
        />
      ) : null}
    </aside>
  );
}
