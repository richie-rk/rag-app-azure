import { describe, expect, test, vi } from "vitest";

import { IDS } from "../../test/handlers";
import type { LiveTurn } from "../stream/turnStore";
import { createTurnSpeech, speakableText, spokenLength } from "./speakable";

function turn(text: string, allowedLinks: readonly string[] | null = null): LiveTurn {
  return {
    clientTurnId: IDS.clientTurn,
    projectId: IDS.projectA,
    sessionId: IDS.session,
    turnId: IDS.turn,
    question: "Q",
    phase: "streaming",
    broadened: false,
    text,
    citations: null,
    allowedLinks,
    followups: null,
    truncated: false,
    failure: null,
    traceId: null,
    metaCorrelationId: null,
    controller: new AbortController(),
    startedAt: 0,
  };
}

describe("the speakable text", () => {
  test("drops tokens and Markdown, collapses white space and redacts URLs", () => {
    expect(
      speakableText("**Claims** are due ⟦1⟧ in  30 days.\n\n- see `https://docs.example/a`", null),
    ).toBe("Claims are due in 30 days. see [link removed]");
  });

  test("keeps an allowed URL once allowedLinks are known", () => {
    expect(speakableText("Read https://docs.example/a now.", ["https://docs.example/a"])).toBe(
      "Read https://docs.example/a now.",
    );
  });

  test("a boundary is . ! ? or : before white space, or a line break", () => {
    expect(spokenLength("One. Two")).toBe(4);
    expect(spokenLength("Is it? Yes! Then: more")).toBe(17);
    expect(spokenLength("a.b and 3.5 no boundary")).toBe(0);
    expect(spokenLength("line\nnext")).toBe(5);
    expect(spokenLength("Ends here.")).toBe(0);
  });
});

describe("T-410 the turn speech", () => {
  test("T-410 speaks sentence by sentence and flushes the rest", () => {
    const append = vi.fn();
    const speech = createTurnSpeech(append);
    speech.feed(turn("The first"));
    expect(append).not.toHaveBeenCalled();
    speech.feed(turn("The first sentence. The sec"));
    expect(append).toHaveBeenLastCalledWith("The first sentence.");
    speech.feed(turn("The first sentence. The second ⟦1⟧ one! Then"));
    expect(append).toHaveBeenLastCalledWith("The second one!");
    speech.flush(turn("The first sentence. The second ⟦1⟧ one! Then the end"));
    expect(append).toHaveBeenLastCalledWith("Then the end");
    expect(append).toHaveBeenCalledTimes(3);
    // A second flush, as after an error that follows citations, says nothing more.
    speech.flush(turn("The first sentence. The second ⟦1⟧ one! Then the end"));
    expect(append).toHaveBeenCalledTimes(3);
  });

  test("T-410 redacts every URL while the answer streams, and uses allowedLinks on the flush", () => {
    const append = vi.fn();
    const speech = createTurnSpeech(append);
    speech.feed(turn("See https://docs.example/a. And"));
    expect(append).toHaveBeenLastCalledWith("See [link removed].");
    speech.flush(
      turn("See https://docs.example/a. And https://docs.example/a", ["https://docs.example/a"]),
    );
    expect(append).toHaveBeenLastCalledWith("And https://docs.example/a");
  });

  test("T-410 a flush without an earlier feed speaks the whole text, a line break included", () => {
    const append = vi.fn();
    createTurnSpeech(append).flush(turn("First line  \nsecond ![image](x.png)"));
    expect(append).toHaveBeenCalledWith("First line second");
  });

  test("T-410 a chunk with nothing to say appends nothing", () => {
    const append = vi.fn();
    const speech = createTurnSpeech(append);
    speech.feed(turn("⟦1⟧\n"));
    speech.flush(turn("⟦1⟧\n"));
    expect(append).not.toHaveBeenCalled();
  });
});
