import { describe, expect, test } from "vitest";

import type { Element as HastElement, Root as HastRoot } from "hast";
import type { Root as MdastRoot } from "mdast";

import { renderAnswer } from "./pipeline";
import {
  hastText,
  hostLabel,
  rehypeDocchatPolicy,
  remarkDocchatPolicy,
  tokensAsText,
  type RenderContext,
} from "./policy";

const REMOVED = "[link removed]";

function render(markdown: string, context: RenderContext = {}): HTMLElement {
  const html = renderAnswer(markdown, context);
  return new DOMParser().parseFromString(`<body>${html}</body>`, "text/html").body;
}

describe("the Markdown rules (section 9.2)", () => {
  test("raw HTML, block or inline, becomes literal text", () => {
    const body = render("<div>block</div>\n\ninline <b>x</b>");
    expect(body.querySelector("div, b")).toBeNull();
    expect(body.textContent).toContain("<div>block</div>");
    expect(body.textContent).toContain("inline <b>x</b>");
  });

  test("images become their alt text, and nothing when the alt is empty", () => {
    const body = render(
      "a ![alt text](https://img.example/a.png) b ![](https://img.example/b.png) c\n\n![ref][r]\n\n[r]: https://img.example/c.png",
    );
    expect(body.querySelector("img")).toBeNull();
    expect(body.textContent).toContain("a alt text b  c");
    expect(body.textContent).toContain("ref");
  });

  test("footnotes become plain text, with no anchors or IDs", () => {
    const body = render(
      "Claim[^n].\n\n[^n]: The note.\n\n[^m]:\n    ```\n    code\n    ```\n\nAgain[^m].",
    );
    expect(body.querySelector("a, section, [id]")).toBeNull();
    expect(body.textContent).toContain("Claim[^n].");
    expect(body.textContent).toContain("[^n]: The note.");
    expect(body.textContent).toContain("[^m]: ");
  });

  test("task-list items lose their checkbox and start with a glyph", () => {
    const body = render("- [x] done\n- [ ] todo");
    expect(body.querySelector("input, [class]")).toBeNull();
    const items = [...body.querySelectorAll("li")].map((item) => item.textContent.trim());
    expect(items).toEqual(["☑ done", "☐ todo"]);
  });

  test("headings move down two levels, never past h6", () => {
    const body = render("# one\n\n## two\n\n#### four\n\n###### six");
    expect([...body.querySelectorAll("h1, h2, h3, h4, h5, h6")].map((h) => h.tagName)).toEqual([
      "H3",
      "H4",
      "H6",
      "H6",
    ]);
  });

  test("tables scroll in their own focusable regions, counted from 1", () => {
    const table = "| a | b |\n|:-|-:|\n| 1 | 2 |";
    const body = render(`${table}\n\ntext\n\n${table}`);
    const regions = [...body.querySelectorAll("div[role=region]")];
    expect(regions.map((region) => region.getAttribute("aria-label"))).toEqual([
      "Table 1",
      "Table 2",
    ]);
    expect(regions[0]?.getAttribute("tabindex")).toBe("0");
    expect(body.querySelector("th")?.getAttribute("align")).toBe("left");
  });

  test("classes and IDs are removed", () => {
    const body = render("```js\nconst a = 1;\n```");
    expect(body.querySelector("[class], [id]")).toBeNull();
    expect(body.querySelector("pre code")?.textContent).toBe("const a = 1;\n");
  });

  test("strikethrough needs two tildes, and ordered lists keep their start", () => {
    const body = render("~~gone~~ ~kept~\n\n3. three\n4. four");
    expect(body.querySelector("del")?.textContent).toBe("gone");
    expect(body.textContent).toContain("~kept~");
    expect(body.querySelector("ol")?.getAttribute("start")).toBe("3");
  });
});

describe("T-382 citation tokens", () => {
  const citations = [
    { number: 1, title: "Expenses guide" },
    { number: 2, title: "Finance handbook" },
  ];

  test("T-382 before citations arrive a token is plain superscript text", () => {
    const body = render("Claims ⟦1⟧ and ⟦7⟧.");
    expect([...body.querySelectorAll("sup")].map((sup) => sup.textContent)).toEqual(["[1]", "[7]"]);
    expect(body.querySelector("button")).toBeNull();
  });

  test("T-382 a listed number is a button, an unlisted one renders as nothing", () => {
    const body = render("Claims ⟦1⟧⟦7⟧ end.", { citations });
    const buttons = [...body.querySelectorAll("sup > button")];
    expect(buttons).toHaveLength(1);
    expect(buttons[0]?.getAttribute("data-citation")).toBe("1");
    expect(buttons[0]?.getAttribute("type")).toBe("button");
    expect(buttons[0]?.getAttribute("aria-label")).toBe("Source 1: Expenses guide");
    expect(buttons[0]?.textContent).toBe("1");
    expect(body.textContent).toBe("Claims 1 end.");
  });

  test("T-382 inside a link or code a token is [n], never a button", () => {
    const body = render(
      "[see ⟦1⟧ ⟦7⟧](https://docs.example/a) `code ⟦1⟧` and\n\n```\nblock ⟦7⟧\n```",
      {
        citations,
        allowedLinks: ["https://docs.example/a"],
      },
    );
    expect(body.querySelector("button")).toBeNull();
    expect(body.querySelector("a")?.textContent).toBe("see [1] [7]");
    expect(body.querySelector("p code")?.textContent).toBe("code [1]");
    expect(body.querySelector("pre code")?.textContent).toBe("block [7]\n");
  });

  test("T-382 a token outside the API's form stays as written", () => {
    expect(tokensAsText("⟦0⟧ ⟦123⟧ ⟦12⟧")).toBe("⟦0⟧ ⟦123⟧ [12]");
  });
});

describe("T-381 the link policy", () => {
  test("T-381 before citations no link exists, even for an allowlisted URL", () => {
    const body = render("[guide](https://docs.example/a)");
    expect(body.querySelector("a")).toBeNull();
    expect(body.textContent).toBe("guide");
  });

  test("T-381 a URL equal to an entry after normalisation links; one with a user name doesn't", () => {
    const allowedLinks = ["https://docs.example/a/b"];
    const body = render(
      "[one](HTTPS://Docs.Example:443/a/./b) [two](https://user@docs.example/a/b)",
      {
        allowedLinks,
      },
    );
    const links = [...body.querySelectorAll("a")];
    expect(links).toHaveLength(1);
    expect(links[0]?.getAttribute("href")).toBe("https://docs.example/a/b");
    expect(links[0]?.getAttribute("target")).toBe("_blank");
    expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(links[0]?.getAttribute("aria-label")).toBe("one (opens in a new tab)");
    expect(body.textContent).toBe("one (docs.example) two");
  });

  test("T-381 mailto, javascript, data and http links become their text", () => {
    const body = render(
      "[m](mailto:a@example.net) [j](javascript:alert(1)) [d](data:text/html,x) [h](http://docs.example/a)",
      { allowedLinks: ["https://docs.example/a"] },
    );
    expect(body.querySelector("a")).toBeNull();
    expect(body.textContent).toBe("m j d h");
  });
});

describe("T-417 the link host label", () => {
  const allowedLinks = [
    "https://Docs.Example/a",
    "https://xn--bcher-kva.example/b",
    "https://bücher.example/c",
    "https://docs.example:8443/d",
    ...Array.from({ length: 11 }, (_, index) => `https://docs.example/n${String(index)}`),
  ];

  test("T-417 an allowed link is followed directly by its lower-case host", () => {
    const body = render("[A](https://docs.example/a)", { allowedLinks });
    const link = body.querySelector("a");
    expect(link?.nextSibling).toBe(body.querySelector("span[data-link-host]"));
    expect(link?.nextElementSibling?.outerHTML).toBe(
      '<span data-link-host=""> (docs.example)</span>',
    );
  });

  test("T-417 an internationalised host shows as punycode, and a port other than 443 shows", () => {
    const body = render("[C](https://bücher.example/c) [D](https://docs.example:8443/d)", {
      allowedLinks,
    });
    const labels = [...body.querySelectorAll("span[data-link-host]")].map(
      (span) => span.textContent,
    );
    expect(labels).toEqual([" (xn--bcher-kva.example)", " (docs.example:8443)"]);
    expect(body.textContent).not.toContain("bücher");
  });

  test("T-417 of 12 allowed links the first 10 are links and the 11th and 12th are text with a host", () => {
    const markdown = Array.from(
      { length: 12 },
      (_, index) => `[L${String(index + 1)}](https://docs.example/n${String(index % 11)})`,
    ).join(" ");
    const body = render(markdown, { allowedLinks });
    expect(body.querySelectorAll("a")).toHaveLength(10);
    expect(body.querySelectorAll("span[data-link-host]")).toHaveLength(12);
    expect(body.textContent).toContain("L11 (docs.example) L12 (docs.example)");
  });

  test("T-417 link text loses Cc, Cf, Co, Cs and Cn code points", () => {
    const body = render("[a‮b⁦c​de͸f](https://docs.example/a)", {
      allowedLinks,
    });
    expect(body.querySelector("a")?.textContent).toBe("abcdef");
    expect(body.querySelector("a")?.getAttribute("aria-label")).toBe("abcdef (opens in a new tab)");
  });

  test("T-417 hostLabel builds the span for any https URL", () => {
    expect(hostLabel("https://EXAMPLE.net:8443/x").children).toEqual([
      { type: "text", value: " (example.net:8443)" },
    ]);
  });
});

describe("T-419 refused URLs in the rendered answer", () => {
  const allowedLinks = ["https://docs.example/a", "https://docs.example/b?x=1"];

  test.each([
    ["prose", "https://evil.example/c?d=secret"],
    ["a code block", "```\nhttps://evil.example/c?d=secret\n```"],
    ["an inline code span", "`https://evil.example/c?d=secret`"],
    ["image alt text", "![https://evil.example/p?d=1](x.png)"],
    ["raw <img> text", "<img src=https://evil.example/p?d=1>"],
    ["a refused link's text", "[https://evil.example/p?d=1](https://evil.example/p?d=1)"],
    ["a www. address", "www.evil.example/p"],
    ["an upper-case WWW. address", "WWW.evil.example/p"],
    ["an ftp URL", "ftp://evil.example/p"],
    ["a protocol-relative address", "<img src=//evil.example/p>"],
  ])("in %s shows as the marker with no link", (_, markdown) => {
    const body = render(markdown, { allowedLinks });
    expect(body.querySelector("a")).toBeNull();
    expect(body.textContent).toContain(REMOVED);
    expect(body.textContent).not.toContain("evil.example");
  });

  test.each([
    ["strong", "**https:**//evil.example/p"],
    ["emphasis", "https://*evil*.example/p"],
    ["an inline code span", "https:`//`evil.example/p"],
    ["a zero-width space", "https:​//evil.example/p"],
    ["a soft hyphen", "https://evil.­example/p"],
  ])("a URL split by %s is redacted as one", (_, markdown) => {
    const body = render(markdown, { allowedLinks });
    expect(body.textContent).toBe(REMOVED);
  });

  test("T-419 an allowed URL stays in prose and in code, and an allowed autolink is a link", () => {
    const body = render(
      "Read https://docs.example/a, `https://docs.example/a` and <https://docs.example/b?x=1>.",
      { allowedLinks },
    );
    expect(body.querySelector("code")?.textContent).toBe("https://docs.example/a");
    expect(body.querySelectorAll("a")).toHaveLength(2);
    expect(body.textContent).toContain("https://docs.example/a (docs.example),");
  });

  test("T-419 trailing punctuation stays after the marker", () => {
    const body = render("Go to https://evil.example/p. Or (https://evil.example/q).", {
      allowedLinks,
    });
    expect(body.textContent).toBe(`Go to ${REMOVED}. Or (${REMOVED}).`);
  });

  test("T-419 a citation between https: and //host leaves https: and redacts the rest", () => {
    const body = render("https:⟦1⟧//evil.example/p", {
      allowedLinks,
      citations: [{ number: 1, title: "Guide" }],
    });
    expect(body.textContent).toBe(`https:1${REMOVED}`);
  });

  test("T-419 before allowedLinks arrives every candidate is redacted", () => {
    expect(render("https://docs.example/a").textContent).toBe(REMOVED);
    expect(render("https://docs.example/a", { allowedLinks }).querySelector("a")).not.toBeNull();
  });

  test("T-419 an email address and a bare domain stay", () => {
    const body = render("name@example.net, mailto:name@example.net and example.net/abc", {
      allowedLinks,
    });
    expect(body.textContent).toContain("name@example.net");
    expect(body.textContent).toContain("example.net/abc");
  });

  test("T-419 a URL spanning a line break is two runs", () => {
    const body = render("https:  \n//evil.example/p", { allowedLinks });
    expect(body.textContent).toBe(`https:\n${REMOVED}`);
  });

  test("T-419 emphasis inside an allowed link keeps its text", () => {
    const body = render("[**bold** guide](https://docs.example/a)", { allowedLinks });
    expect(body.querySelector("a strong")?.textContent).toBe("bold");
    expect(body.querySelector("a")?.getAttribute("aria-label")).toBe(
      "bold guide (opens in a new tab)",
    );
  });
});

describe("the plugins on hand-built trees", () => {
  test("a footnote without a label uses its identifier", () => {
    const tree: MdastRoot = {
      type: "root",
      children: [
        {
          type: "paragraph",
          children: [{ type: "footnoteReference", identifier: "n" }],
        },
        {
          type: "footnoteDefinition",
          identifier: "n",
          children: [{ type: "paragraph", children: [{ type: "text", value: "Note" }] }],
        },
      ],
    };
    remarkDocchatPolicy({})(tree);
    expect(tree.children).toEqual([
      { type: "paragraph", children: [{ type: "text", value: "[^n]" }] },
      {
        type: "paragraph",
        children: [
          { type: "text", value: "[^n]: " },
          { type: "text", value: "Note" },
        ],
      },
    ]);
  });

  test("comments are no text, and an a without an href is refused", () => {
    const link: HastElement = {
      type: "element",
      tagName: "a",
      properties: { href: "https://docs.example/a" },
      children: [
        { type: "comment", value: "c" },
        { type: "text", value: "doc​" },
      ],
    };
    const tree: HastRoot = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "p",
          properties: {},
          children: [
            { type: "comment", value: "https://evil.example/c" },
            link,
            {
              type: "element",
              tagName: "a",
              properties: {},
              children: [{ type: "text", value: "x" }],
            },
          ],
        },
      ],
    };
    rehypeDocchatPolicy({ allowedLinks: ["https://docs.example/a"] })(tree);
    expect(hastText(tree)).toBe("doc (docs.example)x");
    expect(link.properties.href).toBe("https://docs.example/a");
  });
});
