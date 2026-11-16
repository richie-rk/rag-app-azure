import { render } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import * as announce from "../../a11y/announce";
import { runItem, runSummary } from "../../test/adminData";
import type { RunSummary } from "../shared/queries";
import {
  doneCount,
  isOpenRun,
  itemName,
  problemText,
  progressTexts,
  startedByText,
  useRunsFinishedAnnouncement,
} from "./runDisplay";

function Watch({ runs }: { readonly runs: readonly RunSummary[] | undefined }) {
  useRunsFinishedAnnouncement(runs);
  return null;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("run display", () => {
  test("open runs are queued or running", () => {
    expect(isOpenRun("queued")).toBe(true);
    expect(isOpenRun("running")).toBe(true);
    expect(isOpenRun("completed")).toBe(false);
    expect(isOpenRun("completed_with_errors")).toBe(false);
    expect(isOpenRun("failed")).toBe(false);
  });

  test("done counts succeeded, skipped, failed and cancelled items; failures follow the progress", () => {
    const counts = {
      total: 9,
      queued: 1,
      running: 1,
      succeeded: 3,
      skipped: 1,
      failed: 2,
      cancelled: 1,
    };
    expect(doneCount(counts)).toBe(7);
    expect(progressTexts(counts)).toEqual({ progress: "7 of 9 documents", failed: "2 failed" });
    expect(progressTexts({ ...counts, failed: 0 })).toEqual({
      progress: "5 of 9 documents",
      failed: null,
    });
  });

  test("Started by: the requester, or System when the run names none", () => {
    expect(startedByText(runSummary(1))).toBe("Grace Hopper");
    expect(startedByText(runSummary(1, { requestedBy: null, requestedByName: null }))).toBe(
      "System",
    );
  });

  test("an item's problem and name", () => {
    expect(problemText("The file couldn't be read.", "parse_failed")).toBe(
      "The file couldn't be read. (parse_failed)",
    );
    expect(problemText("Only a message.", null)).toBe("Only a message.");
    expect(problemText(null, "only_code")).toBe("only_code");
    expect(problemText(null, null)).toBe("");
    expect(itemName(runItem(1))).toBe("document-1.pdf");
    expect(itemName(runItem(2, { document: null, operation: "purge_index_blobs" }))).toBe(
      "Remove index files",
    );
  });

  test("a watched run that finishes is announced once; runs seen first as final are not", () => {
    const spy = vi.spyOn(announce, "announceStatus").mockImplementation(() => undefined);
    const { rerender } = render(<Watch runs={undefined} />);
    rerender(
      <Watch runs={[runSummary(1, { state: "running" }), runSummary(2, { state: "completed" })]} />,
    );
    expect(spy).not.toHaveBeenCalled();
    rerender(
      <Watch
        runs={[
          runSummary(1, { state: "completed_with_errors" }),
          runSummary(2, { state: "completed" }),
        ]}
      />,
    );
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("Run finished: Completed with errors.");
    rerender(<Watch runs={[runSummary(1, { state: "completed_with_errors" })]} />);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
