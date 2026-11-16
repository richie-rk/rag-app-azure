// The Chat not available state (10-web-client.md section 8.12): a session the caller can't open
// (missing, another user's, or in a project they can no longer reach) shows the problem's own
// title and detail in place of the conversation, with New chat.

import { Button, Text, makeStyles, tokens } from "@fluentui/react-components";

import type { ApiError } from "../../api/problem";
import { en } from "../../copy/en";

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    rowGap: tokens.spacingVerticalM,
    paddingTop: tokens.spacingVerticalXL,
  },
  heading: {
    margin: "0",
    fontSize: tokens.fontSizeBase500,
    lineHeight: tokens.lineHeightBase500,
    fontWeight: tokens.fontWeightSemibold,
  },
});

export interface ChatUnavailableProps {
  readonly error: ApiError;
  readonly onNewChat: () => void;
}

export function ChatUnavailable({ error, onNewChat }: ChatUnavailableProps) {
  const styles = useStyles();
  return (
    <div className={styles.root}>
      <h2 className={styles.heading}>{error.title}</h2>
      <Text>{error.detail}</Text>
      <Button appearance="primary" onClick={onNewChat}>
        {en["chat.unavailable.action"]}
      </Button>
    </div>
  );
}
