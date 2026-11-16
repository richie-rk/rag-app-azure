// Ingestion runs (`/admin/runs`, 10-web-client.md section 7.10): the index and state filters
// (`?indexId=`, `?state=`), the Auto-refresh switch while any shown run is queued or running, the
// table in the API's order and Show more. The list polls every 15,000 ms while a shown run is open
// (section 7.11), and a watched run that finishes is announced.

import {
  Dropdown,
  Field,
  Option,
  TableCell,
  TableRow,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { request } from "../../api/http";
import { apiErrorOf } from "../../api/problem";
import { displayNames, en } from "../../copy/en";
import { ADMIN_LIST_PAGE_SIZE, POLL_LIST_MS } from "../../platform/constants";
import { formatDateTime } from "../../ui/format";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { FilterRow, isCanonicalUuid, useUrlFilters, type UrlFilter } from "../shared/filters";
import { AutoRefreshSwitch, PollStatusBar, usePolling, usePollWatch } from "../shared/polling";
import {
  adminListQuery,
  allIndexesQuery,
  cursorQuery,
  itemsOf,
  type RunSummary,
} from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { RunProgress } from "./RunProgress";
import { isOpenRun, startedByText, useRunsFinishedAnnouncement } from "./runDisplay";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

/** The filters' value meaning "every index" and "every state"; never sent. */
const ALL = "all";

const RUN_STATES = [
  "queued",
  "running",
  "completed",
  "completed_with_errors",
  "failed",
] as const satisfies readonly RunSummary["state"][];
type RunStateFilter = (typeof RUN_STATES)[number] | typeof ALL;

const FILTERS: readonly UrlFilter[] = [
  { name: "indexId", allowed: isCanonicalUuid, fallback: ALL },
  { name: "state", allowed: RUN_STATES, fallback: ALL },
];

/** `adminListIngestionRuns` for one filter, `limit=50` a page, newest first. */
export function runsQuery(indexId: string, state: RunStateFilter) {
  return adminListQuery<RunSummary>("runs", { indexId, state }, (cursor, signal) =>
    request("adminListIngestionRuns", {
      query: {
        limit: ADMIN_LIST_PAGE_SIZE,
        ...(indexId === ALL ? {} : { indexId }),
        ...(state === ALL ? {} : { state }),
        ...cursorQuery(cursor),
      },
      signal,
    }),
  );
}

/** Section 7.11: the list polls while any shown run is queued or running. */
export function runsPoll(runs: readonly RunSummary[]): boolean {
  return runs.some((run) => isOpenRun(run.state));
}

export function RunsScreen() {
  const styles = useStyles();
  useDocumentTitle(en["runs.title"]);
  const { values, setFilter } = useUrlFilters(FILTERS);
  const indexId = values["indexId"] ?? ALL;
  const state = (values["state"] ?? ALL) as RunStateFilter;
  const polling = usePolling(POLL_LIST_MS);
  const list = useInfiniteQuery({
    ...runsQuery(indexId, state),
    refetchInterval: (query) => polling.interval(runsPoll(itemsOf(query.state.data))),
    refetchIntervalInBackground: false,
  });
  usePollWatch(polling, list);
  const runs = list.data === undefined ? undefined : itemsOf(list.data);
  useRunsFinishedAnnouncement(runs);
  const filtered = indexId !== ALL || state !== ALL;

  let content;
  if (list.isPending) {
    content = <TableSkeleton />;
  } else if (runs === undefined) {
    const error = apiErrorOf(list.error);
    content =
      error === null ? null : (
        <ApiErrorBar
          error={error}
          onRetry={() => {
            void list.refetch();
          }}
        />
      );
  } else if (runs.length === 0) {
    content = <Text>{filtered ? en["runs.noMatches"] : en["runs.empty"]}</Text>;
  } else {
    content = (
      <>
        {list.isFetchNextPageError ? null : <PollStatusBar polling={polling} result={list} />}
        <AdminTable
          caption={en["runs.title"]}
          headers={[
            en["runs.col.started"],
            en["runs.col.type"],
            en["runs.col.index"],
            en["runs.col.startedBy"],
            en["runs.col.progress"],
            en["runs.col.state"],
          ]}
        >
          {runs.map((run) => (
            <TableRow key={run.runId}>
              <TableCell>
                <RouterLink to={`/admin/runs/${run.runId}`}>
                  {formatDateTime(run.createdAt)}
                </RouterLink>
              </TableCell>
              <TableCell>{displayNames.runKind[run.kind]}</TableCell>
              <TableCell>{run.index.displayName}</TableCell>
              <TableCell>{startedByText(run)}</TableCell>
              <TableCell>
                <RunProgress counts={run.counts} />
              </TableCell>
              <TableCell>
                <StatusBadge kind="runState" value={run.state} />
              </TableCell>
            </TableRow>
          ))}
        </AdminTable>
        <ShowMore
          hasMore={list.hasNextPage}
          loading={list.isFetchingNextPage}
          error={list.isFetchNextPageError ? apiErrorOf(list.error) : null}
          onShowMore={() => {
            void list.fetchNextPage();
          }}
        />
      </>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader title={en["runs.title"]} />
      <FilterRow>
        <IndexFilter
          value={indexId}
          onChange={(value) => {
            setFilter("indexId", value);
          }}
        />
        <Field label={en["runs.filter.state"]}>
          <Dropdown
            value={state === ALL ? en["runs.filter.stateAll"] : displayNames.runState[state]}
            selectedOptions={[state]}
            onOptionSelect={(_, data) => {
              if (data.optionValue !== undefined) setFilter("state", data.optionValue);
            }}
          >
            <Option value={ALL}>{en["runs.filter.stateAll"]}</Option>
            {RUN_STATES.map((value) => (
              <Option key={value} value={value}>
                {displayNames.runState[value]}
              </Option>
            ))}
          </Dropdown>
        </Field>
      </FilterRow>
      {runsPoll(runs ?? []) ? <AutoRefreshSwitch polling={polling} /> : null}
      {content}
    </div>
  );
}

/** The index filter: `runs.filter.indexAll`, then every index from `adminListIndexes` in the API's
 * order. A chosen index the list doesn't hold (still loading, or deleted) shows no name. */
function IndexFilter({
  value,
  onChange,
}: {
  readonly value: string;
  readonly onChange: (value: string) => void;
}) {
  const indexes = useQuery(allIndexesQuery());
  const items = indexes.data ?? [];
  const chosen = items.find((index) => index.indexId === value);
  const shown = value === ALL ? en["runs.filter.indexAll"] : (chosen?.displayName ?? "");
  return (
    <Field label={en["runs.filter.index"]}>
      <Dropdown
        value={shown}
        selectedOptions={[value]}
        onOptionSelect={(_, data) => {
          if (data.optionValue !== undefined) onChange(data.optionValue);
        }}
      >
        <Option value={ALL}>{en["runs.filter.indexAll"]}</Option>
        {items.map((index) => (
          <Option key={index.indexId} value={index.indexId}>
            {index.displayName}
          </Option>
        ))}
      </Dropdown>
    </Field>
  );
}
