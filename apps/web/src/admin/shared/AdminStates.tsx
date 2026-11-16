// The Error states of the admin screens (10-web-client.md sections 7.1 and 7.4 to 7.10): a load
// that failed shows an error bar where the content would have been, with Retry; a resource that
// doesn't exist (`404`) shows the problem's text with a link back to its list.

import { Button } from "@fluentui/react-components";
import type { To } from "react-router";

import { apiErrorOf, retryAtOf, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar, ErrorBar, errorText } from "../../ui/ErrorBar";
import { fillText, formatRetryTime } from "../../ui/format";
import { RouterLink } from "../../ui/RouterLink";

export interface LoadErrorBarProps {
  /** The screen's own text, such as `projects.loadError`. */
  readonly title: string;
  readonly error: ApiError;
  readonly onRetry: () => void;
}

/** A load that failed: the screen's text as the title, the failure's own text under it, the
 * retry time when the answer names one, the reference ID and Retry, which refetches. */
export function LoadErrorBar({ title, error, onRetry }: LoadErrorBarProps) {
  const text = errorText(error);
  if (text === null) return null;
  const retryAt = retryAtOf(error);
  return (
    <ErrorBar
      title={title}
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

/** The query fields a reload's failure needs. */
export interface ReloadableList {
  readonly data: unknown;
  readonly error: unknown;
  readonly isError: boolean;
  readonly isFetchNextPageError?: boolean;
  readonly refetch: () => Promise<unknown>;
}

/** A reload that failed while rows are on screen (the Partial state): the rows stay, and this bar
 * above them has Retry. A failed next page is Show more's own bar. */
export function ReloadErrorBar({ list }: { readonly list: ReloadableList }) {
  const error = apiErrorOf(list.error);
  if (list.data === undefined || !list.isError || list.isFetchNextPageError === true) return null;
  if (error === null) return null;
  return (
    <ApiErrorBar
      error={error}
      onRetry={() => {
        void list.refetch();
      }}
    />
  );
}

export interface NotFoundBarProps {
  readonly error: ApiError;
  /** The list's name for `admin.backToList`, such as Projects. */
  readonly list: string;
  readonly to: To;
}

/** A `404` for the resource a detail screen shows: the problem's text and `admin.backToList`. */
export function NotFoundBar({ error, list, to }: NotFoundBarProps) {
  const text = errorText(error);
  if (text === null) return null;
  return (
    <ErrorBar
      title={text.title}
      body={text.body}
      referenceId={error.referenceId}
      actions={<RouterLink to={to}>{fillText(en["admin.backToList"], { list })}</RouterLink>}
    />
  );
}
