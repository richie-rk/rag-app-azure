import { describe, expect, test } from "vitest";

import { sanitizeAnswerHtml } from "./sanitize";

const allowedLinks = ["https://docs.example/a"];

function sanitize(html: string, links: readonly string[] | null = allowedLinks): HTMLElement {
  const clean = sanitizeAnswerHtml(html, { allowedLinks: links });
  return new DOMParser().parseFromString(`<body>${clean}</body>`, "text/html").body;
}

const LINK =
  '<a href="https://docs.example/a">doc</a><span data-link-host=""> (docs.example)</span>';

describe("T-383 the sanitiser profile", () => {
  test("T-383 an a with onclick and a URL off the allowlist is reduced to its text", () => {
    const body = sanitize(
      '<p><a href="https://evil.example/x" onclick="steal()" target="_blank">x</a><span data-link-host=""> (evil.example)</span></p>',
    );
    const link = body.querySelector("a");
    expect(link?.attributes).toHaveLength(0);
    expect(body.textContent).toBe("x (evil.example)");
  });

  test("T-383 every style attribute goes, and every button gets type=button", () => {
    const body = sanitize(
      '<p style="color:red">a<button style="x" type="submit" data-citation="1" aria-label="Source 1: A">1</button><button>2</button></p>',
    );
    expect(body.querySelector("[style]")).toBeNull();
    expect(
      [...body.querySelectorAll("button")].map((button) => button.getAttribute("type")),
    ).toEqual(["button", "button"]);
    expect(body.querySelector("button")?.getAttribute("aria-label")).toBe("Source 1: A");
    expect(body.querySelector("button")?.getAttribute("data-citation")).toBe("1");
  });

  test("T-383 forbidden elements and attributes go and their text stays", () => {
    const body = sanitize(
      '<img src="https://evil.example/p"><script>bad()</script><iframe src="https://evil.example"></iframe><svg><a href="https://docs.example/a">s</a></svg><p id="x" class="y" name="z">keep</p><form><input></form>',
    );
    expect(
      body.querySelector("img, script, iframe, svg, form, input, [id], [class], [name], [src]"),
    ).toBeNull();
    expect(body.textContent).toContain("keep");
  });

  test("T-383 an attribute stays only on its own element, with its own values", () => {
    const body = sanitize(
      '<div role="region" tabindex="0" aria-label="Table 1"><table><tr><th align="left">a</th><td align="justify" role="x">b</td></tr></table></div>' +
        '<div role="note" tabindex="1">n</div><ol start="3"><li>c</li></ol><ol start="x1"><li>d</li></ol>' +
        '<span data-link-host="x">e</span><p aria-label="no" data-citation="1">f</p><button data-citation="0">g</button>',
    );
    const region = body.querySelector("div");
    expect(region?.getAttribute("role")).toBe("region");
    expect(region?.getAttribute("tabindex")).toBe("0");
    expect(region?.getAttribute("aria-label")).toBe("Table 1");
    expect(body.querySelector("th")?.getAttribute("align")).toBe("left");
    expect(body.querySelector("td")?.attributes).toHaveLength(0);
    expect(body.querySelectorAll("div")[1]?.attributes).toHaveLength(0);
    expect(body.querySelectorAll("ol")[0]?.getAttribute("start")).toBe("3");
    expect(body.querySelectorAll("ol")[1]?.attributes).toHaveLength(0);
    expect(body.querySelector("span")?.attributes).toHaveLength(0);
    expect(body.querySelector("p")?.attributes).toHaveLength(0);
    expect(body.querySelector("button")?.getAttribute("data-citation")).toBeNull();
  });

  test("T-383 an attribute value that could break out of its context goes", () => {
    const body = sanitize('<button aria-label="--&gt;x" data-citation="1">1</button>');
    expect(body.querySelector("button")?.getAttribute("aria-label")).toBeNull();
  });

  test("T-383 an allowed link with its host label stays a link, with rel and target set", () => {
    const body = sanitize(`<p>${LINK}</p>`);
    const link = body.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://docs.example/a");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link?.getAttribute("target")).toBe("_blank");
  });

  test("T-383 a link without allowedLinks, without an href or with a javascript: URL is inert", () => {
    expect(sanitize(`<p>${LINK}</p>`, null).querySelector("a[href]")).toBeNull();
    expect(sanitize('<p><a target="_blank">x</a></p>').querySelector("a[target]")).toBeNull();
    expect(
      sanitize(
        '<p><a href="javascript:alert(1)">x</a><span data-link-host=""> (x)</span></p>',
      ).querySelector("a[href]"),
    ).toBeNull();
  });

  test("T-417 the sanitiser alone makes inert an 11th link and a link without its host label", () => {
    const eleven = Array.from({ length: 11 }, () => LINK).join(" ");
    expect(sanitize(`<p>${eleven}</p>`).querySelectorAll("a[href]")).toHaveLength(10);
    expect(
      sanitize('<p><a href="https://docs.example/a">doc</a></p>').querySelector("a[href]"),
    ).toBeNull();
    expect(
      sanitize(
        '<p><a href="https://docs.example/a">doc</a><span data-link-host=""> (other.example)</span></p>',
      ).querySelector("a[href]"),
    ).toBeNull();
    expect(
      sanitize(
        '<p><a href="https://docs.example/a">doc</a><b> (docs.example)</b></p>',
      ).querySelector("a[href]"),
    ).toBeNull();
  });

  test("T-383 each call counts its own links", () => {
    const ten = Array.from({ length: 10 }, () => LINK).join(" ");
    sanitize(`<p>${ten}</p>`);
    expect(sanitize(`<p>${LINK}</p>`).querySelectorAll("a[href]")).toHaveLength(1);
  });
});
