// A run's progress (10-web-client.md section 7.10): `runs.progress`, where done counts the
// succeeded, skipped, failed and cancelled items, followed by `runs.progressFailed` when any item
// failed. The runs table and the run page's progress bar both show it.

import { makeStyles, tokens } from "@fluentui/react-components";

import type { RunSummary } from "../shared/queries";
import { progressTexts } from "./runDisplay";

const useStyles = makeStyles({
  failed: { color: tokens.colorPaletteRedForeground1 },
});

export function RunProgress({ counts }: { readonly counts: RunSummary["counts"] }) {
  const styles = useStyles();
  const text = progressTexts(counts);
  return (
    <>
      {text.progress}
      {text.failed === null ? null : (
        <>
          {" "}
          <span className={styles.failed}>{text.failed}</span>
        </>
      )}
    </>
  );
}
