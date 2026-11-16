// Status badges for API values (10-web-client.md section 7.12): appearance `tint`, a colour per
// value, and always the value's display name, so colour is never the only signal. A value the
// generated schema doesn't list fails the response's parse, so each record below is complete.

import { Badge, type BadgeProps } from "@fluentui/react-components";

import { displayNames } from "../../copy/en";

type BadgeColor = NonNullable<BadgeProps["color"]>;

/** The enumerations shown as badges, by their `displayNames` group. */
export type StatusKind =
  | "runState"
  | "itemState"
  | "versionState"
  | "documentDisplayState"
  | "scanStatus"
  | "indexStatus"
  | "projectStatus";

type Values<K extends StatusKind> = keyof (typeof displayNames)[K] & string;

/** Section 7.12's colours. Values the section doesn't name take the colour of the state they
 * resemble: uploading, ingesting and in progress are work under way (`brand`); replaced, not
 * required and deleted are settled and need no attention (`informative`). */
export const BADGE_COLORS: { readonly [K in StatusKind]: Readonly<Record<Values<K>, BadgeColor>> } =
  {
    runState: {
      queued: "informative",
      running: "brand",
      completed: "success",
      completed_with_errors: "warning",
      failed: "danger",
    },
    itemState: {
      queued: "informative",
      running: "brand",
      succeeded: "success",
      skipped: "informative",
      failed: "danger",
      cancelled: "warning",
    },
    versionState: {
      uploading: "brand",
      pending: "informative",
      superseded: "informative",
      ingesting: "brand",
      ingested: "success",
      failed: "danger",
    },
    documentDisplayState: {
      ingested: "success",
      in_progress: "brand",
      failed: "danger",
      no_content: "warning",
    },
    scanStatus: {
      not_required: "informative",
      awaiting: "informative",
      clean: "success",
      malicious: "danger",
      failed: "danger",
      unavailable: "danger",
    },
    indexStatus: {
      provisioning: "brand",
      ready: "success",
      reingest_required: "warning",
      reingesting: "brand",
      rebuilding: "brand",
      failed: "danger",
      deleting: "brand",
      deleted: "informative",
    },
    projectStatus: { active: "success", archived: "warning" },
  };

export interface StatusBadgeProps<K extends StatusKind> {
  readonly kind: K;
  readonly value: Values<K>;
}

/** The badge of one API value: its display name in its colour. */
export function StatusBadge<K extends StatusKind>({ kind, value }: StatusBadgeProps<K>) {
  const names: Readonly<Record<string, string>> = displayNames[kind];
  const colors: Readonly<Record<string, BadgeColor>> = BADGE_COLORS[kind];
  return (
    <Badge appearance="tint" color={colors[value] ?? "informative"}>
      {names[value] ?? value}
    </Badge>
  );
}
