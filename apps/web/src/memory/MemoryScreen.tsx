// Memory (`/memory`, 10-web-client.md section 7.3), a lazy module: the intro, the Use memory switch
// and Clear all, the usage line, the information bar when memory is off or paused, and the saved
// memories with Edit and Delete. Every write sends the latest `etag` as `If-Match`, and its answer
// replaces the whole cached memory, the usage line included (`13-conversations-and-memory.md`
// section 7.3).

import {
  Button,
  Field,
  List,
  MessageBar,
  MessageBarBody,
  ProgressBar,
  Skeleton,
  SkeletonItem,
  Switch,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { Delete20Regular } from "@fluentui/react-icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { announceStatus } from "../a11y/announce";
import { request } from "../api/http";
import type { ResponseOf } from "../api/operations";
import { apiErrorOf, isProblem, retryAtOf, splitFieldErrors, type ApiError } from "../api/problem";
import { queryKeys } from "../api/queryClient";
import { en } from "../copy/en";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { ApiErrorBar, ErrorBar, errorText } from "../ui/ErrorBar";
import { fillText, formatPercent, formatRetryTime, pluralText } from "../ui/format";
import { PageHeader, useDocumentTitle } from "../ui/PageHeader";
import { useRetryWaits } from "../ui/retryWait";
import { FactItem, type FactDraft, type FactFieldErrors, type FactView } from "./FactItem";
import { factReasonText, factTextReason, usageColor } from "./memoryText";

export type MemoryView = ResponseOf<"getMemory">;

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalL,
    maxWidth: "800px",
  },
  controls: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalS,
  },
  usage: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  list: { display: "flex", flexDirection: "column", margin: "0", padding: "0" },
  skeleton: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalL },
});

/** `getMemory`, fresh for 0 s and refetched on window focus (section 5.8). */
export function memoryQuery() {
  return {
    queryKey: queryKeys.memory,
    queryFn: ({ signal }: { signal: AbortSignal }) => request("getMemory", { signal }),
  };
}

/** Where a failure shows: `controls` (under the switch row), or a memory's `factId`. */
type Scope = string;

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
}

type Confirming = { readonly kind: "clear" } | { readonly kind: "delete"; readonly fact: FactView };

export function MemoryScreen() {
  const styles = useStyles();
  const queryClient = useQueryClient();
  const waits = useRetryWaits();
  useDocumentTitle(en["memory.title"]);
  const memory = useQuery(memoryQuery());
  const view = memory.data;
  const [openDraft, setDraft] = useState<FactDraft | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FactFieldErrors>({});
  const [failures, setFailures] = useState<ReadonlyMap<Scope, Failure>>(new Map());
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [confirming, setConfirming] = useState<Confirming | null>(null);
  // An open editor whose memory is gone (deleted elsewhere, found by a refetch) is closed.
  const draft =
    openDraft !== null && view?.facts.some((fact) => fact.factId === openDraft.factId) === true
      ? openDraft
      : null;
  // After a save the editor closes, and focus returns to that memory's Edit button as soon as
  // the list shows it again.
  const focusEditOf = useRef<string | null>(null);
  useEffect(() => {
    const factId = focusEditOf.current;
    if (factId === null || draft !== null) return;
    const button = document.querySelector<HTMLElement>(`[data-fact-edit="${factId}"]`);
    if (button === null) return;
    focusEditOf.current = null;
    button.focus();
  });

  const setFailure = (scope: Scope, failure: Failure | null) => {
    setFailures((current) => {
      const next = new Map(current);
      if (failure === null) next.delete(scope);
      else next.set(scope, failure);
      return next;
    });
  };
  const setBusyKey = (key: string, on: boolean) => {
    setBusy((current) => {
      const next = new Set(current);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  interface Write {
    readonly scope: Scope;
    /** The control that sent it, disabled while it runs and until a `429`'s time. */
    readonly control: string;
    readonly send: (etag: string) => Promise<MemoryView>;
    readonly done: string;
    readonly onDone?: () => void;
    /** Save only: a `422` goes to the editor's fields, a `503` keeps the editor for a retry. */
    readonly save?: boolean;
  }

  /** One memory write and the answers of section 7.3's table. */
  const write = async ({ scope, control, send, done, onDone, save = false }: Write) => {
    const etag = queryClient.getQueryData<MemoryView>(queryKeys.memory)?.etag;
    if (etag === undefined) return;
    setBusyKey(control, true);
    setFailure(scope, null);
    try {
      const next = await send(etag);
      queryClient.setQueryData(queryKeys.memory, next);
      announceStatus(done);
      onDone?.();
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return;
      waits.waitFor(control, error);
      if (error.status === 412) {
        // The latest version shows; an open editor keeps its text while its memory exists.
        await memory.refetch();
        setFailure(scope, { error, lines: [en["memory.conflict"]] });
      } else if (isProblem(error, 404, "memory_fact_not_found")) {
        await memory.refetch();
        setFailure(scope, { error, lines: [en["memory.factGone"]] });
      } else if (save && error.status === 422) {
        const split = splitFieldErrors(error, ["text", "category"]);
        const max = view?.limits.maxFactChars ?? 200;
        const textReason = split.byField.get("text");
        const categoryReason = split.byField.get("category");
        setFieldErrors({
          ...(textReason === undefined ? {} : { text: factReasonText(textReason, max) }),
          ...(categoryReason === undefined
            ? {}
            : { category: factReasonText(categoryReason, max) }),
        });
        if (split.unmatched.length > 0) {
          setFailure(scope, {
            error,
            lines: split.unmatched.map((entry) => `${entry.path}: ${entry.reason}`),
          });
        }
      } else if (save && isProblem(error, 503, "dependency_unavailable")) {
        setFailure(scope, { error, lines: [en["memory.saveUnavailable"]] });
      } else {
        setFailure(scope, {
          error,
          lines: error.status === 428 ? [en["error.defect.body"]] : [],
        });
      }
    } finally {
      setBusyKey(control, false);
    }
  };

  const save = (current: FactDraft) => {
    const max = view?.limits.maxFactChars ?? 200;
    const blocked = factTextReason(current.text, max);
    if (blocked !== null) {
      setFieldErrors({ text: factReasonText(blocked, max) });
      return;
    }
    setFieldErrors({});
    void write({
      scope: current.factId,
      control: `save:${current.factId}`,
      save: true,
      done: en["memory.saved"],
      send: (etag) =>
        request("updateMemoryFact", {
          path: { factId: current.factId },
          body: { text: current.text.trim(), category: current.category },
          ifMatch: etag,
        }),
      onDone: () => {
        setDraft(null);
        focusEditOf.current = current.factId;
      },
    });
  };

  const confirm = () => {
    if (confirming === null) return;
    if (confirming.kind === "clear") {
      void write({
        scope: "controls",
        control: "clear",
        done: en["memory.cleared"],
        send: (etag) => request("clearMemory", { ifMatch: etag }),
      }).finally(() => {
        setConfirming(null);
      });
      return;
    }
    const { fact } = confirming;
    void write({
      scope: fact.factId,
      control: `delete:${fact.factId}`,
      done: en["memory.deleted"],
      send: (etag) => request("deleteMemoryFact", { path: { factId: fact.factId }, ifMatch: etag }),
    }).finally(() => {
      setConfirming(null);
    });
  };

  const factIds = new Set(view?.facts.map((fact) => fact.factId) ?? []);
  // A failure whose memory is gone shows under the switch row.
  const topFailures = [...failures].filter(
    ([scope]) => scope === "controls" || (view !== undefined && !factIds.has(scope)),
  );
  const loading = view === undefined;
  const controlsDisabled = (control: string) =>
    loading || busy.has(control) || waits.isWaiting(control);

  let content;
  if (view !== undefined) {
    const { limits } = view;
    const percent = formatPercent(limits.usedTokens, limits.maxTokens);
    content = (
      <>
        <div className={styles.usage}>
          <Text>
            {pluralText(en["memory.usage.facts"], limits.usedFacts, { max: limits.maxFacts })}
          </Text>
          <Field
            label={en["memory.usage.space"]}
            hint={fillText(en["memory.usage.spaceValue"], { percent })}
          >
            <ProgressBar
              value={Math.min(1, limits.usedTokens / limits.maxTokens)}
              color={usageColor(limits.usedTokens, limits.maxTokens)}
              data-usage-color={usageColor(limits.usedTokens, limits.maxTokens)}
            />
          </Field>
        </div>
        {!view.enabled ? (
          <MessageBar intent="info" layout="multiline">
            <MessageBarBody>{en["memory.disabledBar"]}</MessageBarBody>
          </MessageBar>
        ) : view.paused ? (
          <MessageBar intent="info" layout="multiline">
            <MessageBarBody>{en["memory.pausedBar"]}</MessageBarBody>
          </MessageBar>
        ) : null}
        {view.facts.length === 0 ? (
          <Text>{en["memory.empty"]}</Text>
        ) : (
          <List aria-label={en["memory.list"]} className={styles.list}>
            {view.facts.map((fact) => {
              const editing = draft?.factId === fact.factId ? draft : null;
              const failure = failures.get(fact.factId);
              return (
                <FactItem
                  key={fact.factId}
                  fact={fact}
                  draft={editing}
                  fieldErrors={editing === null ? {} : fieldErrors}
                  saving={busy.has(`save:${fact.factId}`)}
                  saveDisabled={waits.isWaiting(`save:${fact.factId}`)}
                  deleteDisabled={controlsDisabled(`delete:${fact.factId}`)}
                  failure={
                    failure === undefined ? null : (
                      <ApiErrorBar error={failure.error} lines={failure.lines} />
                    )
                  }
                  onEdit={() => {
                    setFieldErrors({});
                    setFailure(fact.factId, null);
                    setDraft({ factId: fact.factId, text: fact.text, category: fact.category });
                  }}
                  onDraft={setDraft}
                  onSave={() => {
                    if (editing !== null) save(editing);
                  }}
                  onCancel={() => {
                    setDraft(null);
                    setFieldErrors({});
                    setFailure(fact.factId, null);
                  }}
                  onDelete={() => {
                    setConfirming({ kind: "delete", fact });
                  }}
                />
              );
            })}
          </List>
        )}
      </>
    );
  } else if (memory.isError) {
    content = <LoadError error={apiErrorOf(memory.error)} onRetry={() => void memory.refetch()} />;
  } else {
    content = (
      <Skeleton className={styles.skeleton}>
        {[0, 1, 2, 3].map((item) => (
          <SkeletonItem key={item} size={48} />
        ))}
      </Skeleton>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader title={en["memory.title"]} />
      <Text>{en["memory.intro"]}</Text>
      <div className={styles.controls}>
        <Switch
          label={en["memory.toggle"]}
          checked={view !== undefined && !view.paused}
          disabled={controlsDisabled("switch")}
          onChange={(_, data) => {
            void write({
              scope: "controls",
              control: "switch",
              done: data.checked ? en["memory.resumedDone"] : en["memory.pausedDone"],
              send: (etag) =>
                request("updateMemorySettings", { body: { paused: !data.checked }, ifMatch: etag }),
            });
          }}
        />
        <Button
          icon={<Delete20Regular />}
          disabled={controlsDisabled("clear") || view?.facts.length === 0}
          onClick={() => {
            setConfirming({ kind: "clear" });
          }}
        >
          {en["memory.clearAll"]}
        </Button>
      </div>
      {topFailures.map(([scope, failure]) => (
        <ApiErrorBar key={scope} error={failure.error} lines={failure.lines} />
      ))}
      {content}
      <ConfirmDialog
        open={confirming !== null}
        title={
          confirming?.kind === "clear"
            ? en["memory.clearConfirm.title"]
            : en["memory.deleteConfirm.title"]
        }
        body={
          confirming?.kind === "clear"
            ? en["memory.clearConfirm.body"]
            : en["memory.deleteConfirm.body"]
        }
        actionLabel={
          confirming?.kind === "clear"
            ? en["memory.clearConfirm.action"]
            : en["memory.deleteConfirm.action"]
        }
        busy={busy.has("clear") || [...busy].some((key) => key.startsWith("delete:"))}
        onConfirm={confirm}
        onCancel={() => {
          setConfirming(null);
        }}
      />
    </div>
  );
}

/** The Error state: `memory.loadError` with the failure's text, the reference ID and Retry. */
function LoadError({
  error,
  onRetry,
}: {
  readonly error: ApiError | null;
  readonly onRetry: () => void;
}) {
  const text = error === null ? null : errorText(error);
  if (error === null || text === null) return null;
  const retryAt = retryAtOf(error);
  return (
    <ErrorBar
      title={en["memory.loadError"]}
      body={text.title}
      lines={[
        ...(text.body === "" ? [] : [text.body]),
        ...(retryAt === null
          ? []
          : [fillText(en["error.retryAt"], { time: formatRetryTime(retryAt) })]),
      ]}
      referenceId={error.referenceId}
      actions={<Button onClick={onRetry}>{en["error.retry"]}</Button>}
    />
  );
}
