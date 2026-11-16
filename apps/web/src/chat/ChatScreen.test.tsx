import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test } from "vitest";

import { initTokens } from "../auth/tokens";
import { abortAllWork } from "../platform/activeWork";
import { sessionDetail, sessionSummary, storedTurn } from "../test/chat";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { ADMIN_USER, IDS, PROJECTS, TEST_CONFIG, apiUrl, problemResponse } from "../test/handlers";
import { setViewportWidth } from "../test/media";
import { configureTestHttp, renderApp } from "../test/render";
import { server } from "../test/server";
import {
  citationsEvent,
  deltaEvent,
  doneEvent,
  followupsEvent,
  metaEvent,
  ndjson,
  statusEvent,
} from "../test/streamEvents";
import { createTurnSpeech } from "./render/speakable";
import { act as reactAct } from "react";
import { resetDraftsForTests } from "./composer/drafts";
import { blockedUntilOf, questionReasonText } from "./ChatScreen";
import { clientError } from "../api/problem";
import { liveTurn, present } from "../test/chat";

afterEach(() => {
  abortAllWork();
  reactAct(() => {
    resetDraftsForTests();
  });
});

function signedIn() {
  configureTestHttp();
  initTokens({ msal: fakeMsal().msal, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
}

interface StreamedTurn {
  readonly bodies: Record<string, unknown>[];
  readonly send: (...events: readonly unknown[]) => void;
  readonly close: () => void;
}

/** `createChatTurn` answered with a stream the test feeds; an abort errors the body, as a browser
 * does. */
function streamTurns(): StreamedTurn {
  const bodies: Record<string, unknown>[] = [];
  const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
  server.use(
    http.post(apiUrl("/v1/chat/turns"), async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>);
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controllers.push(controller);
        },
      });
      request.signal.addEventListener("abort", () => {
        try {
          controllers.at(-1)?.error(new DOMException("Aborted", "AbortError"));
        } catch {
          // Already closed.
        }
      });
      return new HttpResponse(stream, { headers: { "Content-Type": "application/x-ndjson" } });
    }),
  );
  return {
    bodies,
    send: (...events) => {
      act(() => {
        controllers.at(-1)?.enqueue(ndjson(...events));
      });
    },
    close: () => {
      controllers.at(-1)?.close();
    },
  };
}

function sessions(items: readonly Record<string, unknown>[]) {
  server.use(
    http.get(apiUrl("/v1/sessions"), () => HttpResponse.json({ items, nextCursor: null })),
  );
}

function session(detail: ReturnType<typeof sessionDetail>) {
  server.use(http.get(apiUrl(`/v1/sessions/${IDS.session}`), () => HttpResponse.json(detail)));
}

/** The composer, once a project is chosen and it accepts text. */
async function composer() {
  const box = await screen.findByRole("textbox", { name: "Your question" });
  await waitFor(() => {
    expect(box).toBeEnabled();
  });
  return box;
}

describe("T-369 a live turn", () => {
  test("T-369 a new chat streams, takes its session's URL after meta, and is blocked until citations", async () => {
    signedIn();
    const turns = streamTurns();
    let stored = sessionDetail([
      storedTurn({ seq: 1, status: "streaming", answer: "", turnId: IDS.turn }),
    ]);
    server.use(http.get(apiUrl(`/v1/sessions/${IDS.session}`), () => HttpResponse.json(stored)));
    const { user, router } = renderApp({ route: "/chat", speech: createTurnSpeech() });
    expect(
      await screen.findByRole("heading", { level: 2, name: "What would you like to know?" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(router.state.location.search).toBe(`?project=${IDS.projectA}`);
    });
    const box = await composer();
    await user.type(box, "How long do I have?{Enter}");
    await waitFor(() => {
      expect(turns.bodies).toHaveLength(1);
    });
    const body = present(turns.bodies[0]);
    expect(Object.keys(body).sort()).toEqual(["clientTurnId", "projectId", "question"]);
    expect(body).toMatchObject({ projectId: IDS.projectA, question: "How long do I have?" });
    expect(body["clientTurnId"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
    expect(screen.getByText("Searching your documents…")).toBeInTheDocument();
    turns.send(metaEvent({ sessionCreated: true }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/chat/${IDS.session}`);
    });
    // Taking the session's URL isn't an arrival: focus stays in the composer.
    expect(box).toHaveFocus();
    turns.send(statusEvent());
    expect(
      await screen.findByText("Searching more broadly for better matches"),
    ).toBeInTheDocument();
    turns.send(deltaEvent("Claims are due in 30 days ⟦1⟧. More follows"));
    const conversation = screen.getByRole("region", { name: "Conversation" });
    expect(await within(conversation).findByText(/Claims are due in 30 days/)).toBeInTheDocument();
    expect(screen.queryByText("This answer is still being written.")).toBeNull();
    const stop = screen.getByRole("button", { name: "Stop answer" });
    expect(stop).toBeInTheDocument();
    await user.type(box, "Next question{Enter}");
    expect(turns.bodies).toHaveLength(1);
    const log = present(document.querySelector("[role=log]"));
    expect(log.textContent).toContain("Claims are due in 30 days.");
    expect(conversation.querySelector("[aria-live]")).toBeNull();
    turns.send(citationsEvent(1), followupsEvent(["What about receipts?"]));
    expect(await screen.findByRole("button", { name: "Send question" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Source 1, page 1" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Suggested questions" })).toHaveTextContent(
      "What about receipts?",
    );
    stored = sessionDetail([
      storedTurn({
        seq: 1,
        turnId: IDS.turn,
        question: "How long do I have?",
        answer: "Stored answer ⟦1⟧.",
      }),
    ]);
    turns.send(doneEvent());
    turns.close();
    expect(await within(conversation).findByText(/Stored answer/)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Chat: Expense claims" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Expense claims · DocChat");
  });

  test("T-373 a new chat taking its session's URL keeps focus in the composer, which stays enabled before getSession answers", async () => {
    signedIn();
    const turns = streamTurns();
    let answerSession: () => void = () => undefined;
    const sessionRead = new Promise<void>((resolve) => {
      answerSession = resolve;
    });
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), async () => {
        await sessionRead;
        return HttpResponse.json(
          sessionDetail([
            storedTurn({ seq: 1, status: "streaming", answer: "", turnId: IDS.turn }),
          ]),
        );
      }),
    );
    const { user, router } = renderApp({ route: "/chat", speech: createTurnSpeech() });
    const box = await composer();
    // A browser moves focus out of a control that becomes disabled (jsdom doesn't), so the
    // composer must never be disabled on the way.
    const disabledSeen: boolean[] = [];
    const observer = new MutationObserver(() => {
      disabledSeen.push(box.hasAttribute("disabled"));
    });
    observer.observe(box, { attributes: true, attributeFilter: ["disabled"] });
    await user.type(box, "How long do I have?{Enter}");
    await waitFor(() => {
      expect(turns.bodies).toHaveLength(1);
    });
    expect(box).toHaveFocus();
    turns.send(metaEvent({ sessionCreated: true }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/chat/${IDS.session}`);
    });
    expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent(PROJECTS[0].name);
    expect(box).toBeEnabled();
    expect(box).toHaveFocus();
    act(() => {
      answerSession();
    });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Chat: Expense claims" }),
    ).toBeInTheDocument();
    expect(box).toBeEnabled();
    expect(box).toHaveFocus();
    observer.disconnect();
    expect(disabledSeen).not.toContain(true);
  });

  test("T-369 Stop by Escape keeps the partial answer with its caption, and Retry sends a new clientTurnId", async () => {
    signedIn();
    sessions([sessionSummary()]);
    session(sessionDetail([storedTurn({ seq: 1 })]));
    const turns = streamTurns();
    const { user } = renderApp({ route: `/chat/${IDS.session}` });
    expect(await screen.findByRole("article", { name: "Question 1" })).toBeInTheDocument();
    const box = await composer();
    await user.type(box, "Second?{Enter}");
    await waitFor(() => {
      expect(turns.bodies).toHaveLength(1);
    });
    expect(turns.bodies[0]).toMatchObject({ sessionId: IDS.session, question: "Second?" });
    turns.send(
      metaEvent({ turnId: "9aacbf6d-7e8f-4091-92a3-b4c5d6e7f809" }),
      deltaEvent("Partial"),
    );
    expect(await screen.findByText("Partial")).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(await screen.findByText("You stopped this answer.")).toBeInTheDocument();
    expect(screen.getByText("Partial")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(turns.bodies).toHaveLength(2);
    });
    expect(turns.bodies[1]).toMatchObject({ sessionId: IDS.session, question: "Second?" });
    expect(turns.bodies[1]?.["clientTurnId"]).not.toBe(turns.bodies[0]?.["clientTurnId"]);
    expect(screen.getAllByRole("article", { name: "Second?" })).toHaveLength(2);
  });

  test("T-372 a 422 puts the question back in the composer with its reason and no Retry", async () => {
    signedIn();
    server.use(
      http.post(apiUrl("/v1/chat/turns"), () =>
        problemResponse(422, "validation_failed", {
          errors: [{ path: "body.question", reason: "invalid_characters" }],
        }),
      ),
    );
    const { user } = renderApp({ route: "/chat" });
    const box = await composer();
    await user.type(box, "Bad question{Enter}");
    expect(
      await screen.findByText(
        "Your question contains characters DocChat can't accept. Remove them, then send it.",
      ),
    ).toBeInTheDocument();
    expect(box).toHaveValue("Bad question");
    expect(screen.queryByRole("article")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("T-369 an example question sends at once", async () => {
    signedIn();
    const turns = streamTurns();
    const { user } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: "What is alpha?" }));
    await waitFor(() => {
      expect(turns.bodies[0]).toMatchObject({ question: "What is alpha?" });
    });
  });
});

describe("opening a session", () => {
  test("renders stored turns oldest first, with follow-ups for the newest completed turn only", async () => {
    signedIn();
    sessions([sessionSummary()]);
    session(
      sessionDetail([
        storedTurn({ seq: 1, followups: ["Old follow-up?"] }),
        storedTurn({
          seq: 2,
          followups: ["New follow-up?"],
          feedback: {
            rating: "up",
            category: null,
            comment: null,
            updatedAt: "2026-11-05T10:00:00Z",
          },
        }),
      ]),
    );
    renderApp({ route: `/chat/${IDS.session}` });
    const articles = await screen.findAllByRole("article");
    expect(articles.map((article) => within(article).getByText(/^Question/).textContent)).toEqual([
      "Question 1",
      "Question 2",
    ]);
    expect(within(present(articles[1])).getByRole("button", { name: "Helpful" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const group = screen.getByRole("group", { name: "Suggested questions" });
    expect(group).toHaveTextContent("New follow-up?");
    expect(group).not.toHaveTextContent("Old follow-up?");
    expect(screen.getByText("Choosing a different project starts a new chat.")).toBeInTheDocument();
    const panel = screen.getByRole("complementary", { name: "Chats" });
    expect(within(panel).getByRole("link", { name: /Expense claims/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("T-378 404 session_not_found shows Chat not available and drops the session from the list", async () => {
    signedIn();
    let listed = [sessionSummary()];
    server.use(
      http.get(apiUrl("/v1/sessions"), () =>
        HttpResponse.json({ items: listed, nextCursor: null }),
      ),
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () => {
        listed = [];
        return problemResponse(404, "session_not_found", {
          title: "Chat not found",
          detail: "This chat doesn't exist or you can't open it.",
        });
      }),
    );
    const { user, router } = renderApp({ route: `/chat/${IDS.session}` });
    expect(
      await screen.findByRole("heading", { level: 2, name: "Chat not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText("This chat doesn't exist or you can't open it.")).toBeInTheDocument();
    const panel = screen.getByRole("complementary", { name: "Chats" });
    await waitFor(() => {
      expect(within(panel).queryByRole("link", { name: /Expense claims/ })).toBeNull();
    });
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeDisabled();
    const unavailable = present(
      screen.getByRole("heading", { level: 2, name: "Chat not found" }).parentElement,
    );
    await user.click(within(unavailable).getByRole("button", { name: "New chat" }));
    expect(router.state.location.pathname).toBe("/chat");
  });

  test("another getSession failure shows an error bar whose Retry reads again", async () => {
    signedIn();
    let calls = 0;
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () => {
        calls += 1;
        return calls === 1
          ? problemResponse(503, "dependency_unavailable", { title: "Service busy" })
          : HttpResponse.json(sessionDetail());
      }),
    );
    const { user } = renderApp({ route: `/chat/${IDS.session}` });
    expect(await screen.findByText("Service busy")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("article", { name: "Question 1" })).toBeInTheDocument();
  });

  test("choosing another project in a session starts a new chat in it", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/projects"), () =>
        HttpResponse.json({
          items: [...PROJECTS, { ...PROJECTS[0], projectId: IDS.projectB, name: "Beta" }],
          nextCursor: null,
        }),
      ),
    );
    session(sessionDetail());
    const { user, router } = renderApp({ route: `/chat/${IDS.session}` });
    await screen.findByRole("article", { name: "Question 1" });
    await user.click(screen.getByRole("combobox", { name: "Project" }));
    await user.click(await screen.findByRole("option", { name: "Beta" }));
    expect(router.state.location.pathname).toBe("/chat");
    expect(router.state.location.search).toBe(`?project=${IDS.projectB}`);
  });

  test("a citation opens the viewer, whose close returns focus to the button", async () => {
    signedIn();
    session(sessionDetail());
    server.use(
      http.get(
        apiUrl(
          `/v1/sessions/${IDS.session}/turns/00000001-0000-4000-8000-000000000000/citations/1`,
        ),
        () =>
          HttpResponse.json({
            number: 1,
            title: "Expenses guide",
            pageNumber: 3,
            headingPath: [],
            contentType: "application/pdf",
            openAs: "pdf_page",
            fileName: "Expenses guide-page-3.pdf",
            documentPdfAvailable: false,
            documentFileName: "Expenses guide.pdf",
          }),
      ),
    );
    const { user } = renderApp({ route: `/chat/${IDS.session}` });
    const source = await screen.findByRole("button", { name: "Expenses guide, page 3" });
    await user.click(source);
    const viewer = await screen.findByRole("complementary", { name: "Source 1" });
    expect(await within(viewer).findByText("Expenses guide-page-3.pdf")).toBeInTheDocument();
    await user.click(within(viewer).getByRole("button", { name: "Close source" }));
    expect(source).toHaveFocus();
  });
});

describe("the chat screen's states", () => {
  test("no projects shows the No projects state", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/projects"), () => HttpResponse.json({ items: [], nextCursor: null })),
    );
    renderApp({ route: "/chat" });
    expect(
      await screen.findByRole("heading", { name: "You aren't in any projects yet" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeDisabled();
  });

  test("an admin with no projects gets Go to Projects", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/me"), () => HttpResponse.json(ADMIN_USER)),
      http.get(apiUrl("/v1/projects"), () => HttpResponse.json({ items: [], nextCursor: null })),
    );
    renderApp({ route: "/chat" });
    expect(await screen.findByRole("button", { name: "Go to Projects" })).toBeInTheDocument();
  });

  test("a failed project list shows an error bar with Retry and disables the composer", async () => {
    signedIn();
    let calls = 0;
    server.use(
      http.get(apiUrl("/v1/projects"), () => {
        calls += 1;
        return calls === 1
          ? new HttpResponse(null, { status: 502 })
          : HttpResponse.json({ items: PROJECTS, nextCursor: null });
      }),
    );
    const { user } = renderApp({ route: "/chat" });
    expect(await screen.findByText("Your projects didn't load.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { level: 2, name: "What would you like to know?" }),
    ).toBeInTheDocument();
  });

  test("a project that isn't ready disables the composer and says why", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/projects"), () =>
        HttpResponse.json({
          items: [{ ...PROJECTS[0], hasDefaultIndex: false }],
          nextCursor: null,
        }),
      ),
    );
    renderApp({ route: "/chat" });
    expect(await screen.findByText(/This project has no documents yet/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Your question" })).toBeDisabled();
  });

  test("with two projects and none chosen, the screen asks to choose one; ?project= chooses", async () => {
    signedIn();
    server.use(
      http.get(apiUrl("/v1/projects"), () =>
        HttpResponse.json({
          items: [...PROJECTS, { ...PROJECTS[0], projectId: IDS.projectB, name: "Beta" }],
          nextCursor: null,
        }),
      ),
    );
    const { user, router } = renderApp({ route: "/chat" });
    expect(await screen.findAllByText("Choose a project to start asking questions.")).toHaveLength(
      2,
    );
    await user.click(screen.getByRole("combobox", { name: "Project" }));
    await user.click(await screen.findByRole("option", { name: "Beta" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe(`?project=${IDS.projectB}`);
    });
  });

  test("below 768 px the session panel opens from the Chats button", async () => {
    signedIn();
    setViewportWidth(600);
    sessions([sessionSummary()]);
    const { user } = renderApp({ route: "/chat" });
    await user.click(await screen.findByRole("button", { name: "Chats" }));
    const drawer = await screen.findByRole("dialog", { name: "Chats" });
    await user.click(await within(drawer).findByRole("link", { name: /Expense claims/ }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Chats" })).toBeNull();
    });
  });

  test("a 409 turn_in_progress blocks sending until its time", async () => {
    signedIn();
    server.use(
      http.post(apiUrl("/v1/chat/turns"), () =>
        problemResponse(409, "turn_in_progress", {
          title: "Answer in progress",
          retryAfterSeconds: 30,
        }),
      ),
    );
    const { user } = renderApp({ route: "/chat" });
    await user.type(await composer(), "Question{Enter}");
    expect(await screen.findByText("Answer in progress")).toBeInTheDocument();
    expect(screen.getByText(/You can send your next question at/)).toBeInTheDocument();
    await user.type(await composer(), "Again");
    expect(screen.getByRole("button", { name: "Send question" })).toBeDisabled();
  });
});

describe("the chat screen's helpers", () => {
  test("the reason text of a 422", () => {
    const error = (reason: string) => ({
      ...clientError("problem", { status: 422 }),
      errors: [{ path: "body.question", reason }],
    });
    expect(questionReasonText(error("empty"))).toBe(
      "Type a question with at least one letter, number or symbol.",
    );
    expect(questionReasonText(error("too_long"))).toBe(
      "Questions can be up to 4,000 characters. Shorten yours, then send it.",
    );
    expect(questionReasonText(error("unknown_field"))).toBe(
      "This value isn't valid. Check it and correct it.",
    );
  });

  test("a 429 blocks every conversation, a 409 only its own", () => {
    const failure = (status: number, blockedUntil: number) => ({
      kind: "request" as const,
      code: null,
      message: null,
      referenceId: null,
      error: clientError("problem", { status }),
      retryable: false,
      retryAt: null,
      blockedUntil,
    });
    const limited = liveTurn({ failure: failure(429, 5_000), sessionId: "other" });
    const busy = liveTurn({ failure: failure(409, 9_000), sessionId: "other" });
    const own = liveTurn({ failure: failure(409, 7_000) });
    expect(blockedUntilOf([limited, busy, own], [own])).toBe(7_000);
    expect(blockedUntilOf([limited, busy], [])).toBe(5_000);
    expect(blockedUntilOf([liveTurn()], [])).toBeNull();
  });
});
