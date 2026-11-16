// The citation viewer (10-web-client.md section 8.14, ADR 0087): opened at the right-hand end, it
// reads `getCitation`, then shows the cited page from a client-built PDF blob in an unsandboxed
// frame where the browser has a PDF viewer, or a metadata panel with Download page where it
// hasn't. It holds one object URL at a time and revokes it on close, on switching citations and on
// unmount. The frame is driven only by `openAs`, whose one value is `pdf_page`.

import {
  Button,
  Caption1,
  DrawerBody,
  DrawerFooter,
  DrawerHeader,
  DrawerHeaderTitle,
  InlineDrawer,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  OverlayDrawer,
  Spinner,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ArrowDownload20Regular, Dismiss24Regular } from "@fluentui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import type { ContentType } from "../../api/generated/types.gen";
import { request } from "../../api/http";
import type { ResponseOf } from "../../api/operations";
import { apiErrorOf, clientError, isProblem, retryAtOf, type ApiError } from "../../api/problem";
import { queryKeys } from "../../api/queryClient";
import { en } from "../../copy/en";
import { ApiErrorBar, ErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import type { Citation } from "../stream/events";
import { useWaiting } from "../turns/time";
import { downloadFile, safeFileName, saveBlob } from "./download";

const useStyles = makeStyles({
  inline: { width: "440px", maxWidth: "100%" },
  heading: { outlineStyle: "none" },
  header: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalXXS },
  body: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  // The frame fills the body's height and never more, so the body itself never scrolls: a
  // scrolling body would be a region the keyboard can't reach, while the PDF viewer inside the
  // frame scrolls the page and takes focus itself.
  frame: {
    flexGrow: 1,
    flexShrink: 1,
    flexBasis: "0",
    minHeight: "0",
    width: "100%",
    border: "none",
  },
  fields: {
    display: "grid",
    gridTemplateColumns: "max-content 1fr",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalXS,
    margin: "0",
  },
  group: { display: "contents" },
  term: { fontWeight: tokens.fontWeightSemibold },
  value: { margin: "0", overflowWrap: "anywhere" },
  footer: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    rowGap: tokens.spacingVerticalS,
  },
});

/** The citation a viewer shows: a turn's citation, addressed by session, turn and number. */
export interface CitationTarget {
  readonly sessionId: string;
  readonly turnId: string;
  readonly citation: Citation;
}

type CitationDetail = ResponseOf<"getCitation">;

const TYPE_TEXT: Readonly<Record<ContentType, string>> = {
  "application/pdf": en["citation.type.pdf"],
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
    en["citation.type.docx"],
  "application/vnd.openxmlformats-officedocument.presentationml.presentation":
    en["citation.type.pptx"],
};

function failureOf(reason: unknown): ApiError {
  return apiErrorOf(reason) ?? clientError("network");
}

function isDownloadLimit(error: ApiError): boolean {
  return (
    isProblem(error, 429, "rate_limited") ||
    isProblem(error, 429, "too_many_downloads") ||
    isProblem(error, 429, "too_many_requests")
  );
}

/** A refusal shown as a warning with the problem's own text and no Retry: the document changed,
 * or it has no whole-document PDF. */
function ProblemWarning({ error }: { readonly error: ApiError }) {
  return (
    <MessageBar intent="warning" layout="multiline">
      <MessageBarBody>
        <MessageBarTitle>{error.title}</MessageBarTitle>
        {error.detail}
      </MessageBarBody>
    </MessageBar>
  );
}

interface CitationErrorProps {
  readonly error: ApiError;
  /** `citation.error` in the viewer body, `citation.downloadFailed` on a download. */
  readonly failedText: string;
  readonly onRetry: () => void;
}

/** A failed step of the viewer, by the table of section 8.14. */
function CitationError({ error, failedText, onRetry }: CitationErrorProps) {
  const waiting = useWaiting(isDownloadLimit(error) ? retryAtOf(error) : null);
  if (error.kind === "aborted") return null;
  if (isProblem(error, 404, "citation_not_found")) {
    return <ErrorBar title={error.title ?? failedText} body={error.detail} />;
  }
  if (
    isProblem(error, 410, "document_changed") ||
    isProblem(error, 409, "document_pdf_unavailable")
  ) {
    return <ProblemWarning error={error} />;
  }
  if (error.kind === "problem") {
    return <ApiErrorBar error={error} onRetry={onRetry} retryDisabled={waiting} />;
  }
  return (
    <ErrorBar
      title={failedText}
      referenceId={error.referenceId}
      actions={<Button onClick={onRetry}>{en["error.retry"]}</Button>}
    />
  );
}

interface CitationPath {
  readonly sessionId: string;
  readonly turnId: string;
  readonly citationNumber: number;
}

interface DownloadButtonProps {
  readonly operation: "getCitationContent" | "getCitationDocument";
  readonly path: CitationPath;
  readonly fileName: string;
  readonly label: string;
  /** `409 document_pdf_unavailable`: the button gives way to `citation.documentTooLarge`. */
  readonly onUnavailable?: (error: ApiError) => void;
}

/** Download page or Download full document. */
function DownloadButton({ operation, path, fileName, label, onUnavailable }: DownloadButtonProps) {
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
    downloadFile(operation, { path, signal: controller.current.signal }).then(
      (file) => {
        setPending(false);
        saveBlob(file.blob, safeFileName(fileName, path.citationNumber));
      },
      (reason: unknown) => {
        const failure = failureOf(reason);
        setPending(false);
        if (isProblem(failure, 409, "document_pdf_unavailable") && onUnavailable !== undefined) {
          onUnavailable(failure);
          return;
        }
        setError(failure);
      },
    );
  };
  return (
    <>
      <Button icon={<ArrowDownload20Regular />} disabled={pending || waiting} onClick={start}>
        {pending ? en["citation.downloading"] : label}
      </Button>
      {error === null ? null : (
        <CitationError error={error} failedText={en["citation.downloadFailed"]} onRetry={start} />
      )}
    </>
  );
}

type FrameResult =
  | { readonly attempt: number; readonly url: string }
  | { readonly attempt: number; readonly error: ApiError };

/** The cited page in a frame: a PDF blob, the frame's only possible source. No `sandbox`
 * attribute, because a sandboxed frame shows no PDF viewer in Edge or Chrome. */
function PageFrame({ path, title }: { readonly path: CitationPath; readonly title: string }) {
  const styles = useStyles();
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<FrameResult | null>(null);
  const { sessionId, turnId, citationNumber } = path;
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let url: string | null = null;
    downloadFile("getCitationContent", {
      path: { sessionId, turnId, citationNumber },
      signal: controller.signal,
    }).then(
      (file) => {
        if (!active) return;
        url = URL.createObjectURL(file.blob);
        setResult({ attempt, url });
      },
      (reason: unknown) => {
        if (active) setResult({ attempt, error: failureOf(reason) });
      },
    );
    return () => {
      active = false;
      controller.abort();
      if (url !== null) URL.revokeObjectURL(url);
    };
  }, [attempt, sessionId, turnId, citationNumber]);

  const current = result?.attempt === attempt ? result : null;
  if (current === null) return <Spinner label={en["citation.loading"]} />;
  if ("error" in current) {
    return (
      <CitationError
        error={current.error}
        failedText={en["citation.error"]}
        onRetry={() => {
          setAttempt((value) => value + 1);
        }}
      />
    );
  }
  return (
    <iframe className={styles.frame} src={current.url} title={title} referrerPolicy="no-referrer" />
  );
}

/** Where the browser has no PDF viewer: what the citation is, and Download page. */
function MetadataPanel({
  detail,
  path,
}: {
  readonly detail: CitationDetail;
  readonly path: CitationPath;
}) {
  const styles = useStyles();
  const fields: readonly [string, string][] = [
    [en["citation.field.type"], TYPE_TEXT[detail.contentType]],
    [en["citation.field.location"], fillText(en["chat.locator.page"], { page: detail.pageNumber })],
    [en["citation.field.file"], detail.fileName],
  ];
  return (
    <>
      <dl className={styles.fields}>
        {fields.map(([term, value]) => (
          <div key={term} className={styles.group}>
            <dt className={styles.term}>{term}</dt>
            <dd className={styles.value}>{value}</dd>
          </div>
        ))}
      </dl>
      <p className={styles.value}>{en["citation.noPdfViewer"]}</p>
      <div>
        <DownloadButton
          operation="getCitationContent"
          path={path}
          fileName={detail.fileName}
          label={en["citation.downloadPage"]}
        />
      </div>
    </>
  );
}

/** Download full document, or the caption that takes its place. */
function DocumentFooter({
  detail,
  path,
}: {
  readonly detail: CitationDetail;
  readonly path: CitationPath;
}) {
  const styles = useStyles();
  const [unavailable, setUnavailable] = useState<ApiError | null>(null);
  return (
    <DrawerFooter className={styles.footer}>
      {unavailable === null ? null : <ProblemWarning error={unavailable} />}
      {detail.documentPdfAvailable && unavailable === null ? (
        <DownloadButton
          operation="getCitationDocument"
          path={path}
          fileName={detail.documentFileName}
          label={en["citation.downloadDocument"]}
          onUnavailable={setUnavailable}
        />
      ) : (
        <Caption1>{en["citation.documentTooLarge"]}</Caption1>
      )}
    </DrawerFooter>
  );
}

interface ViewerContentProps {
  readonly target: CitationTarget;
  readonly titleId: string;
  readonly onClose: () => void;
}

function ViewerContent({ target, titleId, onClose }: ViewerContentProps) {
  const styles = useStyles();
  const { sessionId, turnId, citation } = target;
  const path: CitationPath = { sessionId, turnId, citationNumber: citation.number };
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  const detail = useQuery({
    queryKey: queryKeys.citation(sessionId, turnId, citation.number),
    queryFn: ({ signal }) => request("getCitation", { path, signal }),
  });
  const locator = fillText(en["chat.locator.page"], { page: citation.pageNumber });
  const frameTitle = fillText(en["citation.frame"], {
    n: citation.number,
    title: citation.title,
    locator,
  });
  let body;
  if (detail.data !== undefined) {
    body = navigator.pdfViewerEnabled ? (
      <PageFrame path={path} title={frameTitle} />
    ) : (
      <MetadataPanel detail={detail.data} path={path} />
    );
  } else if (detail.isError) {
    body = (
      <CitationError
        error={failureOf(detail.error)}
        failedText={en["citation.error"]}
        onRetry={() => void detail.refetch()}
      />
    );
  } else {
    body = <Spinner label={en["citation.loading"]} />;
  }
  return (
    <>
      <DrawerHeader className={styles.header}>
        <DrawerHeaderTitle
          heading={{ id: titleId, ref: headingRef, tabIndex: -1, className: styles.heading }}
          action={
            <Button
              appearance="subtle"
              aria-label={en["citation.close"]}
              icon={<Dismiss24Regular />}
              onClick={onClose}
            />
          }
        >
          {fillText(en["citation.title"], { n: citation.number })}
        </DrawerHeaderTitle>
        <Subtitle2>{citation.title}</Subtitle2>
        <Caption1>{locator}</Caption1>
      </DrawerHeader>
      <DrawerBody className={styles.body}>{body}</DrawerBody>
      {detail.data === undefined ? null : <DocumentFooter detail={detail.data} path={path} />}
    </>
  );
}

export interface CitationViewerProps {
  readonly target: CitationTarget | null;
  /** Section 8.1: inline from 1,280 px, an overlay of size `medium` from 768 px, `full` below. */
  readonly layout: "inline" | "medium" | "full";
  readonly onClose: () => void;
}

export function CitationViewer({ target, layout, onClose }: CitationViewerProps) {
  const styles = useStyles();
  const titleId = useId();
  const content =
    target === null ? null : (
      <ViewerContent
        key={`${target.sessionId}:${target.turnId}:${String(target.citation.number)}`}
        target={target}
        titleId={titleId}
        onClose={onClose}
      />
    );
  if (layout === "inline") {
    return (
      <InlineDrawer
        as="aside"
        open={target !== null}
        position="end"
        separator
        aria-labelledby={titleId}
        className={styles.inline}
        onKeyDown={(event: KeyboardEvent) => {
          if (event.key === "Escape") onClose();
        }}
      >
        {content}
      </InlineDrawer>
    );
  }
  return (
    <OverlayDrawer
      open={target !== null}
      position="end"
      size={layout}
      aria-labelledby={titleId}
      onOpenChange={(_, data) => {
        if (!data.open) onClose();
      }}
    >
      {content}
    </OverlayDrawer>
  );
}
