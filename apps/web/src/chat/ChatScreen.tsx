// The chat screen (`/chat` and `/chat/:sessionId`, 10-web-client.md sections 7.2 and 8): the
// session panel, then the main panel with the project picker, the conversation, the follow-up
// suggestions and the composer, and the citation viewer at the right-hand end. Live turns come
// from the turn store and outlive navigation; stored turns from `getSession`.

import {
  Button,
  MessageBar,
  MessageBarBody,
  OverlayDrawer,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ChatMultiple20Regular } from "@fluentui/react-icons";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import { useVisuallyHiddenStyles } from "../a11y/LiveRegions";

import { apiErrorOf, clientError, isProblem, type ApiError } from "../api/problem";
import {
  myProjectsQuery,
  queryKeys,
  sessionsOf,
  sessionsQuery,
  type SessionPage,
} from "../api/queryClient";
import { NoProjectsState } from "../auth/AccessScreens";
import { en } from "../copy/en";
import { useCurrentUser } from "../shell/AppShell";
import { ApiErrorBar } from "../ui/ErrorBar";
import { fillText } from "../ui/format";
import { useBreakpoint } from "../ui/media";
import { useDocumentTitle } from "../ui/PageHeader";
import { CitationViewer, type CitationTarget } from "./citations/CitationViewer";
import { Composer } from "./composer/Composer";
import { draftKey, setDraft, setDraftReason } from "./composer/drafts";
import { ProjectPicker } from "./project/ProjectPicker";
import { chooseProject } from "./project/selection";
import { SessionPanel } from "./sessions/SessionPanel";
import type { Citation } from "./stream/events";
import {
  blocksSending,
  isSettled,
  useConversationTurns,
  useLiveTurns,
  useTurnStore,
  type LiveTurn,
  type SendHandle,
} from "./stream/turnStore";
import { ChatUnavailable } from "./turns/ChatUnavailable";
import { Conversation } from "./turns/Conversation";
import { EmptyState } from "./turns/EmptyState";
import { Followups } from "./turns/Followups";
import { useWaiting } from "./turns/time";
import {
  arrangeTurns,
  newestSeq,
  rememberAnchor,
  turnModel,
  type TurnModel,
} from "./turns/turnModel";
import { useSessionTurns } from "./turns/useSessionTurns";

const useStyles = makeStyles({
  root: { display: "flex", flexGrow: 1, minHeight: 0, columnGap: tokens.spacingHorizontalM },
  main: {
    display: "flex",
    flexDirection: "column",
    flexGrow: 1,
    minWidth: 0,
    minHeight: 0,
    rowGap: tokens.spacingVerticalS,
  },
  pickerRow: { display: "flex", alignItems: "flex-end", columnGap: tokens.spacingHorizontalS },
  bottom: {
    width: "100%",
    maxWidth: "860px",
    marginLeft: "auto",
    marginRight: "auto",
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
  },
  overlayPanel: { padding: tokens.spacingHorizontalM, height: "100%", boxSizing: "border-box" },
  note: { paddingTop: tokens.spacingVerticalL },
});

/** The reason under the composer after a `422` from `createChatTurn` (section 8.6). */
export function questionReasonText(error: ApiError): string {
  const reason = error.errors.find((entry) => entry.path === "body.question")?.reason;
  switch (reason) {
    case "empty":
      return en["chat.composer.reason.empty"];
    case "too_long":
      return en["chat.composer.tooLong"];
    case "invalid_characters":
      return en["chat.composer.reason.invalid_characters"];
    default:
      return en["error.validation.field"];
  }
}

/** Until when sending stays blocked by a wait: a `429` of any live turn, which limits the user,
 * or a `409 turn_in_progress` in this conversation. */
export function blockedUntilOf(
  all: readonly LiveTurn[],
  conversation: readonly LiveTurn[],
): number | null {
  let until: number | null = null;
  for (const turn of all) {
    const blocked = turn.failure?.blockedUntil ?? null;
    if (blocked === null) continue;
    if (turn.failure?.error?.status === 429 || conversation.includes(turn)) {
      until = Math.max(until ?? blocked, blocked);
    }
  }
  return until;
}

/** Location state of the `meta` replacement, which isn't an arrival: focus stays in the
 * composer (section 11.4). */
const KEEP_FOCUS = "docchatKeepFocus";

/** The screen's visually hidden `h1`, focused on every route change (section 6.4) except the
 * `meta` replacement of a new chat's URL. Focus doesn't scroll, so the session panel keeps its
 * scroll position. */
function ChatHeading({ children }: { readonly children: string }) {
  const hidden = useVisuallyHiddenStyles();
  const ref = useRef<HTMLHeadingElement>(null);
  const location = useLocation();
  const keepFocus =
    typeof location.state === "object" && location.state !== null && KEEP_FOCUS in location.state;
  useEffect(() => {
    if (!keepFocus) ref.current?.focus({ preventScroll: true });
  }, [location.pathname, keepFocus]);
  return (
    <h1 ref={ref} tabIndex={-1} className={hidden.root}>
      {children}
    </h1>
  );
}

interface ViewerState {
  readonly target: CitationTarget;
  readonly opener: HTMLElement;
}

export function ChatScreen() {
  const styles = useStyles();
  const sessionId = useParams()["sessionId"] ?? null;
  const [searchParams] = useSearchParams();
  const projectParam = searchParams.get("project");
  const navigate = useNavigate();
  const client = useQueryClient();
  const user = useCurrentUser();
  const turnStore = useTurnStore();
  const breakpoint = useBreakpoint();
  const projectsQuery = useQuery(myProjectsQuery());
  const sessionList = useInfiniteQuery(sessionsQuery());
  const { session, earlier, stored } = useSessionTurns(sessionId);
  const [viewer, setViewer] = useState<ViewerState | null>(null);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [composerHeight, setComposerHeight] = useState(0);

  const projects = projectsQuery.data ?? null;
  const sessions = sessionsOf(sessionList.data);
  const allLive = useLiveTurns();
  // A new chat that has just taken its session's URL has no `getSession` answer yet; its live
  // turn names the project, so the picker keeps it and the composer stays enabled, with focus in
  // it (section 11.4), rather than being disabled until the session loads.
  const liveSessionProjectId =
    sessionId === null
      ? null
      : (allLive.find((turn) => turn.sessionId === sessionId)?.projectId ?? null);
  const selected =
    projects === null
      ? null
      : chooseProject({
          projects,
          sessionId,
          sessionProjectId: session.data?.projectId ?? liveSessionProjectId,
          projectParam,
          newestSessionProjectId: sessions[0]?.projectId ?? null,
        });
  const projectId = selected?.projectId ?? null;
  const live = useConversationTurns(sessionId, sessionId === null ? projectId : null);
  const arranged = useMemo(() => arrangeTurns(stored, live), [stored, live]);
  const sessionProjectId = session.data?.projectId ?? null;
  const models = useMemo(
    () => arranged.turns.map((item) => turnModel(item, sessionId, sessionProjectId)),
    [arranged.turns, sessionId, sessionProjectId],
  );

  // A stored turn with a final status replaces its live copy in place (section 8.13).
  useEffect(() => {
    if (arranged.replaced.length > 0) turnStore.removeByTurnIds(arranged.replaced);
  }, [arranged.replaced, turnStore]);

  const sessionError = session.data === undefined && session.isError ? session.error : null;
  const liveNotFound = live.find((turn) =>
    isProblem(turn.failure?.error, 404, "session_not_found"),
  );
  const unavailable: ApiError | null = isProblem(apiErrorOf(sessionError), 404, "session_not_found")
    ? apiErrorOf(sessionError)
    : (liveNotFound?.failure?.error ?? null);

  // A session that can't be opened leaves the cached list, which is read once more.
  const missingSession = unavailable !== null ? sessionId : null;
  useEffect(() => {
    if (missingSession === null) return;
    client.setQueryData<InfiniteData<SessionPage>>(queryKeys.sessions, (data) =>
      data === undefined
        ? data
        : {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.filter((item) => item.sessionId !== missingSession),
            })),
          },
    );
    void client.invalidateQueries({ queryKey: queryKeys.sessions });
  }, [client, missingSession]);

  const streamingTurn = live.find(blocksSending) ?? null;
  const blockedUntil = blockedUntilOf(allLive, live);
  const waiting = useWaiting(blockedUntil);
  const notReady = selected !== null && !selected.hasDefaultIndex;
  const composerDisabled = selected === null || notReady || unavailable !== null;
  const blocked = composerDisabled || streamingTurn !== null || waiting;
  const key = draftKey(sessionId, projectId);

  // Kept current for the `meta` listener, which runs outside rendering.
  const screen = useRef({ sessionId, projectId, newChatTurns: new Set<string>() });
  useEffect(() => {
    screen.current = {
      sessionId,
      projectId,
      newChatTurns: new Set(
        live.filter((turn) => turn.sessionId === null).map((turn) => turn.clientTurnId),
      ),
    };
  });
  // A new chat still on screen takes its session's URL once `meta` names it, without remounting.
  useEffect(
    () =>
      turnStore.onSessionAssigned((turn) => {
        const current = screen.current;
        if (
          current.sessionId === null &&
          turn.sessionId !== null &&
          current.newChatTurns.has(turn.clientTurnId)
        ) {
          void navigate(`/chat/${turn.sessionId}`, {
            replace: true,
            state: { [KEEP_FOCUS]: true },
          });
        }
      }),
    [turnStore, navigate],
  );

  // The choice on `/chat` is mirrored to `?project=`.
  useEffect(() => {
    if (sessionId !== null || projects === null || projectId === projectParam) return;
    void navigate(projectId === null ? "/chat" : `/chat?project=${projectId}`, { replace: true });
  }, [navigate, sessionId, projects, projectId, projectParam]);

  const track = (handle: SendHandle, question: string) => {
    rememberAnchor(handle.clientTurnId, newestSeq(stored));
    setDraft(key, "");
    setDraftReason(key, null);
    void handle.settled.then((settlement) => {
      if (settlement.kind !== "rejected") return;
      setDraft(key, question);
      setDraftReason(key, questionReasonText(settlement.error));
    });
  };

  const send = (question: string) => {
    if (selected === null || blocked) return;
    track(turnStore.send({ projectId: selected.projectId, sessionId, question }), question);
  };

  const retry = (model: TurnModel) => {
    if (blocked) return;
    const handle =
      model.clientTurnId === null
        ? model.projectId === null
          ? null
          : turnStore.send({
              projectId: model.projectId,
              sessionId: model.sessionId,
              question: model.question,
            })
        : turnStore.retry(model.clientTurnId);
    if (handle !== null) track(handle, model.question);
  };

  const newChat = () => {
    void navigate(projectId === null ? "/chat" : `/chat?project=${projectId}`);
  };

  const openCitation = (model: TurnModel, citation: Citation, opener: HTMLElement) => {
    if (model.sessionId === null || model.turnId === null) return;
    setViewer({ target: { sessionId: model.sessionId, turnId: model.turnId, citation }, opener });
  };
  // The viewer belongs to its session: opening another session closes it.
  const shownViewer = viewer?.target.sessionId === sessionId ? viewer : null;
  const closeViewer = () => {
    const opener = viewer?.opener;
    setViewer(null);
    opener?.focus();
  };

  const pollSession = useCallback(
    () => session.refetch().then((result) => !result.isError),
    [session],
  );
  const onHeight = useCallback((height: number) => {
    setComposerHeight(height);
  }, []);

  const title = session.data?.title ?? sessions.find((item) => item.sessionId === sessionId)?.title;
  useDocumentTitle(sessionId === null || title === undefined ? en["chat.heading"].new : title);
  const heading =
    sessionId === null || title === undefined
      ? en["chat.heading"].new
      : fillText(en["chat.heading"].session, { title });

  const newest = models.at(-1);
  const answering = new Set(
    allLive.flatMap((turn) => (isSettled(turn) || turn.sessionId === null ? [] : [turn.sessionId])),
  );

  let empty: ReactNode = null;
  if (projects !== null && selected === null && sessionId === null) {
    empty = <Text className={styles.note}>{en["chat.project.chooseFirst"]}</Text>;
  } else if (notReady) {
    empty = (
      <MessageBar intent="info" layout="multiline">
        <MessageBarBody>{en["chat.project.notReady"]}</MessageBarBody>
      </MessageBar>
    );
  } else if (selected !== null && sessionId === null) {
    empty = <EmptyState project={selected} disabled={blocked} onSend={send} />;
  }

  let content: ReactNode;
  if (projects === null && projectsQuery.isError) {
    content = (
      <ApiErrorBar
        error={apiErrorOf(projectsQuery.error) ?? clientError("network")}
        lines={[en["chat.projects.error"]]}
        onRetry={() => void projectsQuery.refetch()}
      />
    );
  } else if (projects?.length === 0) {
    content = (
      <NoProjectsState isAdmin={user.isAdmin} onRefresh={() => void projectsQuery.refetch()} />
    );
  } else if (unavailable !== null) {
    content = <ChatUnavailable error={unavailable} onNewChat={newChat} />;
  } else {
    const loadError =
      sessionError === null ? null : (apiErrorOf(sessionError) ?? clientError("network"));
    content = (
      <Conversation
        sessionId={sessionId}
        turns={models}
        loading={sessionId !== null && session.data === undefined && !session.isError}
        loadError={loadError}
        onReload={() => void session.refetch()}
        earlier={earlier}
        empty={empty}
        sendingBlocked={blocked}
        composerHeight={composerHeight}
        onRetry={retry}
        onNewChat={newChat}
        onCitation={openCitation}
        pollSession={pollSession}
      />
    );
  }

  const panel = (inline: boolean) => (
    <SessionPanel
      sessions={sessionList}
      projects={projects ?? []}
      currentSessionId={sessionId}
      answering={answering}
      projectId={projectId}
      inline={inline}
      onNavigate={() => {
        setSessionsOpen(false);
      }}
    />
  );

  return (
    <div className={styles.root}>
      {breakpoint === "narrow" ? (
        <OverlayDrawer
          open={sessionsOpen}
          position="start"
          aria-label={en["chat.sessions.label"]}
          onOpenChange={(_, data) => {
            setSessionsOpen(data.open);
          }}
        >
          <div className={styles.overlayPanel}>{panel(false)}</div>
        </OverlayDrawer>
      ) : (
        panel(true)
      )}
      <div className={styles.main}>
        <ChatHeading>{heading}</ChatHeading>
        <div className={styles.pickerRow}>
          {breakpoint === "narrow" ? (
            <Button
              icon={<ChatMultiple20Regular />}
              onClick={() => {
                setSessionsOpen(true);
              }}
            >
              {en["chat.sessions.open"]}
            </Button>
          ) : null}
          <ProjectPicker
            projects={projects}
            selected={selected}
            inSession={sessionId !== null}
            failed={projectsQuery.isError}
            onSelect={(id) => {
              void navigate(`/chat?project=${id}`, { replace: sessionId === null });
            }}
          />
        </div>
        {content}
        <div className={styles.bottom}>
          {newest?.followups == null || unavailable !== null ? null : (
            <Followups items={newest.followups} disabled={blocked} onSend={send} />
          )}
          <Composer
            draftKey={key}
            disabled={composerDisabled}
            blocked={blocked}
            blockedUntil={waiting ? blockedUntil : null}
            stoppable={streamingTurn !== null}
            onSend={send}
            onStop={() => {
              if (streamingTurn !== null) turnStore.stop(streamingTurn.clientTurnId);
            }}
            onHeight={onHeight}
          />
        </div>
      </div>
      <CitationViewer
        target={shownViewer?.target ?? null}
        layout={breakpoint === "wide" ? "inline" : breakpoint === "medium" ? "medium" : "full"}
        onClose={closeViewer}
      />
    </div>
  );
}
