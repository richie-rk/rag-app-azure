// Copy answer (10-web-client.md section 8.17): writes only `text/plain`, built from the rendered,
// policy-applied and redacted answer. The icon confirms for 2,000 ms and the status region
// announces it; a failure (client outcome `clipboard_failed`) shows an inline message, with no
// reference ID because no request failed.

import { Button, Text, Tooltip, makeStyles, tokens } from "@fluentui/react-components";
import { Checkmark20Regular, Copy20Regular } from "@fluentui/react-icons";
import { useEffect, useState } from "react";

import { announceStatus } from "../../a11y/announce";
import { en } from "../../copy/en";
import { COPY_CONFIRMATION_MS } from "../../platform/constants";
import { devLogOutcome } from "../../platform/devLog";
import { answerToPlainText } from "../render/copy";
import type { CitationLabel } from "../render/policy";

const useStyles = makeStyles({
  failed: { color: tokens.colorPaletteRedForeground1 },
});

export interface CopyAnswerProps {
  readonly text: string;
  readonly citations: readonly CitationLabel[] | null;
  readonly allowedLinks: readonly string[] | null;
}

export function CopyAnswer({ text, citations, allowedLinks }: CopyAnswerProps) {
  const styles = useStyles();
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state !== "copied") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, COPY_CONFIRMATION_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  const fail = () => {
    setState("failed");
    devLogOutcome("clipboard_failed", null);
  };
  const copy = () => {
    let plain: string;
    try {
      plain = answerToPlainText(text, { citations, allowedLinks });
    } catch {
      fail();
      return;
    }
    navigator.clipboard.writeText(plain).then(() => {
      setState("copied");
      announceStatus(en["chat.turn.copied"]);
    }, fail);
  };

  return (
    <>
      <Tooltip content={en["chat.turn.copy"]} relationship="label">
        <Button
          appearance="subtle"
          icon={state === "copied" ? <Checkmark20Regular /> : <Copy20Regular />}
          onClick={copy}
        />
      </Tooltip>
      {state === "failed" ? (
        <Text className={styles.failed}>{en["chat.turn.copyFailed"]}</Text>
      ) : null}
    </>
  );
}
