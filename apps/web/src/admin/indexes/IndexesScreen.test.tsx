import { screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { indexSummary, page, uuid } from "../../test/adminData";
import { renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { IndexesScreen } from "./IndexesScreen";

const ROUTES = [{ path: "/admin/indexes", element: <IndexesScreen /> }];

describe("Indexes", () => {
  test("lists indexes with name, method, embedding, documents and status, and offers New index", async () => {
    server.use(
      http.get(apiUrl("/v1/admin/indexes"), ({ request }) => {
        expect(new URL(request.url).searchParams.get("limit")).toBe("50");
        return HttpResponse.json(
          page([
            indexSummary(1),
            indexSummary(2, {
              status: "reingest_required",
              reingestRequired: true,
              embedding: { model: "text-embedding-3-small", dimensions: 256 },
              liveDocumentCount: 0,
            }),
          ]),
        );
      }),
    );
    renderAdmin(ROUTES, "/admin/indexes");
    expect(screen.getByRole("heading", { level: 1, name: "Indexes" })).toBeInTheDocument();
    expect(document.title).toBe("Indexes · DocChat");
    const table = await screen.findByRole("table", { name: "Indexes" });
    expect(
      within(table)
        .getAllByRole("row")
        .map((row) => row.textContent),
    ).toEqual([
      "NameMethodEmbedding modelDocumentsStatus",
      "Index 1By headings and paragraphstext-embedding-3-large, 1024 dimensions12Ready",
      "Index 2By headings and paragraphstext-embedding-3-small, 256 dimensions0Re-ingest needed",
    ]);
    expect(within(table).getByRole("link", { name: "Index 2" })).toHaveAttribute(
      "href",
      `/admin/indexes/${uuid(502)}`,
    );
    expect(screen.getByRole("link", { name: "New index" })).toHaveAttribute(
      "href",
      "/admin/indexes/new",
    );
  });

  test("Empty, then Error with Retry, then Show more", async () => {
    let call = 0;
    server.use(
      http.get(apiUrl("/v1/admin/indexes"), ({ request }) => {
        call += 1;
        if (call === 1) return HttpResponse.json(page([]));
        if (call === 2)
          return problemResponse(500, "internal_error", { title: "Unexpected error" });
        return new URL(request.url).searchParams.has("cursor")
          ? HttpResponse.json(page([indexSummary(2)]))
          : HttpResponse.json(page([indexSummary(1)], "c2"));
      }),
    );
    const first = renderAdmin(ROUTES, "/admin/indexes");
    expect(
      await screen.findByText("No indexes yet. Create one, then upload documents to it."),
    ).toBeInTheDocument();
    first.result.unmount();
    const { user } = renderAdmin(ROUTES, "/admin/indexes");
    expect(await screen.findByText("Indexes didn't load.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByRole("link", { name: "Index 2" })).toBeInTheDocument();
  });
});
