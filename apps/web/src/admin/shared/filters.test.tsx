import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import {
  FilterDropdown,
  FilterRow,
  FilterSearchBox,
  isCanonicalUuid,
  useDebouncedValue,
  useUrlFilters,
  type UrlFilter,
} from "./filters";

const FILTERS: readonly UrlFilter[] = [
  { name: "q", allowed: (value) => value.length <= 5, fallback: "" },
  { name: "state", allowed: ["all", "failed"], fallback: "all" },
];

function Filters() {
  const { values, setFilter } = useUrlFilters(FILTERS);
  return (
    <FilterRow>
      <FilterSearchBox
        label="Filter by name"
        value={values["q"] ?? ""}
        maxLength={5}
        onSearch={(value) => {
          setFilter("q", value);
        }}
      />
      <FilterDropdown
        text={{ label: "State", options: { all: "All", failed: "Failed" } }}
        values={["all", "failed"]}
        value={values["state"] ?? "all"}
        onChange={(value) => {
          setFilter("state", value);
        }}
      />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </FilterRow>
  );
}

describe("URL filters", () => {
  test("an ID filter allows canonical UUIDs only", () => {
    expect(isCanonicalUuid("00000000-0000-4000-8000-000000000500")).toBe(true);
    expect(isCanonicalUuid("00000000-0000-4000-8000-00000000050A")).toBe(false);
    expect(isCanonicalUuid("not-an-id")).toBe(false);
  });

  test("a value settles after 300 ms", () => {
    vi.useFakeTimers();
    try {
      const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value), {
        initialProps: { value: "a" },
      });
      rerender({ value: "ab" });
      act(() => {
        vi.advanceTimersByTime(299);
      });
      expect(result.current).toBe("a");
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(result.current).toBe("ab");
    } finally {
      vi.useRealTimers();
    }
  });

  test("values come from the URL; invalid ones are dropped with replace", async () => {
    const { router } = renderWithProviders(<Filters />, {
      route: "/list?q=toolongvalue&state=failed&other=1",
    });
    await waitFor(() => {
      expect(router.state.location.search).toBe("?state=failed&other=1");
    });
    expect(router.state.historyAction).toBe("REPLACE");
    expect(screen.getByTestId("values")).toHaveTextContent('{"q":"","state":"failed"}');
  });

  test("typing reaches the URL after the debounce; a fallback value is removed", async () => {
    const { user, router } = renderWithProviders(<Filters />, { route: "/list?state=failed" });
    await user.type(screen.getByRole("searchbox", { name: "Filter by name" }), " ab ");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?state=failed&q=ab");
    });
    await user.click(screen.getByRole("combobox", { name: "State" }));
    await user.click(await screen.findByRole("option", { name: "All" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=ab");
    });
    await act(() => router.navigate("/list?q=xy"));
    expect(screen.getByRole("searchbox", { name: "Filter by name" })).toHaveValue("xy");
    await user.clear(screen.getByRole("searchbox", { name: "Filter by name" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
  });
});
