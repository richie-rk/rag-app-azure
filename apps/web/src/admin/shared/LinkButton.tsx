// A link that looks like a button, such as New project and New index (10-web-client.md sections
// 7.5 and 7.7): a real `href`, so it reads as a link and opens in a new tab, and router navigation
// on a plain click.

import { Button } from "@fluentui/react-components";
import type { MouseEvent, ReactElement, ReactNode } from "react";
import { useHref, useNavigate, type To } from "react-router";

import { isModifiedClick } from "../../ui/RouterLink";

export interface LinkButtonProps {
  readonly to: To;
  readonly icon?: ReactElement;
  readonly appearance?: "primary" | "secondary";
  readonly children: ReactNode;
}

export function LinkButton({ to, icon, appearance = "secondary", children }: LinkButtonProps) {
  const navigate = useNavigate();
  const href = useHref(to);
  return (
    <Button
      as="a"
      href={href}
      appearance={appearance}
      {...(icon === undefined ? {} : { icon })}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        if (isModifiedClick(event)) return;
        event.preventDefault();
        void navigate(to);
      }}
    >
      {children}
    </Button>
  );
}
