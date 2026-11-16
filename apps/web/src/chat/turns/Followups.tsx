// Follow-up suggestions (10-web-client.md section 8.15): the newest turn's questions as text
// buttons between the message list and the composer. Each shows, and sends, its text with every
// URL redacted and no allowed link, so a follow-up never carries an address.

import { Button, makeStyles, tokens } from "@fluentui/react-components";

import { en } from "../../copy/en";
import { redactRefusedUrls } from "../render/redact";

const useStyles = makeStyles({
  group: { display: "flex", flexWrap: "wrap", gap: tokens.spacingHorizontalS },
  button: { height: "auto", textAlign: "start" },
});

/** What a follow-up shows and sends. */
export function followupText(item: string): string {
  return redactRefusedUrls(item, []);
}

export interface FollowupsProps {
  readonly items: readonly string[];
  readonly disabled: boolean;
  readonly onSend: (question: string) => void;
}

export function Followups({ items, disabled, onSend }: FollowupsProps) {
  const styles = useStyles();
  if (items.length === 0) return null;
  return (
    <div role="group" aria-label={en["chat.followups.label"]} className={styles.group}>
      {items.map((item) => {
        const text = followupText(item);
        return (
          <Button
            key={item}
            appearance="outline"
            shape="circular"
            className={styles.button}
            disabled={disabled}
            onClick={() => {
              onSend(text);
            }}
          >
            {text}
          </Button>
        );
      })}
    </div>
  );
}
