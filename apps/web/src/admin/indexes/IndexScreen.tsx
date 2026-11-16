// Index (`/admin/indexes/:indexId`, 10-web-client.md section 7.8): the breadcrumb, the name with
// its status badge, the re-ingest banner, the summary, the rebuild progress, Edit settings, Delete
// index when allowed, the links to its documents and runs, and the Auto-refresh switch while the
// page polls (every 15,000 ms while a run is open or the index is working, section 7.11).

import {
  Button,
  Field,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  ProgressBar,
  SkeletonItem,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ArrowSync20Regular, Delete20Regular, Edit20Regular } from "@fluentui/react-icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router";

import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { displayNames, en } from "../../copy/en";
import { POLL_LIST_MS } from "../../platform/constants";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { useRetryWaits } from "../../ui/retryWait";
import { RouterLink } from "../../ui/RouterLink";
import { LoadErrorBar, NotFoundBar } from "../shared/AdminStates";
import { problemLines } from "../shared/formErrors";
import { describeParameters } from "../shared/ParamForm";
import { AutoRefreshSwitch, PollStatusBar, usePolling, usePollWatch } from "../shared/polling";
import {
  adminIndexQuery,
  adminKeys,
  indexOptionsQuery,
  type IndexDetail,
  type IndexOptions,
  type IndexStatus,
} from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { useAdminToast } from "../shared/toast";
import { EditIndexSettings } from "./EditIndexSettings";
import { embeddingText } from "./IndexesScreen";
import { methodOf, methodValue, schemaOf } from "./indexForm";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  title: { display: "inline-flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
  skeleton: { width: "240px" },
  summary: {
    display: "grid",
    gridTemplateColumns: "max-content minmax(0, 1fr)",
    columnGap: tokens.spacingHorizontalL,
    rowGap: tokens.spacingVerticalS,
    margin: "0",
  },
  // Each term and its definition sit in one wrapper, laid out as cells of the list's grid.
  pair: { display: "contents" },
  term: { fontWeight: tokens.fontWeightSemibold },
  definition: { margin: "0", overflowWrap: "anywhere" },
  settings: {
    display: "flex",
    flexDirection: "column",
    margin: "0",
    padding: "0",
    listStyle: "none",
  },
  row: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalS,
  },
});

/** The statuses during which the index changes by itself. */
const WORKING: ReadonlySet<IndexStatus> = new Set([
  "provisioning",
  "reingesting",
  "rebuilding",
  "deleting",
]);

/** Section 7.11: the Index page polls while a run is open or the index is working. */
export function indexPolls(index: IndexDetail | undefined): boolean {
  return index !== undefined && (index.openRun !== null || WORKING.has(index.status));
}

/** Delete index is offered only when no project maps the index and `adminDeleteIndex` accepts its
 * status (`11-api.md` section 3.10.6). */
export function canDeleteIndex(index: IndexDetail): boolean {
  return (
    index.mappedProjects.length === 0 &&
    (index.status === "ready" || index.status === "reingest_required" || index.status === "failed")
  );
}

/** `index.summary.documents`: the live documents, then the counts by state. */
export function documentCountsText(counts: IndexDetail["documentCounts"]): string {
  const names = displayNames.documentDisplayState;
  return `${String(counts.live)} (${names.ingested}: ${String(counts.ingested)}, ${names.in_progress}: ${String(counts.inProgress)}, ${names.failed}: ${String(counts.failed)})`;
}

export function IndexScreen() {
  const { indexId = "" } = useParams();
  // Another index starts from nothing: its own editor, dialogs and polling.
  return <IndexView key={indexId} indexId={indexId} />;
}

type Action = "reingest" | "delete";

interface Failure {
  readonly action: Action;
  readonly error: ApiError;
  readonly lines: readonly string[];
}

function IndexView({ indexId }: { readonly indexId: string }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const polling = usePolling(POLL_LIST_MS);
  const query = useQuery({
    ...adminIndexQuery(indexId),
    refetchInterval: (current) => polling.interval(indexPolls(current.state.data)),
    refetchIntervalInBackground: false,
  });
  usePollWatch(polling, query);
  const options = useQuery(indexOptionsQuery());
  const optionsError = options.isError ? apiErrorOf(options.error) : null;
  const index = query.data;
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  useDocumentTitle(...(index === undefined ? [] : [index.displayName]), en["indexes.title"]);

  const failed = (action: Action, error: ApiError) => {
    waits.waitFor(action, error);
    const conflict = error.status === 412;
    setFailure({
      action,
      error,
      lines: conflict ? [en["admin.conflict.action"]] : problemLines(error),
    });
    // A 412 shows the latest version, so the admin can check it and repeat the action.
    if (conflict) void query.refetch();
  };

  const reingest = async (current: IndexDetail) => {
    setBusy(true);
    setFailure(null);
    try {
      const started = await request("adminStartReingest", {
        path: { indexId },
        body: {},
        ifMatch: current.rowVersion,
      });
      queryClient.setQueryData(adminKeys.index(indexId), started.index);
      void queryClient.invalidateQueries({ queryKey: adminKeys.indexLists });
      toast(en["index.reingestStarted"], {
        label: en["index.viewProgress"],
        to: `/admin/runs/${started.runId}`,
      });
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") failed("reingest", error);
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  const remove = async (current: IndexDetail) => {
    setBusy(true);
    setFailure(null);
    try {
      await request("adminDeleteIndex", { path: { indexId }, ifMatch: current.rowVersion });
      void queryClient.invalidateQueries({ queryKey: adminKeys.indexLists });
      queryClient.removeQueries({ queryKey: adminKeys.index(indexId) });
      void navigate("/admin/indexes");
      toast(fillText(en["index.deleted"], { name: current.displayName }));
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") failed("delete", error);
      setBusy(false);
      setConfirming(null);
    }
  };

  const failureFor = (action: Action) =>
    failure?.action === action ? <ApiErrorBar error={failure.error} lines={failure.lines} /> : null;

  let body;
  if (index === undefined) {
    const error = query.isError ? apiErrorOf(query.error) : null;
    if (error === null) body = <SkeletonItem size={32} />;
    else if (isProblem(error, 404)) {
      body = <NotFoundBar error={error} list={en["indexes.title"]} to="/admin/indexes" />;
    } else {
      body = (
        <ApiErrorBar
          error={error}
          onRetry={() => {
            void query.refetch();
          }}
        />
      );
    }
  } else {
    body = (
      <>
        <PollStatusBar polling={polling} result={query} />
        {indexPolls(index) ? <AutoRefreshSwitch polling={polling} /> : null}
        {index.reingestRequired ? (
          <MessageBar intent="warning" layout="multiline">
            <MessageBarBody>
              <MessageBarTitle>{en["index.reingestBanner.title"]}</MessageBarTitle>
              {en["index.reingestBanner.body"]}
            </MessageBarBody>
            <MessageBarActions>
              <Button
                icon={<ArrowSync20Regular />}
                disabled={busy || waits.isWaiting("reingest")}
                onClick={() => {
                  setConfirming("reingest");
                }}
              >
                {en["index.reingest"]}
              </Button>
            </MessageBarActions>
          </MessageBar>
        ) : null}
        {failureFor("reingest")}
        <IndexSummary index={index} options={options.data} />
        {index.rebuildProgress === null ? null : (
          <Field
            label={fillText(en["index.rebuildProgress"], {
              ready: index.rebuildProgress.servedCurrent,
              total: index.rebuildProgress.liveDocuments,
            })}
          >
            <ProgressBar
              max={Math.max(1, index.rebuildProgress.liveDocuments)}
              value={Math.min(
                index.rebuildProgress.servedCurrent,
                index.rebuildProgress.liveDocuments,
              )}
            />
          </Field>
        )}
        <div className={styles.row}>
          {editing ? null : (
            <Button
              icon={<Edit20Regular />}
              onClick={() => {
                setEditing(true);
              }}
            >
              {en["index.edit"]}
            </Button>
          )}
          {canDeleteIndex(index) ? (
            <Button
              icon={<Delete20Regular />}
              disabled={busy || waits.isWaiting("delete")}
              onClick={() => {
                setConfirming("delete");
              }}
            >
              {en["index.delete"]}
            </Button>
          ) : null}
          <RouterLink to={`/admin/indexes/${indexId}/documents`}>
            {en["index.documents"]}
          </RouterLink>
          <RouterLink to={`/admin/runs?indexId=${indexId}`}>{en["index.runs"]}</RouterLink>
        </div>
        {failureFor("delete")}
        {editing ? (
          options.data !== undefined ? (
            <EditIndexSettings
              index={index}
              options={options.data}
              onClose={() => {
                setEditing(false);
              }}
            />
          ) : optionsError !== null ? (
            <LoadErrorBar
              title={en["index.optionsError"]}
              error={optionsError}
              onRetry={() => {
                void options.refetch();
              }}
            />
          ) : (
            <SkeletonItem size={32} />
          )
        ) : null}
        <ConfirmDialog
          open={confirming === "reingest"}
          title={fillText(en["index.reingestConfirm.title"], { name: index.displayName })}
          body={en["index.reingestConfirm.body"]}
          actionLabel={en["index.reingestConfirm.action"]}
          busy={busy}
          onConfirm={() => {
            void reingest(index);
          }}
          onCancel={() => {
            setConfirming(null);
          }}
        />
        <ConfirmDialog
          open={confirming === "delete"}
          title={fillText(en["index.deleteConfirm.title"], { name: index.displayName })}
          body={en["index.deleteConfirm.body"]}
          actionLabel={en["index.deleteConfirm.action"]}
          busy={busy}
          onConfirm={() => {
            void remove(index);
          }}
          onCancel={() => {
            setConfirming(null);
          }}
        />
      </>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader
        title={
          index === undefined ? (
            <SkeletonItem className={styles.skeleton} size={24} />
          ) : (
            <span className={styles.title}>
              {index.displayName}
              <StatusBadge kind="indexStatus" value={index.status} />
            </span>
          )
        }
        breadcrumbs={[
          { label: en["indexes.title"], to: "/admin/indexes" },
          ...(index === undefined ? [] : [{ label: index.displayName }]),
        ]}
      />
      {body}
    </div>
  );
}

/** The summary definition list (`index.summary.*`). Setting titles and the language's label come
 * from the index options when they have loaded; until then the raw names show. */
function IndexSummary({
  index,
  options,
}: {
  readonly index: IndexDetail;
  readonly options: IndexOptions | undefined;
}) {
  const styles = useStyles();
  const method =
    options === undefined
      ? undefined
      : methodOf(options, methodValue(index.method.key, index.method.version));
  const settings = describeParameters(schemaOf(method), index.method.parameters);
  const language =
    options?.languageAnalyzers.find((each) => each.value === index.languageAnalyzer)?.label ??
    index.languageAnalyzer;
  const rows: [string, ReactNode][] = [
    [en["index.summary.method"], index.method.displayName],
    [
      en["index.summary.settings"],
      <ul key="settings" className={styles.settings}>
        {settings.map((setting) => (
          <li key={setting.name}>{`${setting.title}: ${setting.value}`}</li>
        ))}
      </ul>,
    ],
    [en["index.summary.embeddingModel"], embeddingText(index.embedding)],
    [en["index.summary.language"], language],
    [
      en["index.summary.status"],
      <StatusBadge key="status" kind="indexStatus" value={index.status} />,
    ],
    [en["index.summary.documents"], documentCountsText(index.documentCounts)],
  ];
  return (
    <dl className={styles.summary}>
      {rows.map(([term, value]) => (
        <div key={term} className={styles.pair}>
          <dt className={styles.term}>{term}</dt>
          <dd className={styles.definition}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
