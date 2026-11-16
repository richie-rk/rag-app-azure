// Screen headings and titles (10-web-client.md sections 6.4 and 7.1): every screen has one `h1`,
// focused on arrival so a screen-reader user hears where they arrived, and a document title per
// route; admin detail screens add a breadcrumb.

import {
  Breadcrumb,
  BreadcrumbButton,
  BreadcrumbDivider,
  BreadcrumbItem,
  makeStyles,
  mergeClasses,
  tokens,
} from "@fluentui/react-components";
import { Fragment, useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import { useHref, useLocation, useNavigate, type To } from "react-router";

import { useVisuallyHiddenStyles } from "../a11y/LiveRegions";
import { en } from "../copy/en";
import { isModifiedClick } from "./RouterLink";

const SEPARATOR = " · ";

/** `<part> · … · DocChat`, or `DocChat` alone (section 6.1). */
export function documentTitle(...parts: readonly string[]): string {
  return [...parts, en["shell.productName"]].join(SEPARATOR);
}

/** Sets the document title while the calling screen is shown. */
export function useDocumentTitle(...parts: readonly string[]): void {
  const title = documentTitle(...parts);
  useEffect(() => {
    document.title = title;
  }, [title]);
}

const useStyles = makeStyles({
  heading: { outlineStyle: "none", margin: "0" },
  header: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
    marginBottom: tokens.spacingVerticalL,
  },
  titleRow: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalS,
  },
  title: {
    fontSize: tokens.fontSizeBase600,
    lineHeight: tokens.lineHeightBase600,
    fontWeight: tokens.fontWeightSemibold,
  },
  actions: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalS },
});

export interface ScreenHeadingProps {
  readonly children: ReactNode;
  readonly className?: string;
  /** The chat screen's heading is visually hidden (section 8.1). */
  readonly visuallyHidden?: boolean;
}

/** The screen's `h1` (`tabindex="-1"`), focused on every route change. Focus doesn't scroll, so
 * opening a session from the session panel keeps the panel's scroll position. */
export function ScreenHeading({ children, className, visuallyHidden = false }: ScreenHeadingProps) {
  const styles = useStyles();
  const hidden = useVisuallyHiddenStyles();
  const ref = useRef<HTMLHeadingElement>(null);
  const { pathname } = useLocation();
  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, [pathname]);
  return (
    <h1
      ref={ref}
      tabIndex={-1}
      className={mergeClasses(styles.heading, visuallyHidden && hidden.root, className)}
    >
      {children}
    </h1>
  );
}

export interface Crumb {
  readonly label: string;
  /** Omitted on the current page, the last crumb. */
  readonly to?: To;
}

function CrumbButton({ crumb }: { readonly crumb: Crumb & { readonly to: To } }) {
  const navigate = useNavigate();
  const href = useHref(crumb.to);
  return (
    <BreadcrumbButton
      as="a"
      href={href}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        if (isModifiedClick(event)) return;
        event.preventDefault();
        void navigate(crumb.to);
      }}
    >
      {crumb.label}
    </BreadcrumbButton>
  );
}

export interface PageHeaderProps {
  readonly title: ReactNode;
  readonly breadcrumbs?: readonly Crumb[];
  /** Beside the title: New project, the status badge, Archive project, … */
  readonly actions?: ReactNode;
}

/** The admin page header: an optional breadcrumb, the `h1` and its actions. */
export function PageHeader({ title, breadcrumbs, actions }: PageHeaderProps) {
  const styles = useStyles();
  return (
    <div className={styles.header}>
      {breadcrumbs === undefined || breadcrumbs.length === 0 ? null : (
        <Breadcrumb>
          {breadcrumbs.map((crumb, index) => (
            <Fragment key={`${String(index)}:${crumb.label}`}>
              {index > 0 ? <BreadcrumbDivider /> : null}
              <BreadcrumbItem>
                {crumb.to === undefined ? (
                  <BreadcrumbButton current>{crumb.label}</BreadcrumbButton>
                ) : (
                  <CrumbButton crumb={{ ...crumb, to: crumb.to }} />
                )}
              </BreadcrumbItem>
            </Fragment>
          ))}
        </Breadcrumb>
      )}
      <div className={styles.titleRow}>
        <ScreenHeading className={styles.title}>{title}</ScreenHeading>
        {actions === undefined ? null : <div className={styles.actions}>{actions}</div>}
      </div>
    </div>
  );
}
