import { TableCell, TableRow } from "@fluentui/react-components";
import { Delete20Regular } from "@fluentui/react-icons";
import { screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { clientError } from "../../api/problem";
import { setViewportWidth } from "../../test/media";
import { renderWithProviders } from "../../test/render";
import { AdminTable, RowActions, ShowMore, TableSkeleton } from "./AdminTable";

describe("admin tables", () => {
  test("a table with header cells, a visually hidden caption and its own scroll region", () => {
    renderWithProviders(
      <AdminTable caption="Projects" headers={["Name", "Status"]}>
        <TableRow>
          <TableCell>Alpha</TableCell>
          <TableCell>Active</TableCell>
        </TableRow>
      </AdminTable>,
    );
    const region = screen.getByRole("region", { name: "Projects" });
    expect(region).toHaveAttribute("tabindex", "0");
    const table = within(region).getByRole("table", { name: "Projects" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Name", "Status"]);
  });

  test("a skeleton of 6 rows by default", () => {
    const { result } = renderWithProviders(<TableSkeleton />);
    expect(result.container.querySelectorAll(".fui-SkeletonItem")).toHaveLength(6);
  });

  test("row actions are buttons with text from 768 px wide and a menu named for the row below", async () => {
    const onRemove = vi.fn();
    const actions = [
      {
        key: "remove",
        label: "Remove",
        accessibleName: "Remove Ada",
        icon: <Delete20Regular />,
        onClick: onRemove,
      },
    ];
    const { user, result } = renderWithProviders(<RowActions name="Ada" actions={actions} />);
    await user.click(screen.getByRole("button", { name: "Remove Ada" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    result.unmount();
    setViewportWidth(600);
    const narrow = renderWithProviders(<RowActions name="Ada" actions={actions} />);
    await narrow.user.click(screen.getByRole("button", { name: "Actions for Ada" }));
    await narrow.user.click(await screen.findByRole("menuitem", { name: "Remove" }));
    expect(onRemove).toHaveBeenCalledTimes(2);
  });

  test("Show more while there is another page; a failed page shows its error bar with Retry", async () => {
    const onShowMore = vi.fn();
    const { user, result } = renderWithProviders(
      <ShowMore hasMore loading={false} onShowMore={onShowMore} />,
    );
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(onShowMore).toHaveBeenCalledTimes(1);
    result.rerender(
      <ShowMore
        hasMore
        loading={false}
        onShowMore={onShowMore}
        error={clientError("network", { traceId: "t1" })}
      />,
    );
    expect(await screen.findByText("Can't reach DocChat")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onShowMore).toHaveBeenCalledTimes(2);
    result.rerender(<ShowMore hasMore={false} loading={false} onShowMore={onShowMore} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
