// The empty state of a new chat with a ready project (10-web-client.md section 8.4): a decorative
// icon, the heading, the project's description and one card per example question, which sends
// that question at once under the composer's rules.

import { Card, Text, makeStyles, tokens } from "@fluentui/react-components";
import { ChatSparkle48Regular } from "@fluentui/react-icons";
import type { KeyboardEvent } from "react";

import type { ProjectSummary } from "../../api/queryClient";
import { en } from "../../copy/en";
import { BREAKPOINT_MEDIUM_PX } from "../../platform/constants";

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    rowGap: tokens.spacingVerticalM,
    paddingTop: tokens.spacingVerticalXXL,
    textAlign: "center",
  },
  icon: { color: tokens.colorBrandForeground1 },
  heading: {
    margin: "0",
    fontSize: tokens.fontSizeBase500,
    lineHeight: tokens.lineHeightBase500,
    fontWeight: tokens.fontWeightSemibold,
  },
  examples: {
    display: "grid",
    gridTemplateColumns: "1fr",
    gap: tokens.spacingHorizontalM,
    width: "100%",
    [`@media (min-width: ${String(BREAKPOINT_MEDIUM_PX)}px)`]: { gridTemplateColumns: "1fr 1fr" },
  },
  card: { textAlign: "start", cursor: "pointer" },
});

export interface EmptyStateProps {
  readonly project: ProjectSummary;
  /** While sending is blocked the cards are disabled. */
  readonly disabled: boolean;
  readonly onSend: (question: string) => void;
}

export function EmptyState({ project, disabled, onSend }: EmptyStateProps) {
  const styles = useStyles();
  const activate = (question: string) => {
    if (!disabled) onSend(question);
  };
  return (
    <div className={styles.root}>
      <ChatSparkle48Regular aria-hidden="true" className={styles.icon} />
      <h2 className={styles.heading}>{en["chat.empty.title"]}</h2>
      <Text>{project.description === "" ? en["chat.empty.body"] : project.description}</Text>
      {project.exampleQuestions.length === 0 ? null : (
        <>
          <Text weight="semibold">{en["chat.empty.examples"]}</Text>
          <div className={styles.examples}>
            {project.exampleQuestions.map((question) => (
              <Card
                key={question}
                appearance="outline"
                role="button"
                className={styles.card}
                disabled={disabled}
                onClick={() => {
                  activate(question);
                }}
                onKeyDown={(event: KeyboardEvent) => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  activate(question);
                }}
              >
                <Text>{question}</Text>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
