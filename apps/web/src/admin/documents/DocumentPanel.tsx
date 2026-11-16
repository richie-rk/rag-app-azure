// The document panel (10-web-client.md section 7.9): an overlay drawer at the end, size `medium`,
// opened by `?document=<id>`. It reads `adminGetDocument` and lists the versions, newest first.
// Above them, Download PDF saves the current version's whole-document rendition through
// `adminGetDocumentPdf` under section 8.14's download rules, or a caption says the document is too
// large to download as one PDF. The uploaded file itself is never offered: no operation serves it.

import {
  Button,
  Caption1,
  DrawerBody,
  DrawerHeader,
  DrawerHeaderTitle,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  OverlayDrawer,
  TableCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ArrowDownload20Regular, Dismiss24Regular } from "@fluentui/react-icons";
import { queryOptions, useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";

import { request } from "../../api/http";
import { apiErrorOf, clientError, isProblem, retryAtOf, type ApiError } from "../../api/problem";
import { downloadFile, safeFileName, saveBlob } from "../../chat/citations/download";
import { useWaiting } from "../../chat/turns/time";
import { en } from "../../copy/en";
import { ApiErrorBar, ErrorBar } from "../../ui/ErrorBar";
import { FocusFirstControl } from "../../ui/FocusFirstControl";
import { formatDateTime, formatSize } from "../../ui/format";
import { AdminTable, TableSkeleton } from "../shared/AdminTable";
import { adminKeys, type DocumentDetail } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { problemText } from "../runs/runDisplay";

const useStyles = makeStyles({
  body: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  heading: { overflowWrap: "anywhere" },
  section: {
    margin: "0",
    fontSize: tokens.fontSizeBase400,
    lineHeight: tokens.lineHeightBase400,
    fontWeight: tokens.fontWeightSemibold,
  },
  download: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    rowGap: tokens.spacingVerticalS,
  },
  cell: { overflowWrap: "anywhere" },
});

/** `adminGetDocument` under `['admin', 'document', documentId]`. */
export function documentQuery(indexId: string, documentId: string) {
  return queryOptions({
    queryKey: adminKeys.document(documentId),
    queryFn: ({ signal }) => request("adminGetDocument", { path: { indexId, documentId }, signal }),
  });
}

export interface DocumentPanelProps {
  readonly indexId: string;
  /** The document in `?document=`, or null while the panel is closed. */
  readonly documentId: string | null;
  /** The name from the list, shown while the document loads. */
  readonly name: string | null;
  readonly onClose: () => void;
}

export function DocumentPanel({ indexId, documentId, name, onClose }: DocumentPanelProps) {
  const titleId = useId();
  return (
    <OverlayDrawer
      open={documentId !== null}
      position="end"
      size="medium"
      aria-labelledby={titleId}
      onOpenChange={(_, data) => {
        if (!data.open) onClose();
      }}
    >
      {documentId === null ? null : (
        <PanelContent
          key={documentId}
          indexId={indexId}
          documentId={documentId}
          name={name}
          titleId={titleId}
          onClose={onClose}
        />
      )}
    </OverlayDrawer>
  );
}

interface PanelContentProps {
  readonly indexId: string;
  readonly documentId: string;
  readonly name: string | null;
  readonly titleId: string;
  readonly onClose: () => void;
}

function PanelContent({ indexId, documentId, name, titleId, onClose }: PanelContentProps) {
  const styles = useStyles();
  const detail = useQuery(documentQuery(indexId, documentId));
  const [unavailable, setUnavailable] = useState<ApiError | null>(null);
  const document = detail.data;

  let body;
  if (document === undefined) {
    const error = detail.isError ? apiErrorOf(detail.error) : null;
    if (error === null) body = <TableSkeleton />;
    else if (isProblem(error, 404)) {
      body = <ErrorBar title={error.title ?? ""} body={error.detail} />;
    } else {
      body = (
        <ApiErrorBar
          error={error}
          onRetry={() => {
            void detail.refetch();
          }}
        />
      );
    }
  } else {
    body = (
      <>
        <PdfAction
          indexId={indexId}
          document={document}
          onUnavailable={(error) => {
            setUnavailable(error);
            void detail.refetch();
          }}
        />
        {unavailable === null ? null : (
          <MessageBar intent="warning" layout="multiline">
            <MessageBarBody>
              <MessageBarTitle>{unavailable.title}</MessageBarTitle>
              {unavailable.detail}
            </MessageBarBody>
          </MessageBar>
        )}
        <h3 className={styles.section}>{en["documentPanel.versions"]}</h3>
        <Versions document={document} />
      </>
    );
  }

  return (
    <>
      <DrawerHeader>
        <DrawerHeaderTitle
          heading={{ id: titleId, className: styles.heading }}
          action={
            <Button
              appearance="subtle"
              aria-label={en["documentPanel.close"]}
              icon={<Dismiss24Regular />}
              onClick={onClose}
            />
          }
        >
          {document?.name ?? name ?? ""}
        </DrawerHeaderTitle>
      </DrawerHeader>
      <DrawerBody className={styles.body}>{body}</DrawerBody>
      <FocusFirstControl />
    </>
  );
}

/** The versions, newest first, with `documentPanel.col.*`. */
function Versions({ document }: { readonly document: DocumentDetail }) {
  const styles = useStyles();
  return (
    <AdminTable
      caption={en["documentPanel.versions"]}
      headers={[
        en["documentPanel.col.version"],
        en["documentPanel.col.state"],
        en["documentPanel.col.uploaded"],
        en["documentPanel.col.uploadedBy"],
        en["documentPanel.col.throughProject"],
        en["documentPanel.col.size"],
        en["documentPanel.col.scanResult"],
        en["documentPanel.col.problem"],
      ]}
    >
      {document.versions.map((version) => (
        <TableRow key={version.versionId}>
          <TableCell>{version.versionNumber}</TableCell>
          <TableCell>
            <StatusBadge kind="versionState" value={version.state} />
          </TableCell>
          <TableCell>{formatDateTime(version.reservedAt)}</TableCell>
          <TableCell className={styles.cell}>
            {version.uploadedByName ?? en["admin.notSet"]}
          </TableCell>
          <TableCell className={styles.cell}>{version.uploadedViaProject.name}</TableCell>
          <TableCell>{formatSize(version.sizeBytes)}</TableCell>
          <TableCell>
            <StatusBadge kind="scanStatus" value={version.scanStatus} />
          </TableCell>
          <TableCell className={styles.cell}>
            {problemText(version.failureMessage, version.failureCode)}
          </TableCell>
        </TableRow>
      ))}
    </AdminTable>
  );
}

/** The limits a download waits on: the answer's retry time disables the button and Retry. */
function isDownloadLimit(error: ApiError): boolean {
  return (
    isProblem(error, 429, "rate_limited") ||
    isProblem(error, 429, "too_many_downloads") ||
    isProblem(error, 429, "too_many_requests")
  );
}

/** The refusals shown in the warning above the versions: no whole-document PDF for the current
 * version, or its file is missing. */
function isUnavailable(error: ApiError): boolean {
  return (
    isProblem(error, 409, "document_pdf_unavailable") ||
    isProblem(error, 500, "document_pdf_missing")
  );
}

interface PdfActionProps {
  readonly indexId: string;
  readonly document: DocumentDetail;
  readonly onUnavailable: (error: ApiError) => void;
}

/** Download PDF while the current version's PDF is `built`; the too-large caption for
 * `too_large`; nothing without a current version or before its rendition exists. */
function PdfAction({ indexId, document, onUnavailable }: PdfActionProps) {
  const status = document.currentVersion?.documentPdfStatus ?? null;
  if (status === "too_large") return <Caption1>{en["documentPanel.pdfTooLarge"]}</Caption1>;
  if (status !== "built" || document.currentVersion === null) return null;
  return (
    <DownloadPdf
      indexId={indexId}
      documentId={document.documentId}
      name={document.name}
      versionNumber={document.currentVersion.versionNumber}
      onUnavailable={onUnavailable}
    />
  );
}

interface DownloadPdfProps {
  readonly indexId: string;
  readonly documentId: string;
  readonly name: string;
  readonly versionNumber: number;
  readonly onUnavailable: (error: ApiError) => void;
}

/** Download PDF (section 8.14's Downloads): only the API is fetched, the body must be a PDF of at
 * most 104,857,600 bytes, and the file is saved under the document's name with `.pdf`. */
function DownloadPdf({
  indexId,
  documentId,
  name,
  versionNumber,
  onUnavailable,
}: DownloadPdfProps) {
  const styles = useStyles();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const controller = useRef<AbortController | null>(null);
  const waiting = useWaiting(error !== null && isDownloadLimit(error) ? retryAtOf(error) : null);
  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );
  const start = () => {
    controller.current = new AbortController();
    setPending(true);
    setError(null);
    downloadFile("adminGetDocumentPdf", {
      path: { indexId, documentId },
      signal: controller.current.signal,
    }).then(
      (file) => {
        setPending(false);
        // The fallback number only matters for a name that is all extension.
        saveBlob(file.blob, safeFileName(name, versionNumber));
      },
      (reason: unknown) => {
        const failure = apiErrorOf(reason) ?? clientError("aborted");
        setPending(false);
        if (failure.kind === "aborted") return;
        if (isUnavailable(failure)) {
          onUnavailable(failure);
          return;
        }
        setError(failure);
      },
    );
  };
  return (
    <div className={styles.download}>
      <Button icon={<ArrowDownload20Regular />} disabled={pending || waiting} onClick={start}>
        {pending ? en["citation.downloading"] : en["documentPanel.downloadPdf"]}
      </Button>
      {error === null ? null : (
        <DownloadError error={error} retryDisabled={waiting} onRetry={start} />
      )}
    </div>
  );
}

/** A failed Download PDF, by the download column of section 8.14's table. */
function DownloadError({
  error,
  retryDisabled,
  onRetry,
}: {
  readonly error: ApiError;
  readonly retryDisabled: boolean;
  readonly onRetry: () => void;
}) {
  if (isProblem(error, 404)) return <ErrorBar title={error.title ?? ""} body={error.detail} />;
  if (error.kind === "problem") {
    return <ApiErrorBar error={error} onRetry={onRetry} retryDisabled={retryDisabled} />;
  }
  return (
    <ErrorBar
      title={en["citation.downloadFailed"]}
      referenceId={error.referenceId}
      actions={<Button onClick={onRetry}>{en["error.retry"]}</Button>}
    />
  );
}
