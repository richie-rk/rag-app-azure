import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import { etag, fact, memoryView } from "../test/adminData";
import { apiUrl, problemResponse } from "../test/handlers";
import { configureTestHttp, renderWithProviders } from "../test/render";
import { server } from "../test/server";
import { formatDate, formatRetryTime } from "../ui/format";
import type { MemoryView } from "./MemoryScreen";
import { MemoryScreen } from "./MemoryScreen";

interface Sent {
  readonly method: string;
  readonly path: string;
  readonly ifMatch: string | null;
  readonly body: unknown;
}

/** The memory API: GET answers `current()`; each write answers `write(sent)`. */
function memoryApi(
  current: () => MemoryView | Response,
  write: (sent: Sent) => MemoryView | Response = () => memoryView(),
) {
  const sent: Sent[] = [];
  const answer = (value: MemoryView | Response) =>
    value instanceof Response ? value : HttpResponse.json(value);
  const record = async (request: Request): Promise<Response> => {
    const text = await request.text();
    const entry: Sent = {
      method: request.method,
      path: new URL(request.url).pathname,
      ifMatch: request.headers.get("If-Match"),
      body: text === "" ? null : (JSON.parse(text) as unknown),
    };
    sent.push(entry);
    return answer(write(entry));
  };
  server.use(
    http.get(apiUrl("/v1/memory"), () => answer(current())),
    http.patch(apiUrl("/v1/memory"), ({ request }) => record(request)),
    http.put(apiUrl("/v1/memory/facts/:factId"), ({ request }) => record(request)),
    http.delete(apiUrl("/v1/memory/facts/:factId"), ({ request }) => record(request)),
    http.delete(apiUrl("/v1/memory/facts"), ({ request }) => record(request)),
  );
  return sent;
}

function show() {
  configureTestHttp();
  return renderWithProviders(<MemoryScreen />, { route: "/memory" });
}

function statusRegion(): HTMLElement {
  const region = document.querySelector<HTMLElement>("[role='status']");
  if (region === null) throw new Error("no status region");
  return region;
}

describe("the Memory screen", () => {
  test("shows the switch, usage, each memory with its category and date, in the API's order", async () => {
    memoryApi(() => memoryView());
    show();
    expect(screen.getByRole("heading", { level: 1, name: "Memory" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Use memory" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeDisabled();
    const list = await screen.findByRole("list", { name: "Saved details" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("Fact number 1Topic"),
      expect.stringContaining("Fact number 2Role"),
    ]);
    expect(within(list).getAllByText(`Updated ${formatDate(fact(1).updatedAt)}`)).toHaveLength(2);
    expect(screen.getByRole("switch", { name: "Use memory" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeEnabled();
    expect(screen.getByText("2 of 50 memories saved")).toBeInTheDocument();
    expect(screen.getByText("12% of memory space used")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit memory: Fact number 1" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete memory: Fact number 2" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Memory · DocChat");
  });

  test("the information bars: off for everyone, or paused", async () => {
    memoryApi(() => memoryView({ enabled: false, paused: true }));
    const { result } = show();
    expect(
      await screen.findByText(/Memory is turned off for everyone by your DocChat administrator/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Memory is paused/)).toBeNull();
    expect(screen.getByRole("switch", { name: "Use memory" })).not.toBeChecked();
    result.unmount();
    memoryApi(() => memoryView({ paused: true }));
    show();
    expect(await screen.findByText(/Memory is paused\./)).toBeInTheDocument();
  });

  test("Empty: memory.empty, with Clear all disabled", async () => {
    memoryApi(() => memoryView({ facts: [] }));
    show();
    expect(
      await screen.findByText("Nothing saved yet. Details you share in chats will appear here."),
    ).toBeInTheDocument();
    expect(screen.getByText("0 of 50 memories saved")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeDisabled();
  });

  test("Error: memory.loadError with Retry, which refetches", async () => {
    let fail = true;
    memoryApi(() =>
      fail
        ? problemResponse(503, "dependency_unavailable", { title: "Service unavailable" })
        : memoryView(),
    );
    const { user } = show();
    expect(await screen.findByText("Your memory didn't load.")).toBeInTheDocument();
    expect(screen.getByText("Service unavailable")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("list", { name: "Saved details" })).toBeInTheDocument();
  });

  test("T-394 every action sends If-Match with the current ETag and takes the answer's", async () => {
    let version = 1;
    let view = memoryView();
    const sent = memoryApi(
      () => view,
      (entry) => {
        version += 1;
        if (entry.path === "/v1/memory") {
          view = memoryView({ paused: true, etag: etag(version) });
        } else if (entry.method === "PUT") {
          view = memoryView({
            etag: etag(version),
            facts: [fact(1, { text: "Works on renewals", category: "role" }), fact(2)],
          });
        } else if (entry.path === "/v1/memory/facts") {
          view = memoryView({ etag: etag(version), facts: [] });
        } else {
          view = memoryView({ etag: etag(version), facts: [fact(2)] });
        }
        return view;
      },
    );
    const { user } = show();
    const toggle = await screen.findByRole("switch", { name: "Use memory" });
    await user.click(toggle);
    await waitFor(() => {
      expect(statusRegion()).toHaveTextContent("Memory paused.");
    });
    expect(sent[0]).toMatchObject({ method: "PATCH", ifMatch: etag(1), body: { paused: true } });
    expect(await screen.findByText(/Memory is paused\./)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit memory: Fact number 1" }));
    const box = screen.getByRole("textbox", { name: "Memory" });
    expect(box).toHaveFocus();
    await user.clear(box);
    await user.type(box, "  Works on renewals  ");
    await user.click(screen.getByRole("combobox", { name: "Category" }));
    await user.click(await screen.findByRole("option", { name: "Role" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(statusRegion()).toHaveTextContent("Memory updated.");
    });
    expect(sent[1]).toMatchObject({
      method: "PUT",
      path: `/v1/memory/facts/${fact(1).factId}`,
      ifMatch: etag(2),
      body: { text: "Works on renewals", category: "role" },
    });
    expect(await screen.findByText("Works on renewals")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Memory" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Delete memory: Works on renewals" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this memory?" });
    expect(within(dialog).getByText(/DocChat stops using it in answers/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(statusRegion()).toHaveTextContent("Memory deleted.");
    });
    expect(sent[2]).toMatchObject({ method: "DELETE", ifMatch: etag(3) });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    await user.click(await screen.findByRole("button", { name: "Clear all" }));
    const clear = await screen.findByRole("dialog", { name: "Clear all memories?" });
    await user.click(within(clear).getByRole("button", { name: "Clear all" }));
    await waitFor(() => {
      expect(statusRegion()).toHaveTextContent("All memories cleared.");
    });
    expect(sent[3]).toMatchObject({ method: "DELETE", path: "/v1/memory/facts", ifMatch: etag(4) });
    expect(await screen.findByText(/Nothing saved yet/)).toBeInTheDocument();
  });

  test("T-394 a 412 refetches, shows memory.conflict and keeps the open editor's text", async () => {
    let gets = 0;
    memoryApi(
      () => {
        gets += 1;
        return gets === 1 ? memoryView() : memoryView({ etag: etag(5) });
      },
      () =>
        problemResponse(412, "precondition_failed", {
          title: "Changed by someone else",
          detail: "Someone else changed this item.",
        }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    const box = screen.getByRole("textbox", { name: "Memory" });
    await user.type(box, " today");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "The latest version is shown now. Make your change once more if you still need it.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Changed by someone else")).toBeInTheDocument();
    expect(gets).toBe(2);
    expect(screen.getByRole("textbox", { name: "Memory" })).toHaveValue("Fact number 1 today");
  });

  test("T-394 a 412 for a memory that is gone closes its editor and shows the bar above the list", async () => {
    let gets = 0;
    memoryApi(
      () => {
        gets += 1;
        return gets === 1 ? memoryView() : memoryView({ etag: etag(5), facts: [fact(2)] });
      },
      () => problemResponse(412, "precondition_failed", { title: "Changed by someone else" }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Changed by someone else")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Memory" })).toBeNull();
    expect(screen.queryByText("Fact number 1")).toBeNull();
  });

  test.each([
    ["empty", "Enter some text for this memory."],
    ["contains_link", "Memories can't include web addresses. Remove the link, then save."],
    ["contains_code", "Memories can't include code. Describe it in plain words instead."],
    [
      "contains_instruction",
      "Write this as a fact about you, such as your role or how you like answers, rather than as an instruction.",
    ],
    [
      "contains_secret",
      "This looks like a password or other secret. Don't keep secrets in memory.",
    ],
    ["duplicate_fact", "You already have a memory that says this."],
    ["memory_full", "Your memory is full. Delete or shorten other memories, then save this one."],
    ["invalid_characters", "This value isn't valid. Check it and correct it."],
  ])("T-394 a 422 with reason %s shows its text on the text box", async (reason, text) => {
    memoryApi(
      () => memoryView(),
      () => problemResponse(422, "validation_failed", { errors: [{ path: "body.text", reason }] }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    await user.type(screen.getByRole("textbox", { name: "Memory" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Memory" })).toHaveValue("Fact number 1!");
    expect(screen.getByRole("textbox", { name: "Memory" })).toHaveAttribute("aria-invalid", "true");
  });

  test("T-394 a 422 at body.category marks the category; other paths go to the error bar", async () => {
    memoryApi(
      () => memoryView(),
      () =>
        problemResponse(422, "validation_failed", {
          title: "Invalid input",
          errors: [
            { path: "body.category", reason: "not_allowed" },
            { path: "header.If-Match", reason: "invalid_format" },
          ],
        }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("This value isn't valid. Check it and correct it."),
    ).toBeInTheDocument();
    expect(screen.getByText("header.If-Match: invalid_format")).toBeInTheDocument();
    expect(screen.getByText("Invalid input")).toBeInTheDocument();
  });

  test("T-394 a 503 on Save keeps the editor and its text, with saveUnavailable and the reference ID", async () => {
    memoryApi(
      () => memoryView(),
      () =>
        problemResponse(503, "dependency_unavailable", {
          title: "Service unavailable",
          retryAfterSeconds: 5,
          correlationId: "fedcba9876543210fedcba9876543210",
        }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    await user.type(screen.getByRole("textbox", { name: "Memory" }), " x");
    await user.click(screen.getByRole("combobox", { name: "Category" }));
    await user.click(await screen.findByRole("option", { name: "Team" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("Your text is still here. Select Save to retry."),
    ).toBeInTheDocument();
    expect(screen.getByText("Reference ID: fedcba9876543210fedcba9876543210")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Memory" })).toHaveValue("Fact number 1 x");
    expect(screen.getByRole("combobox", { name: "Category" })).toHaveTextContent("Team");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  test("a 404 memory_fact_not_found refetches and shows memory.factGone", async () => {
    let gets = 0;
    memoryApi(
      () => {
        gets += 1;
        return gets === 1 ? memoryView() : memoryView({ facts: [fact(2)] });
      },
      () =>
        problemResponse(404, "memory_fact_not_found", {
          title: "Memory item not found",
          detail: "That memory item doesn't exist.",
        }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Delete memory: Fact number 1" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("The list is up to date now.")).toBeInTheDocument();
    expect(screen.getByText("Memory item not found")).toBeInTheDocument();
    expect(screen.queryByText("Fact number 1")).toBeNull();
  });

  test("a 429 on the switch shows the retry time and keeps the switch disabled until then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      memoryApi(
        () => memoryView(),
        () =>
          problemResponse(429, "write_rate_limited", {
            title: "Too many changes",
            retryAfterSeconds: 20,
          }),
      );
      const { user } = show();
      const toggle = await screen.findByRole("switch", { name: "Use memory" });
      const sentAt = Date.now();
      await user.click(toggle);
      expect(await screen.findByText("Too many changes")).toBeInTheDocument();
      expect(screen.getByText(/You can retry at/)).toBeInTheDocument();
      expect(
        screen.getByText(new RegExp(formatRetryTime(sentAt + 20_000).slice(0, 4))),
      ).toBeInTheDocument();
      expect(toggle).toBeDisabled();
      expect(toggle).toBeChecked();
      await vi.advanceTimersByTimeAsync(21_000);
      await waitFor(() => {
        expect(screen.getByRole("switch", { name: "Use memory" })).toBeEnabled();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  test("a 428 on Clear all shows the defect line", async () => {
    memoryApi(
      () => memoryView(),
      () => problemResponse(428, "precondition_required", { title: "Version needed" }),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Clear all" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear all" }),
    );
    expect(await screen.findByText("Version needed")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Reload the page. If this keeps happening, send the reference ID to your DocChat administrator.",
      ),
    ).toBeInTheDocument();
  });

  test("T-396 the fact box keeps 800 code points; Save is blocked above maxFactChars", async () => {
    const sent = memoryApi(() => memoryView());
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Edit memory: Fact number 1" }));
    const box = screen.getByRole("textbox", { name: "Memory" });
    await user.clear(box);
    await user.click(box);
    await user.paste(`😀${"a".repeat(899)}`);
    expect(Array.from((box as HTMLTextAreaElement).value)).toHaveLength(800);
    expect((box as HTMLTextAreaElement).value.startsWith("😀")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("A memory can be up to 200 characters. Shorten it, then save."),
    ).toBeInTheDocument();
    await user.clear(box);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Enter some text for this memory.")).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Memory" })).toBeNull();
  });

  test("T-396 the usage line and bar follow limits and each write's answer", async () => {
    let view = memoryView({
      limits: { maxFacts: 10, maxFactChars: 200, maxTokens: 1000, usedFacts: 1, usedTokens: 899 },
    });
    memoryApi(
      () => view,
      () => {
        view = memoryView({
          paused: true,
          etag: etag(2),
          limits: {
            maxFacts: 10,
            maxFactChars: 200,
            maxTokens: 1000,
            usedFacts: 2,
            usedTokens: 1000,
          },
        });
        return view;
      },
    );
    const { user } = show();
    expect(await screen.findByText("1 of 10 memories saved")).toBeInTheDocument();
    expect(screen.getByText("89% of memory space used")).toBeInTheDocument();
    expect(document.querySelector("[data-usage-color]")).toHaveAttribute(
      "data-usage-color",
      "brand",
    );
    await user.click(screen.getByRole("switch", { name: "Use memory" }));
    expect(await screen.findByText("2 of 10 memories saved")).toBeInTheDocument();
    expect(screen.getByText("100% of memory space used")).toBeInTheDocument();
    expect(document.querySelector("[data-usage-color]")).toHaveAttribute(
      "data-usage-color",
      "error",
    );
  });
});
