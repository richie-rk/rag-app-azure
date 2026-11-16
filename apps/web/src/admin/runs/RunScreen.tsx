// Ingestion run (`/admin/runs/:runId`, 10-web-client.md section 7.10): `adminGetIngestionRun`
// with `itemsLimit=100`; the breadcrumb (Ingestion runs › started time), the definition list, the
// progress bar of done over total, the Auto-refresh switch while the run is queued or running, the
// items table and Show more, which passes `itemsCursor`. The page polls every 5,000 ms while the
// run is open (section 7.11), and announces the run when it finishes.

import {
  Field,
  ProgressBar,
  TableCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { infiniteQueryOptions, useInfiniteQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useParams } from "react-router";

import { request } from "../../api/http";
import { apiErrorOf, isProblem } from "../../api/problem";
import { displayNames, en } from "../../copy/en";
import { POLL_RUN_MS, RUN_ITEMS_PAGE_SIZE } from "../../platform/constants";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { formatDateTime } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { AdminTable, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { NotFoundBar } from "../shared/AdminStates";
import { AutoRefreshSwitch, PollStatusBar, usePolling, usePollWatch } from "../shared/polling";
import { adminKeys, type IngestionRunDetail, type RunSummary } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { RunProgress } from "./RunProgress";
import {
  doneCount,
  isOpenRun,
  itemName,
  problemText,
  startedByText,
  useRunsFinishedAnnouncement,
} from "./runDisplay";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  fields: {
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
  heading: {
    margin: "0",
    fontSize: tokens.fontSizeBase400,
    lineHeight: tokens.lineHeightBase400,
    fontWeight: tokens.fontWeightSemibold,
  },
  cell: { overflowWrap: "anywhere" },
});

/** `adminGetIngestionRun` with `itemsLimit=100`; each further page of items follows
 * `nextItemsCursor`. */
export function runQuery(runId: string) {
  return infiniteQueryOptions({
    queryKey: adminKeys.run(runId),
    queryFn: ({ pageParam, signal }) =>
      request("adminGetIngestionRun", {
        path: { runId },
        query: {
          itemsLimit: RUN_ITEMS_PAGE_SIZE,
          ...(pageParam === null ? {} : { itemsCursor: pageParam }),
        },
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last: IngestionRunDetail) => last.nextItemsCursor,
  });
}

/** The run as the newest page read it: every page carries it, and a poll reads them in order. */
function runOf(pages: readonly IngestionRunDetail[] | undefined): RunSummary | undefined {
  return pages?.at(-1)?.run;
}

export function RunScreen() {
  const { runId = "" } = useParams();
  // Another run starts from nothing: its own polling and announcements.
  return <RunView key={runId} runId={runId} />;
}

function RunView({ runId }: { readonly runId: string }) {
  const styles = useStyles();
  useDocumentTitle(en["run.title"]);
  const polling = usePolling(POLL_RUN_MS);
  const query = useInfiniteQuery({
    ...runQuery(runId),
    refetchInterval: (current) => {
      const run = runOf(current.state.data?.pages);
      return polling.interval(run !== undefined && isOpenRun(run.state));
    },
    refetchIntervalInBackground: false,
  });
  usePollWatch(polling, query);
  const run = runOf(query.data?.pages);
  useRunsFinishedAnnouncement(run === undefined ? undefined : [run]);
  const items = (query.data?.pages ?? []).flatMap((page) => page.items);

  let body;
  if (run === undefined) {
    const error = query.isError ? apiErrorOf(query.error) : null;
    if (error === null) body = <TableSkeleton />;
    else if (isProblem(error, 404)) {
      body = <NotFoundBar error={error} list={en["runs.title"]} to="/admin/runs" />;
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
    const fields: [string, ReactNode][] = [
      [en["run.field.state"], <StatusBadge key="state" kind="runState" value={run.state} />],
      [en["run.field.type"], displayNames.runKind[run.kind]],
      [en["run.field.index"], run.index.displayName],
      [en["run.field.startedBy"], startedByText(run)],
      [en["run.field.started"], formatDateTime(run.createdAt)],
      [
        en["run.field.finished"],
        run.completedAt === null ? en["run.notFinished"] : formatDateTime(run.completedAt),
      ],
    ];
    body = (
      <>
        {query.isFetchNextPageError ? null : <PollStatusBar polling={polling} result={query} />}
        {isOpenRun(run.state) ? <AutoRefreshSwitch polling={polling} /> : null}
        <dl className={styles.fields}>
          {fields.map(([term, value]) => (
            <div key={term} className={styles.pair}>
              <dt className={styles.term}>{term}</dt>
              <dd className={styles.definition}>{value}</dd>
            </div>
          ))}
        </dl>
        <Field label={<RunProgress counts={run.counts} />}>
          <ProgressBar
            max={Math.max(1, run.counts.total)}
            value={Math.min(doneCount(run.counts), run.counts.total)}
          />
        </Field>
        <h2 className={styles.heading}>{en["run.items"]}</h2>
        <AdminTable
          caption={en["run.items"]}
          headers={[
            en["run.col.document"],
            en["run.col.version"],
            en["run.col.stage"],
            en["run.col.state"],
            en["run.col.attempts"],
            en["run.col.problem"],
          ]}
        >
          {items.map((item) => (
            <TableRow key={item.itemId}>
              <TableCell className={styles.cell}>{itemName(item)}</TableCell>
              <TableCell>{item.versionNumber ?? ""}</TableCell>
              <TableCell>{item.stage === null ? "" : displayNames.itemStage[item.stage]}</TableCell>
              <TableCell>
                <StatusBadge kind="itemState" value={item.state} />
              </TableCell>
              <TableCell>{item.attempts}</TableCell>
              <TableCell className={styles.cell}>
                {problemText(item.outcomeMessage, item.outcomeCode)}
              </TableCell>
            </TableRow>
          ))}
        </AdminTable>
        <ShowMore
          hasMore={query.hasNextPage}
          loading={query.isFetchingNextPage}
          error={query.isFetchNextPageError ? apiErrorOf(query.error) : null}
          onShowMore={() => {
            void query.fetchNextPage();
          }}
        />
      </>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader
        title={en["run.title"]}
        breadcrumbs={[
          { label: en["runs.title"], to: "/admin/runs" },
          ...(run === undefined ? [] : [{ label: formatDateTime(run.createdAt) }]),
        ]}
      />
      {body}
    </div>
  );
}
