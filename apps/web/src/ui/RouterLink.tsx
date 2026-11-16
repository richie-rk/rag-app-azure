// A Fluent Link that navigates inside the app (10-web-client.md section 6.1): a real `href`, so
// it opens in a new tab and reads as a link, and router navigation on a plain click.

import { Link } from "@fluentui/react-components";
import type { MouseEvent, ReactNode } from "react";
import { useHref, useNavigate, type To } from "react-router";

export interface RouterLinkProps {
  readonly to: To;
  readonly children: ReactNode;
  readonly className?: string;
  readonly inline?: boolean;
  readonly appearance?: "default" | "subtle";
  readonly "aria-label"?: string;
  readonly "aria-current"?: "page";
  /** Called after an in-app navigation, such as to close the overlay the link sits in. */
  readonly onNavigate?: (() => void) | undefined;
}

/** Whether the browser should handle the click itself: a new tab or window, or a download. */
export function isModifiedClick(event: MouseEvent): boolean {
  return event.button !== 0 || event.metaKey || event.altKey || event.ctrlKey || event.shiftKey;
}

export function RouterLink({ to, children, onNavigate, ...rest }: RouterLinkProps) {
  const navigate = useNavigate();
  const href = useHref(to);
  return (
    <Link
      {...rest}
      href={href}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        if (isModifiedClick(event)) return;
        event.preventDefault();
        void navigate(to);
        onNavigate?.();
      }}
    >
      {children}
    </Link>
  );
}
