// The admin tables (10-web-client.md sections 7.1 and 11.1): a Fluent Table (not DataGrid) with
// header cells and a visually hidden caption, inside its own scroll region so a narrow screen never
// scrolls sideways; a skeleton shaped like the rows; row actions as buttons with icon and text from
// 768 px wide and in a menu below it; and Show more while the API has another page.

import {
  Button,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Skeleton,
  SkeletonItem,
  Table,
  TableBody,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components";
import { MoreHorizontal20Regular } from "@fluentui/react-icons";
import type { ReactElement, ReactNode, Ref } from "react";

import { useVisuallyHiddenStyles } from "../../a11y/LiveRegions";
import type { ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { MEDIUM_QUERY, useMediaQuery } from "../../ui/media";

const useStyles = makeStyles({
  region: { overflowX: "auto", maxWidth: "100%" },
  caption: { outlineStyle: "none" },
  skeleton: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  actions: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalXS },
  more: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
});

export interface AdminTableProps {
  /** The visually hidden caption, equal to the screen's `h1`; it also names the scroll region. */
  readonly caption: string;
  readonly headers: readonly string[];
  /** The rows: `TableRow` elements. */
  readonly children: ReactNode;
  /** The caption takes focus when the last row is removed (section 11.4). */
  readonly captionRef?: Ref<HTMLTableCaptionElement>;
}

export function AdminTable({ caption, headers, children, captionRef }: AdminTableProps) {
  const styles = useStyles();
  const hidden = useVisuallyHiddenStyles();
  return (
    // A keyboard user must be able to scroll a wide table at 320 px, so its region is focusable.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div className={styles.region} role="region" aria-label={caption} tabIndex={0}>
      <Table>
        <caption
          ref={captionRef}
          tabIndex={-1}
          className={mergeClasses(hidden.root, styles.caption)}
        >
          {caption}
        </caption>
        <TableHeader>
          <TableRow>
            {headers.map((header) => (
              <TableHeaderCell key={header}>{header}</TableHeaderCell>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>{children}</TableBody>
      </Table>
    </div>
  );
}

/** The Loading state of a table: skeleton rows shaped like the content (6 by default). */
export function TableSkeleton({ rows = 6 }: { readonly rows?: number }) {
  const styles = useStyles();
  return (
    <Skeleton className={styles.skeleton}>
      {Array.from({ length: rows }, (_, index) => (
        <SkeletonItem key={index} size={24} />
      ))}
    </Skeleton>
  );
}

export interface RowAction {
  readonly key: string;
  /** The visible text; it starts the accessible name too (section 11.1, 2.5.3). */
  readonly label: string;
  /** The accessible name when it says more than the text, such as `Remove {name}`. */
  readonly accessibleName?: string;
  readonly icon: ReactElement;
  readonly onClick: () => void;
  readonly disabled?: boolean;
}

export interface RowActionsProps {
  /** The row's name, for the menu's `admin.actionsFor`. */
  readonly name: string;
  readonly actions: readonly RowAction[];
}

/** A row's actions: buttons with icon and text from 768 px wide, a menu below it. */
export function RowActions({ name, actions }: RowActionsProps) {
  const styles = useStyles();
  const medium = useMediaQuery(MEDIUM_QUERY);
  if (medium) {
    return (
      <div className={styles.actions}>
        {actions.map((action) => (
          <Button
            key={action.key}
            size="small"
            icon={action.icon}
            onClick={action.onClick}
            disabled={action.disabled === true}
            {...(action.accessibleName === undefined
              ? {}
              : { "aria-label": action.accessibleName })}
          >
            {action.label}
          </Button>
        ))}
      </div>
    );
  }
  return (
    <Menu>
      <MenuTrigger disableButtonEnhancement>
        <Button
          size="small"
          appearance="subtle"
          icon={<MoreHorizontal20Regular />}
          aria-label={fillText(en["admin.actionsFor"], { name })}
        />
      </MenuTrigger>
      <MenuPopover>
        <MenuList>
          {actions.map((action) => (
            <MenuItem
              key={action.key}
              icon={action.icon}
              onClick={action.onClick}
              disabled={action.disabled === true}
            >
              {action.label}
            </MenuItem>
          ))}
        </MenuList>
      </MenuPopover>
    </Menu>
  );
}

export interface ShowMoreProps {
  readonly hasMore: boolean;
  readonly loading: boolean;
  readonly onShowMore: () => void;
  /** A failed next page: its error bar under the table (the Partial state). */
  readonly error?: ApiError | null;
}

/** `admin.showMore` while `nextCursor` isn't null; a failed page shows its error bar, whose Retry
 * asks for the same page again. */
export function ShowMore({ hasMore, loading, onShowMore, error = null }: ShowMoreProps) {
  const styles = useStyles();
  if (!hasMore && error === null) return null;
  return (
    <div className={styles.more}>
      {error === null ? null : <ApiErrorBar error={error} onRetry={onShowMore} />}
      {hasMore && error === null ? (
        <div>
          <Button onClick={onShowMore} disabled={loading}>
            {en["admin.showMore"]}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
