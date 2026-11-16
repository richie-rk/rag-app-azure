// The Sources list under an answer (10-web-client.md section 8.14): one button per citation,
// reading "<title>, <locator>", each opening the citation viewer. The locator is the page of the
// PDF the viewer shows, for every format.

import { Button, makeStyles, tokens } from "@fluentui/react-components";
import { useId } from "react";

import { en } from "../../copy/en";
import { fillText } from "../../ui/format";
import type { Citation } from "../stream/events";

const useStyles = makeStyles({
  heading: {
    margin: "0",
    fontSize: tokens.fontSizeBase300,
    lineHeight: tokens.lineHeightBase300,
    fontWeight: tokens.fontWeightSemibold,
  },
  list: { margin: "0", paddingLeft: "24px" },
  button: {
    minHeight: "32px",
    height: "auto",
    textAlign: "start",
    justifyContent: "flex-start",
    fontWeight: tokens.fontWeightRegular,
  },
});

/** A citation's button text. */
export function sourceText(citation: Citation): string {
  return `${citation.title}, ${fillText(en["chat.locator.page"], { page: citation.pageNumber })}`;
}

export interface SourcesListProps {
  readonly citations: readonly Citation[];
  readonly onOpen: (citation: Citation, button: HTMLElement) => void;
}

export function SourcesList({ citations, onOpen }: SourcesListProps) {
  const styles = useStyles();
  const headingId = useId();
  if (citations.length === 0) return null;
  return (
    <section aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        {en["chat.turn.sources"]}
      </h3>
      <ol className={styles.list}>
        {citations.map((citation) => (
          <li key={citation.number}>
            <Button
              appearance="subtle"
              className={styles.button}
              onClick={(event) => {
                onOpen(citation, event.currentTarget);
              }}
            >
              {sourceText(citation)}
            </Button>
          </li>
        ))}
      </ol>
    </section>
  );
}
