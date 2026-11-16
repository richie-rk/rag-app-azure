import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import { queryKeys } from "../../api/queryClient";
import { sessionDetail, storedTurn, turnIdOf } from "../../test/chat";
import { CURRENT_USER, IDS, apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp, createTestQueryClient, renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import type { StoredFeedback } from "../turns/turnModel";
import { FeedbackControls } from "./FeedbackControls";

const TURN = turnIdOf(1);
const feedbackUrl = apiUrl(`/v1/sessions/${IDS.session}/turns/${TURN}/feedback`);

function saved(fields: Partial<StoredFeedback> = {}): StoredFeedback {
  return {
    rating: "up",
    category: null,
    comment: null,
    updatedAt: "2026-11-05T10:01:00Z",
    ...fields,
  };
}

function recordFeedback(answer: (body: unknown) => Response) {
  const bodies: unknown[] = [];
  server.use(
    http.put(feedbackUrl, async ({ request }) => {
      const body = await request.json();
      bodies.push(body);
      return answer(body);
    }),
  );
  return bodies;
}

function show(stored: StoredFeedback | null = null, feedbackComments = true) {
  configureTestHttp();
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(queryKeys.session(IDS.session), sessionDetail([storedTurn()]));
  queryClient.setQueryData(queryKeys.sessionEarlier(IDS.session), {
    pages: [{ items: [storedTurn()], nextCursor: null }],
    pageParams: ["c"],
  });
  const harness = renderWithProviders(
    <FeedbackControls sessionId={IDS.session} turnId={TURN} stored={stored}>
      <span>copy</span>
    </FeedbackControls>,
    {
      queryClient,
      currentUser: {
        ...CURRENT_USER,
        roles: ["User"],
        features: { memoryEnabled: true, feedbackComments },
      },
    },
  );
  return harness;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("T-391 feedback", () => {
  test("T-391 Helpful shows pressed only after the 200, which is written into the cached turn", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const bodies: unknown[] = [];
    server.use(
      http.put(feedbackUrl, async ({ request }) => {
        bodies.push(await request.json());
        await gate;
        return HttpResponse.json(saved());
      }),
    );
    const { user, queryClient } = show();
    const helpful = screen.getByRole("button", { name: "Helpful" });
    await user.click(helpful);
    expect(helpful).toHaveAttribute("aria-pressed", "false");
    expect(helpful).toBeDisabled();
    expect(screen.getByRole("button", { name: "Not helpful" })).toBeDisabled();
    act(() => {
      release();
    });
    await waitFor(() => {
      expect(helpful).toHaveAttribute("aria-pressed", "true");
    });
    expect(bodies).toEqual([{ rating: "up", category: null, comment: null }]);
    const cached = queryClient.getQueryData<ReturnType<typeof sessionDetail>>(
      queryKeys.session(IDS.session),
    );
    expect(cached?.turns[0]?.feedback).toEqual(saved());
    await user.click(helpful);
    expect(bodies).toHaveLength(1);
  });

  test("T-391 on a 500 Helpful stays unpressed and the error shows with a reference ID", async () => {
    recordFeedback(() => problemResponse(500, "internal_error", { title: "Unexpected error" }));
    const { user } = show();
    await user.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByText("Unexpected error")).toBeInTheDocument();
    expect(screen.getByText(/Reference ID: 0123456789abcdef/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Helpful" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("T-391 a 429 disables the toggles until its time", async () => {
    recordFeedback(() => problemResponse(429, "write_rate_limited", { retryAfterSeconds: 20 }));
    const { user } = show();
    await user.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByText(/You can retry at/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Helpful" })).toBeDisabled();
  });

  test("T-391 the dialog sends a category and comment, or null, and stays open on failure", async () => {
    let fail = true;
    const bodies = recordFeedback((body) =>
      fail
        ? new HttpResponse(null, { status: 502 })
        : HttpResponse.json(
            saved({ ...(body as Partial<StoredFeedback>), updatedAt: "2026-11-05T10:02:00Z" }),
          ),
    );
    const { user } = show();
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    const dialog = await screen.findByRole("dialog", { name: "What went wrong?" });
    await user.click(within(dialog).getByRole("radio", { name: "Incomplete" }));
    await user.type(
      within(dialog).getByRole("textbox", { name: "Comment (optional)" }),
      "  Missing a step.  ",
    );
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    expect(await within(dialog).findByText("Something went wrong")).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: "Incomplete" })).toBeChecked();
    fail = false;
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(bodies.at(-1)).toEqual({
      rating: "down",
      category: "incomplete",
      comment: "Missing a step.",
    });
    expect(await screen.findByRole("button", { name: "Not helpful" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("button", { name: /comment/ })).toBeNull();
  });

  test("T-391 Not helpful while pressed reopens the dialog prefilled; Cancel sends nothing", async () => {
    const bodies = recordFeedback(() => HttpResponse.json(saved()));
    const { user } = show(saved({ rating: "down", category: "other", comment: "Old" }));
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    const dialog = await screen.findByRole("dialog", { name: "What went wrong?" });
    expect(within(dialog).getByRole("radio", { name: "Other" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Comment (optional)" })).toHaveValue("Old");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(bodies).toEqual([]);
  });

  test("T-391 closing the dialog with Escape or Cancel returns focus to the control that opened it", async () => {
    const { user } = show(saved());
    const notHelpful = screen.getByRole("button", { name: "Not helpful" });
    notHelpful.focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog", { name: "What went wrong?" });
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(notHelpful).toHaveFocus();
    });
    const comment = screen.getByRole("button", { name: "Add a comment" });
    await user.click(comment);
    const dialog = await screen.findByRole("dialog", { name: "What made this answer helpful?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(comment).toHaveFocus();
    });
  });

  test("T-391 the comment box is hidden when comments are off", async () => {
    const bodies = recordFeedback(() => HttpResponse.json(saved({ rating: "down" })));
    const { user } = show(null, false);
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    await waitFor(() => {
      expect(bodies).toEqual([{ rating: "down", category: null, comment: null }]);
    });
  });
});

describe("T-392 the feedback comment", () => {
  test("T-392 after Helpful, Add a comment opens up mode and sends the trimmed text; then Edit your comment", async () => {
    const bodies = recordFeedback((body) =>
      HttpResponse.json(saved({ comment: (body as { comment: string | null }).comment })),
    );
    const { user } = show();
    expect(screen.queryByRole("button", { name: "Add a comment" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Helpful" }));
    await user.click(await screen.findByRole("button", { name: "Add a comment" }));
    const dialog = await screen.findByRole("dialog", { name: "What made this answer helpful?" });
    expect(within(dialog).queryByRole("radiogroup")).toBeNull();
    await user.type(within(dialog).getByRole("textbox"), "  Clear and short.  ");
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    expect(await screen.findByRole("button", { name: "Edit your comment" })).toBeInTheDocument();
    expect(bodies.at(-1)).toEqual({ rating: "up", category: null, comment: "Clear and short." });
  });

  test("T-392 a blank comment is sent as null, which removes the saved one", async () => {
    const bodies = recordFeedback(() => HttpResponse.json(saved()));
    const { user } = show(saved({ comment: "Old comment" }));
    await user.click(screen.getByRole("button", { name: "Edit your comment" }));
    const box = within(await screen.findByRole("dialog")).getByRole("textbox");
    expect(box).toHaveValue("Old comment");
    await user.clear(box);
    await user.type(box, "   ");
    await user.click(screen.getByRole("button", { name: "Send feedback" }));
    await waitFor(() => {
      expect(bodies).toEqual([{ rating: "up", category: null, comment: null }]);
    });
    expect(await screen.findByRole("button", { name: "Add a comment" })).toBeInTheDocument();
  });

  test("T-392 a 422 comments_disabled shows on the comment box and refetches the user", async () => {
    recordFeedback(() =>
      problemResponse(422, "validation_failed", {
        errors: [{ path: "body.comment", reason: "comments_disabled" }],
      }),
    );
    const meCalls: number[] = [];
    server.use(
      http.get(apiUrl("/v1/me"), () => {
        meCalls.push(1);
        return HttpResponse.json(CURRENT_USER);
      }),
    );
    const { user } = show(saved());
    await user.click(screen.getByRole("button", { name: "Add a comment" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox"), "Nice");
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    expect(
      await within(dialog).findByText(
        "Comments are turned off by your DocChat administrator, so this comment can't be saved. Remove it to send your rating.",
      ),
    ).toBeInTheDocument();
  });

  test("T-392 422 reasons show on their fields, and unknown paths in the error bar", async () => {
    recordFeedback(() =>
      problemResponse(422, "validation_failed", {
        title: "Some fields aren't valid",
        errors: [
          { path: "body.category", reason: "category_requires_down" },
          { path: "body.comment", reason: "too_long" },
          { path: "body.rating", reason: "invalid_type" },
        ],
      }),
    );
    const { user } = show();
    await user.click(screen.getByRole("button", { name: "Not helpful" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Send feedback" }));
    expect(
      await within(dialog).findByText(
        "A problem type can only be chosen for an answer that wasn't helpful.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Comments can be up to 2,000 characters. Shorten yours, then send it.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Some fields aren't valid")).toBeInTheDocument();
  });

  test("T-392 the comment keeps at most 2,000 code points, with the counter from 1,800", async () => {
    recordFeedback(() => HttpResponse.json(saved()));
    const { user } = show(saved());
    await user.click(screen.getByRole("button", { name: "Add a comment" }));
    const box = within(await screen.findByRole("dialog")).getByRole("textbox");
    await user.click(box);
    await user.paste("😀".repeat(2_100));
    expect(Array.from((box as HTMLTextAreaElement).value)).toHaveLength(2_000);
    expect(screen.getByText(/2,000 of 2,000 characters/)).toBeInTheDocument();
  });
});
