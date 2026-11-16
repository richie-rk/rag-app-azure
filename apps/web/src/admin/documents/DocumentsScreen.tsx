// Documents (`/admin/indexes/:indexId/documents`, 10-web-client.md section 7.9): the breadcrumb,
// the heading, Upload through project, Ingestion status, Upload documents, the Auto-refresh switch
// while a shown document is in progress, the filters (`?q=`, `?state=`), the table in the API's
// order with Re-ingest and Delete per row, Show more, the document panel (`?document=`) and the
// upload panel. The list polls every 15,000 ms while a shown document is in progress (7.11).

import {
  Button,
  Dropdown,
  Field,
  Link,
  Option,
  SkeletonItem,
  TableCell,
  TableRow,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ArrowSync20Regular, ArrowUpload20Regular, Delete20Regular } from "@fluentui/react-icons";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";

import type { ProjectRef } from "../../api/generated/types.gen";
import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ADMIN_LIST_PAGE_SIZE, POLL_LIST_MS } from "../../platform/constants";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { codePointLength, fillText, formatDateTime, formatSize } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { useRetryWaits } from "../../ui/retryWait";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable, RowActions, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { NotFoundBar } from "../shared/AdminStates";
import {
  FilterDropdown,
  FilterRow,
  FilterSearchBox,
  isCanonicalUuid,
  useUrlFilters,
  type UrlFilter,
} from "../shared/filters";
import { problemLines } from "../shared/formErrors";
import { AutoRefreshSwitch, PollStatusBar, usePolling, usePollWatch } from "../shared/polling";
import {
  adminIndexQuery,
  adminKeys,
  adminListQuery,
  cursorQuery,
  itemsOf,
  type DocumentSummary,
  type Page,
} from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { useAdminToast } from "../shared/toast";
import { DocumentPanel } from "./DocumentPanel";
import { UploadPanel, useUploadBatch } from "./UploadPanel";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  row: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "flex-end",
    columnGap: tokens.spacingHorizontalL,
    rowGap: tokens.spacingVerticalS,
  },
  project: { minWidth: "min(100%, 240px)" },
  name: { textAlign: "start", overflowWrap: "anywhere" },
  title: { width: "240px" },
});

/** The longest name filter `adminListDocuments` accepts, in code points. */
export const DOCUMENT_FILTER_MAX = 255;

const STATES = ["all", "ingested", "in_progress", "failed"] as const;
type StateFilter = (typeof STATES)[number];

const FILTERS: readonly UrlFilter[] = [
  {
    name: "q",
    allowed: (value) => value.trim() !== "" && codePointLength(value) <= DOCUMENT_FILTER_MAX,
    fallback: "",
  },
  { name: "state", allowed: STATES, fallback: "all" },
  { name: "document", allowed: isCanonicalUuid, fallback: "" },
];

/** `adminListDocuments` for one filter, `limit=50` a page, in the API's order. */
export function documentsQuery(indexId: string, q: string, state: StateFilter) {
  return adminListQuery<DocumentSummary>("documents", { indexId, q, state }, (cursor, signal) =>
    request("adminListDocuments", {
      path: { indexId },
      query: {
        limit: ADMIN_LIST_PAGE_SIZE,
        ...(q === "" ? {} : { q }),
        ...(state === "all" ? {} : { state }),
        ...cursorQuery(cursor),
      },
      signal,
    }),
  );
}

/** Section 7.11: the list polls while any shown document is in progress. */
export function documentsPoll(documents: readonly DocumentSummary[]): boolean {
  return documents.some((document) => document.displayState === "in_progress");
}

/** The project Upload through project starts with: the only one, when exactly one maps the
 * index. */
export function initialProject(projects: readonly ProjectRef[]): string | null {
  return projects.length === 1 ? (projects[0]?.projectId ?? null) : null;
}

/** The row that takes focus after `documentId` leaves the list: the next one, else the new last
 * one; null when no row remains (focus then goes to the caption). */
export function rowAfterRemoval(ids: readonly string[], documentId: string): string | null {
  const index = ids.indexOf(documentId);
  const rest = ids.filter((each) => each !== documentId);
  if (rest.length === 0 || index < 0) return null;
  return rest[Math.min(index, rest.length - 1)] ?? null;
}

export function DocumentsScreen() {
  const { indexId = "" } = useParams();
  // Another index starts from nothing: its own upload batch, dialogs and polling.
  return <DocumentsView key={indexId} indexId={indexId} />;
}

type Action = "reingest" | "delete";

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
}

function DocumentsView({ indexId }: { readonly indexId: string }) {
  const styles = useStyles();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { values, setFilter } = useUrlFilters(FILTERS);
  const q = values["q"] ?? "";
  const state = (values["state"] ?? "all") as StateFilter;
  const openDocument = values["document"] ?? "";
  const index = useQuery(adminIndexQuery(indexId));
  const polling = usePolling(POLL_LIST_MS);
  const listOptions = documentsQuery(indexId, q, state);
  const list = useInfiniteQuery({
    ...listOptions,
    refetchInterval: (query) => polling.interval(documentsPoll(itemsOf(query.state.data))),
    refetchIntervalInBackground: false,
  });
  usePollWatch(polling, list);
  const documents = list.data === undefined ? undefined : itemsOf(list.data);
  const batch = useUploadBatch(indexId);
  const [uploading, setUploading] = useState(false);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<{
    readonly action: Action;
    readonly document: DocumentSummary;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const captionRef = useRef<HTMLTableCaptionElement>(null);
  const pendingFocus = useRef<{ readonly removed: string; readonly next: string | null } | null>(
    null,
  );
  const lastOpened = useRef<string | null>(null);

  const indexName = index.data?.displayName;
  useDocumentTitle(en["index.documents"], ...(indexName === undefined ? [] : [indexName]));

  const projects = index.data?.mappedProjects;
  // Upload through project: the only mapped project is preselected; a choice that no longer maps
  // the index is dropped.
  const chosenProject =
    projects?.find((project) => project.projectId === (projectId ?? initialProject(projects))) ??
    null;

  // After a removal, once the rows are on screen, focus moves to the next row's name, or to the
  // caption when no row remains (section 11.4).
  useEffect(() => {
    const pending = pendingFocus.current;
    if (pending === null) return;
    if (documents?.some((each) => each.documentId === pending.removed) !== false) return;
    pendingFocus.current = null;
    const row =
      pending.next === null
        ? null
        : document.querySelector<HTMLElement>(`[data-document-name="${pending.next}"]`);
    // With no row left the table gives way to the Empty text, so the heading takes focus.
    (row ?? captionRef.current ?? document.querySelector<HTMLElement>("h1"))?.focus();
  });

  const refreshAfterRun = () => {
    void queryClient.invalidateQueries({ queryKey: adminKeys.documentLists });
    void queryClient.invalidateQueries({ queryKey: adminKeys.index(indexId) });
    void queryClient.invalidateQueries({ queryKey: adminKeys.runLists });
  };

  const failed = (action: Action, documentId: string, error: ApiError) => {
    waits.waitFor(`${action}:${documentId}`, error);
    const conflict = error.status === 412;
    setFailure({ error, lines: conflict ? [en["admin.conflict.action"]] : problemLines(error) });
    // A 412 shows the latest version, so the admin can check it and repeat the action.
    if (conflict) void list.refetch();
  };

  const reingest = async (target: DocumentSummary) => {
    setBusy(true);
    setFailure(null);
    try {
      const started = await request("adminReingestDocument", {
        path: { indexId, documentId: target.documentId },
        body: {},
        ifMatch: target.rowVersion,
      });
      queryClient.setQueryData<InfiniteData<Page<DocumentSummary>>>(listOptions.queryKey, (data) =>
        replaceDocument(data, started.document),
      );
      void queryClient.invalidateQueries({ queryKey: adminKeys.document(target.documentId) });
      refreshAfterRun();
      toast(fillText(en["documents.reingestStarted"], { name: target.name }), {
        label: en["index.viewProgress"],
        to: `/admin/runs/${started.runId}`,
      });
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") failed("reingest", target.documentId, error);
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  const remove = async (target: DocumentSummary) => {
    setBusy(true);
    setFailure(null);
    try {
      await request("adminDeleteDocument", {
        path: { indexId, documentId: target.documentId },
        ifMatch: target.rowVersion,
      });
      // Deleted documents are never listed, so the row goes at once.
      pendingFocus.current = {
        removed: target.documentId,
        next: rowAfterRemoval(
          (documents ?? []).map((each) => each.documentId),
          target.documentId,
        ),
      };
      queryClient.setQueryData<InfiniteData<Page<DocumentSummary>>>(listOptions.queryKey, (data) =>
        removeDocument(data, target.documentId),
      );
      queryClient.removeQueries({ queryKey: adminKeys.document(target.documentId) });
      refreshAfterRun();
      toast(fillText(en["documents.deleted"], { name: target.name }));
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") failed("delete", target.documentId, error);
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  const openPanel = (documentId: string) => {
    lastOpened.current = documentId;
    setFilter("document", documentId);
  };
  const closePanel = () => {
    setFilter("document", "");
    // Focus returns to the name that opened the panel.
    const opener = lastOpened.current;
    lastOpened.current = null;
    if (opener !== null) {
      setTimeout(() => {
        document.querySelector<HTMLElement>(`[data-document-name="${opener}"]`)?.focus();
      }, 0);
    }
  };

  const indexError = index.isError ? apiErrorOf(index.error) : null;
  if (index.data === undefined && indexError !== null && isProblem(indexError, 404)) {
    return (
      <div className={styles.root}>
        <PageHeader
          title={en["index.documents"]}
          breadcrumbs={[{ label: en["indexes.title"], to: "/admin/indexes" }]}
        />
        <NotFoundBar error={indexError} list={en["indexes.title"]} to="/admin/indexes" />
      </div>
    );
  }

  const filtered = q !== "" || state !== "all";
  let content;
  if (list.isPending) {
    content = <TableSkeleton />;
  } else if (documents === undefined) {
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
  } else if (documents.length === 0) {
    content = <Text>{filtered ? en["documents.noMatches"] : en["documents.empty"]}</Text>;
  } else {
    content = (
      <>
        {list.isFetchNextPageError ? null : <PollStatusBar polling={polling} result={list} />}
        {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
        <AdminTable
          caption={en["index.documents"]}
          captionRef={captionRef}
          headers={[
            en["documents.col.name"],
            en["documents.col.version"],
            en["documents.col.state"],
            en["documents.col.size"],
            en["documents.col.updated"],
            "",
          ]}
        >
          {documents.map((each) => (
            <TableRow key={each.documentId}>
              <TableCell>
                <Link
                  as="button"
                  className={styles.name}
                  data-document-name={each.documentId}
                  onClick={() => {
                    openPanel(each.documentId);
                  }}
                >
                  {each.name}
                </Link>
              </TableCell>
              <TableCell>{each.latestVersion?.versionNumber ?? en["admin.notSet"]}</TableCell>
              <TableCell>
                <StatusBadge kind="documentDisplayState" value={each.displayState} />
              </TableCell>
              <TableCell>
                {each.latestVersion === null
                  ? en["admin.notSet"]
                  : formatSize(each.latestVersion.sizeBytes)}
              </TableCell>
              <TableCell>{formatDateTime(each.updatedAt)}</TableCell>
              <TableCell>
                <RowActions
                  name={each.name}
                  actions={[
                    {
                      key: "reingest",
                      label: en["documents.reingest"],
                      icon: <ArrowSync20Regular />,
                      disabled: busy || waits.isWaiting(`reingest:${each.documentId}`),
                      onClick: () => {
                        setConfirming({ action: "reingest", document: each });
                      },
                    },
                    {
                      key: "delete",
                      label: en["documents.delete"],
                      icon: <Delete20Regular />,
                      disabled: busy || waits.isWaiting(`delete:${each.documentId}`),
                      onClick: () => {
                        setConfirming({ action: "delete", document: each });
                      },
                    },
                  ]}
                />
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

  const panelName = documents?.find((each) => each.documentId === openDocument)?.name ?? null;

  return (
    <div className={styles.root}>
      <PageHeader
        title={
          indexName === undefined ? (
            <SkeletonItem className={styles.title} size={24} />
          ) : (
            fillText(en["documents.title"], { index: indexName })
          )
        }
        breadcrumbs={[
          { label: en["indexes.title"], to: "/admin/indexes" },
          ...(indexName === undefined
            ? []
            : [{ label: indexName, to: `/admin/indexes/${indexId}` }]),
          { label: en["index.documents"] },
        ]}
      />
      {indexError === null ? null : (
        <ApiErrorBar
          error={indexError}
          onRetry={() => {
            void index.refetch();
          }}
        />
      )}
      <div className={styles.row}>
        <UploadThrough
          projects={projects}
          chosen={chosenProject}
          onChoose={(value) => {
            setProjectId(value);
          }}
        />
        <RouterLink to={`/admin/runs?indexId=${indexId}`}>{en["documents.status"]}</RouterLink>
        <Button
          appearance="primary"
          icon={<ArrowUpload20Regular />}
          disabled={chosenProject === null}
          onClick={() => {
            setUploading(true);
          }}
        >
          {en["documents.upload"]}
        </Button>
      </div>
      {documentsPoll(documents ?? []) ? <AutoRefreshSwitch polling={polling} /> : null}
      <FilterRow>
        <FilterSearchBox
          label={en["documents.filter"]}
          value={q}
          maxLength={DOCUMENT_FILTER_MAX}
          onSearch={(value) => {
            setFilter("q", value);
          }}
        />
        <FilterDropdown
          text={en["documents.stateFilter"]}
          values={STATES}
          value={state}
          onChange={(value) => {
            setFilter("state", value);
          }}
        />
      </FilterRow>
      {content}
      <DocumentPanel
        indexId={indexId}
        documentId={openDocument === "" ? null : openDocument}
        name={panelName}
        onClose={closePanel}
      />
      <UploadPanel
        open={uploading}
        indexId={indexId}
        project={chosenProject}
        batch={batch}
        onClose={() => {
          setUploading(false);
        }}
      />
      {confirming === null ? null : (
        <ConfirmDialog
          open
          {...(confirming.action === "delete"
            ? {
                title: fillText(en["documents.deleteConfirm.title"], {
                  name: confirming.document.name,
                }),
                body: en["documents.deleteConfirm.body"],
                actionLabel: en["documents.deleteConfirm.action"],
              }
            : {
                title: confirming.document.name,
                body: en["index.reingestConfirm.body"],
                actionLabel: en["documents.reingest"],
              })}
          busy={busy}
          onConfirm={() => {
            if (confirming.action === "delete") void remove(confirming.document);
            else void reingest(confirming.document);
          }}
          onCancel={() => {
            setConfirming(null);
          }}
        />
      )}
    </div>
  );
}

/** Upload through project: the projects that map the index, in the API's order (sorted by name),
 * with its hint; without any, `documents.project.none` and a link to Projects. Disabled while the
 * index loads. */
function UploadThrough({
  projects,
  chosen,
  onChoose,
}: {
  readonly projects: readonly ProjectRef[] | undefined;
  readonly chosen: ProjectRef | null;
  readonly onChoose: (projectId: string) => void;
}) {
  const styles = useStyles();
  if (projects?.length === 0) {
    return (
      <Text>
        {en["documents.project.none"]}{" "}
        <RouterLink to="/admin/projects" inline>
          {en["documents.project.noneLink"]}
        </RouterLink>
      </Text>
    );
  }
  return (
    <Field
      label={en["documents.project"]}
      hint={en["documents.project.hint"]}
      className={styles.project}
    >
      <Dropdown
        disabled={projects === undefined}
        value={chosen?.name ?? ""}
        selectedOptions={chosen === null ? [] : [chosen.projectId]}
        onOptionSelect={(_, data) => {
          if (data.optionValue !== undefined) onChoose(data.optionValue);
        }}
      >
        {(projects ?? []).map((project) => (
          <Option key={project.projectId} value={project.projectId}>
            {project.name}
          </Option>
        ))}
      </Dropdown>
    </Field>
  );
}

function replaceDocument(
  data: InfiniteData<Page<DocumentSummary>> | undefined,
  next: DocumentSummary,
): InfiniteData<Page<DocumentSummary>> | undefined {
  if (data === undefined) return data;
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((item) => (item.documentId === next.documentId ? next : item)),
    })),
  };
}

function removeDocument(
  data: InfiniteData<Page<DocumentSummary>> | undefined,
  documentId: string,
): InfiniteData<Page<DocumentSummary>> | undefined {
  if (data === undefined) return data;
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.filter((item) => item.documentId !== documentId),
    })),
  };
}
