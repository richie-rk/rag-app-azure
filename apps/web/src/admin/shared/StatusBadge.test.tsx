import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { displayNames } from "../../copy/en";
import { BADGE_COLORS, StatusBadge, type StatusKind } from "./StatusBadge";

describe("status badges", () => {
  test("every badge value has a colour and shows its display name", () => {
    for (const kind of Object.keys(BADGE_COLORS) as StatusKind[]) {
      expect(Object.keys(BADGE_COLORS[kind]).sort()).toEqual(
        Object.keys(displayNames[kind]).sort(),
      );
    }
  });

  test.each([
    ["runState", "queued", "informative"],
    ["runState", "running", "brand"],
    ["runState", "completed", "success"],
    ["runState", "completed_with_errors", "warning"],
    ["runState", "failed", "danger"],
    ["itemState", "succeeded", "success"],
    ["itemState", "skipped", "informative"],
    ["itemState", "cancelled", "warning"],
    ["versionState", "pending", "informative"],
    ["documentDisplayState", "ingested", "success"],
    ["documentDisplayState", "no_content", "warning"],
    ["scanStatus", "awaiting", "informative"],
    ["scanStatus", "clean", "success"],
    ["scanStatus", "malicious", "danger"],
    ["scanStatus", "unavailable", "danger"],
    ["indexStatus", "provisioning", "brand"],
    ["indexStatus", "reingesting", "brand"],
    ["indexStatus", "rebuilding", "brand"],
    ["indexStatus", "deleting", "brand"],
    ["indexStatus", "ready", "success"],
    ["indexStatus", "reingest_required", "warning"],
    ["projectStatus", "active", "success"],
    ["projectStatus", "archived", "warning"],
  ] as const)("section 7.12: %s %s is %s", (kind, value, color) => {
    expect((BADGE_COLORS[kind] as Record<string, string>)[value]).toBe(color);
  });

  test("the badge carries its text, so colour is never the only signal", () => {
    render(
      <FluentProvider theme={webLightTheme}>
        <StatusBadge kind="indexStatus" value="reingest_required" />
      </FluentProvider>,
    );
    expect(screen.getByText("Re-ingest needed")).toBeInTheDocument();
  });
});
