// The upload panel (10-web-client.md section 10): an overlay drawer at the end, size `large`, for
// the index and the project chosen in Upload through project. It holds Choose files (a file input
// behind a button), a drop zone that nothing needs dragging, the results table with each update's
// checkbox and each file's progress, the shared-index warning directly above Upload, Upload and
// Stop uploading, the summary and the batch's error bar. The batch itself is `UploadBatch`.

import {
  Button,
  Checkbox,
  DrawerBody,
  DrawerHeader,
  DrawerHeaderTitle,
  Field,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  OverlayDrawer,
  ProgressBar,
  Spinner,
  TableCell,
  TableRow,
  Text,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components";
import {
  ArrowUpload20Regular,
  Dismiss24Regular,
  DocumentAdd20Regular,
  Stop20Regular,
} from "@fluentui/react-icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
  type DragEvent,
} from "react";

import type { ProjectRef, UploadLimits } from "../../api/generated/types.gen";
import { apiErrorOf, retryAtOf } from "../../api/problem";
import { useWaiting } from "../../chat/turns/time";
import { en } from "../../copy/en";
import { ApiErrorBar, ErrorBar } from "../../ui/ErrorBar";
import { FocusFirstControl } from "../../ui/FocusFirstControl";
import { fillText, pluralText } from "../../ui/format";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable } from "../shared/AdminTable";
import { LoadErrorBar } from "../shared/AdminStates";
import { adminIndexQuery, adminKeys, indexOptionsQuery } from "../shared/queries";
import {
  UploadBatch,
  chosenCount,
  httpUploadApi,
  nothingToUpload,
  type UploadRow,
  type UploadState,
} from "./uploadBatch";
import { UPLOAD_ACCEPT } from "./uploadChecks";
import { maxSizeText, progressText, resultText, sharedIndexText, summaryText } from "./uploadText";

const useStyles = makeStyles({
  body: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  // The input is reached only through Choose files, so it is hidden from everyone.
  input: { display: "none" },
  drop: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    rowGap: tokens.spacingVerticalS,
    padding: tokens.spacingHorizontalL,
    borderTopWidth: tokens.strokeWidthThick,
    borderRightWidth: tokens.strokeWidthThick,
    borderBottomWidth: tokens.strokeWidthThick,
    borderLeftWidth: tokens.strokeWidthThick,
    borderTopStyle: "dashed",
    borderRightStyle: "dashed",
    borderBottomStyle: "dashed",
    borderLeftStyle: "dashed",
    borderTopColor: tokens.colorNeutralStroke1,
    borderRightColor: tokens.colorNeutralStroke1,
    borderBottomColor: tokens.colorNeutralStroke1,
    borderLeftColor: tokens.colorNeutralStroke1,
    borderRadius: tokens.borderRadiusMedium,
  },
  dropActive: {
    borderTopColor: tokens.colorBrandStroke1,
    borderRightColor: tokens.colorBrandStroke1,
    borderBottomColor: tokens.colorBrandStroke1,
    borderLeftColor: tokens.colorBrandStroke1,
  },
  result: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalXS,
    overflowWrap: "anywhere",
  },
  name: { overflowWrap: "anywhere" },
  actions: {
    display: "flex",
    flexWrap: "wrap",
    columnGap: tokens.spacingHorizontalS,
    rowGap: tokens.spacingVerticalS,
  },
});

/** The screen's upload batch: one per Documents screen, so closing the panel keeps it, and
 * leaving the screen stops an upload and still submits what went up. */
export function useUploadBatch(indexId: string): UploadBatch {
  const queryClient = useQueryClient();
  const [batch] = useState(
    () =>
      new UploadBatch({
        api: httpUploadApi(indexId),
        onChanged: () => {
          void queryClient.invalidateQueries({ queryKey: adminKeys.documentLists });
          void queryClient.invalidateQueries({ queryKey: adminKeys.index(indexId) });
          void queryClient.invalidateQueries({ queryKey: adminKeys.runLists });
        },
      }),
  );
  useEffect(
    () => () => {
      batch.dispose();
    },
    [batch],
  );
  return batch;
}

export interface UploadPanelProps {
  readonly open: boolean;
  readonly indexId: string;
  /** The project chosen in Upload through project; uploads go through it. */
  readonly project: ProjectRef | null;
  readonly batch: UploadBatch;
  readonly onClose: () => void;
}

export function UploadPanel({ open, indexId, project, batch, onClose }: UploadPanelProps) {
  const titleId = useId();
  const close = () => {
    batch.close();
    onClose();
  };
  return (
    <OverlayDrawer
      open={open}
      position="end"
      size="large"
      aria-labelledby={titleId}
      onOpenChange={(_, data) => {
        if (!data.open) close();
      }}
    >
      <DrawerHeader>
        <DrawerHeaderTitle
          heading={{ id: titleId }}
          action={
            <Button
              appearance="subtle"
              aria-label={en["upload.close"]}
              icon={<Dismiss24Regular />}
              onClick={close}
            />
          }
        >
          {en["upload.title"]}
        </DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody>
        {open ? (
          <PanelContent indexId={indexId} project={project} batch={batch} onClose={close} />
        ) : null}
      </DrawerBody>
      <FocusFirstControl />
    </OverlayDrawer>
  );
}

interface PanelContentProps {
  readonly indexId: string;
  readonly project: ProjectRef | null;
  readonly batch: UploadBatch;
  readonly onClose: () => void;
}

function PanelContent({ indexId, project, batch, onClose }: PanelContentProps) {
  const styles = useStyles();
  const options = useQuery(indexOptionsQuery());
  const shared = useSharedProjects(indexId);
  const state = useSyncExternalStore(batch.subscribe, batch.getState);
  if (options.data === undefined) {
    const error = options.isError ? apiErrorOf(options.error) : null;
    return error === null ? (
      <Spinner size="small" aria-label={en["upload.title"]} />
    ) : (
      <LoadErrorBar
        title={en["index.optionsError"]}
        error={error}
        onRetry={() => {
          void options.refetch();
        }}
      />
    );
  }
  return (
    <div className={styles.body}>
      <ChooseFiles batch={batch} state={state} limits={options.data.uploadLimits} />
      {state.tooMany ? (
        <ErrorBar
          title={fillText(en["upload.tooMany"], {
            max: options.data.uploadLimits.maxFilesPerBatch,
          })}
        />
      ) : null}
      {state.phase === "checking" && state.progress !== null ? (
        <Spinner
          size="tiny"
          labelPosition="after"
          label={fillText(en["upload.checking"], {
            done: state.progress.done,
            total: state.progress.total,
          })}
        />
      ) : null}
      {state.rows.length === 0 ? null : (
        <ResultsTable
          batch={batch}
          state={state}
          maxFileBytes={options.data.uploadLimits.maxFileBytes}
        />
      )}
      {nothingToUpload(state) ? <Text>{en["upload.nothingToUpload"]}</Text> : null}
      <BatchFailureBar batch={batch} state={state} />
      {state.summary === null ? null : <Summary state={state} onClose={onClose} />}
      <UploadActions batch={batch} state={state} project={project} shared={shared} />
    </div>
  );
}

/** The projects that map the index, read again when the panel opens (section 10.4), so a mapping
 * added after the Documents screen loaded is named; null while that read is pending or after it
 * failed. */
function useSharedProjects(indexId: string): readonly ProjectRef[] | null {
  const queryClient = useQueryClient();
  const [projects, setProjects] = useState<readonly ProjectRef[] | null>(null);
  useEffect(() => {
    let current = true;
    queryClient.query({ ...adminIndexQuery(indexId), staleTime: 0 }).then(
      (index) => {
        if (current) setProjects(index.mappedProjects);
      },
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [queryClient, indexId]);
  return projects;
}

/** The time until which a paused step waits, or null. */
function pausedUntil(state: UploadState): number | null {
  return state.failure?.paused === true ? retryAtOf(state.failure.error) : null;
}

function ChooseFiles({
  batch,
  state,
  limits,
}: {
  readonly batch: UploadBatch;
  readonly state: UploadState;
  readonly limits: UploadLimits;
}) {
  const styles = useStyles();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const paused = useWaiting(pausedUntil(state));
  const busy =
    state.phase === "checking" ||
    state.phase === "uploading" ||
    state.phase === "submitting" ||
    (state.failure !== null && state.failure.step !== "preflight");
  const disabled = busy || paused;
  const take = (files: FileList | null) => {
    if (files === null || disabled) return;
    batch.choose(Array.from(files), limits);
  };
  return (
    // Dropping is a shortcut for pointer users only; Choose files inside the zone is the way that
    // needs no dragging and works by keyboard (WCAG 2.5.7), so the zone itself takes no focus.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      className={mergeClasses(styles.drop, dragging && !disabled && styles.dropActive)}
      onDragOver={(event: DragEvent) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => {
        setDragging(false);
      }}
      onDrop={(event: DragEvent) => {
        event.preventDefault();
        setDragging(false);
        take(event.dataTransfer.files);
      }}
    >
      <Button
        icon={<DocumentAdd20Regular />}
        disabled={disabled}
        onClick={() => {
          input.current?.click();
        }}
      >
        {en["upload.choose"]}
      </Button>
      <input
        ref={input}
        className={styles.input}
        type="file"
        multiple
        accept={UPLOAD_ACCEPT}
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event: ChangeEvent<HTMLInputElement>) => {
          take(event.target.files);
          // The same file chosen again must count as a new selection.
          event.target.value = "";
        }}
      />
      <Text>{fillText(en["upload.drop"], { maxSize: maxSizeText(limits.maxFileBytes) })}</Text>
    </div>
  );
}

function ResultsTable({
  batch,
  state,
  maxFileBytes,
}: {
  readonly batch: UploadBatch;
  readonly state: UploadState;
  readonly maxFileBytes: number;
}) {
  return (
    <AdminTable
      caption={en["upload.title"]}
      headers={[en["upload.col.file"], en["upload.col.result"]]}
    >
      {state.rows.map((row) => (
        <ResultRow
          key={row.key}
          row={row}
          maxFileBytes={maxFileBytes}
          editable={state.phase === "ready" && state.failure === null}
          onConfirm={(checked) => {
            batch.setConfirm(row.key, checked);
          }}
        />
      ))}
    </AdminTable>
  );
}

function ResultRow({
  row,
  maxFileBytes,
  editable,
  onConfirm,
}: {
  readonly row: UploadRow;
  readonly maxFileBytes: number;
  readonly editable: boolean;
  readonly onConfirm: (checked: boolean) => void;
}) {
  const styles = useStyles();
  const name = row.file.name;
  const isUpdate =
    row.upload.kind === "none" &&
    row.check.kind === "preflight" &&
    row.check.result.status === "update";
  return (
    <TableRow>
      <TableCell>
        <span className={styles.name}>{name}</span>
      </TableCell>
      <TableCell>
        <div className={styles.result}>
          {row.upload.kind === "uploading" ? (
            <Field label={progressText(name, row.upload.percent)}>
              <ProgressBar value={row.upload.percent} max={100} />
            </Field>
          ) : (
            <Text>{resultText(row, maxFileBytes)}</Text>
          )}
          {isUpdate ? (
            <Checkbox
              label={fillText(en["upload.result.updateLabel"], { name })}
              checked={row.confirmUpdate}
              disabled={!editable}
              onChange={(_, data) => {
                onConfirm(data.checked === true);
              }}
            />
          ) : null}
        </div>
      </TableCell>
    </TableRow>
  );
}

/** The batch's error bar: a failed preflight or submit, or the paused state with its retry time
 * (section 10.7). Retry stays disabled until a `retryAfterSeconds` time passes. */
function BatchFailureBar({
  batch,
  state,
}: {
  readonly batch: UploadBatch;
  readonly state: UploadState;
}) {
  const { failure } = state;
  const waiting = useWaiting(failure === null ? null : retryAtOf(failure.error));
  if (failure === null) return null;
  return (
    <ApiErrorBar
      error={failure.error}
      retryDisabled={waiting}
      onRetry={() => {
        batch.retry();
      }}
    />
  );
}

function Summary({
  state,
  onClose,
}: {
  readonly state: UploadState;
  readonly onClose: () => void;
}) {
  if (state.summary === null) return null;
  const text = summaryText(state.summary);
  const runId = state.summary.runId;
  return (
    <MessageBar intent={text.complete ? "success" : "warning"} layout="multiline">
      <MessageBarBody>
        {text.title === null ? null : <MessageBarTitle>{text.title}</MessageBarTitle>}
        {text.body}
      </MessageBarBody>
      <MessageBarActions>
        {runId === null ? null : (
          <RouterLink to={`/admin/runs/${runId}`}>{en["upload.viewProgress"]}</RouterLink>
        )}
        <Button onClick={onClose}>{en["upload.close"]}</Button>
      </MessageBarActions>
    </MessageBar>
  );
}

/** The shared-index warning directly above Upload, Upload with its count, and Stop uploading. */
function UploadActions({
  batch,
  state,
  project,
  shared,
}: {
  readonly batch: UploadBatch;
  readonly state: UploadState;
  readonly project: ProjectRef | null;
  readonly shared: readonly ProjectRef[] | null;
}) {
  const styles = useStyles();
  const paused = useWaiting(pausedUntil(state));
  const running = state.failure === null;
  if (state.phase === "uploading" && running) {
    return (
      <div className={styles.actions}>
        <Button
          icon={<Stop20Regular />}
          onClick={() => {
            batch.stop();
          }}
        >
          {en["upload.stop"]}
        </Button>
      </div>
    );
  }
  // A batch paused mid-upload or at the submit still shows Upload, disabled until Retry may run
  // (section 10.7): Retry, not Upload, repeats the refused step.
  const pausedBatch = state.failure?.paused === true;
  if (
    !pausedBatch &&
    state.phase !== "idle" &&
    state.phase !== "checking" &&
    state.phase !== "ready"
  ) {
    return null;
  }
  const count = chosenCount(state);
  return (
    <>
      <MessageBar intent="warning" layout="multiline">
        <MessageBarBody>{sharedIndexText(shared)}</MessageBarBody>
      </MessageBar>
      <div className={styles.actions}>
        <Button
          appearance="primary"
          icon={<ArrowUpload20Regular />}
          disabled={state.phase !== "ready" || count === 0 || paused || project === null}
          onClick={() => {
            if (project !== null) batch.start(project.projectId);
          }}
        >
          {pluralText(en["upload.start"], count)}
        </Button>
      </div>
    </>
  );
}
