import { fireEvent, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { indexDetail, indexOptions, methodOption, uuid } from "../../test/adminData";
import { findToast, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { NewIndexScreen } from "./NewIndexScreen";

const ROUTES = [
  { path: "/admin/indexes/new", element: <NewIndexScreen /> },
  { path: "/admin/indexes/:indexId", element: <p>index page</p> },
];

function api(
  options: () => Response,
  create: (body: Record<string, unknown>) => Response = () =>
    HttpResponse.json(
      { index: indexDetail({ indexId: uuid(600) }), runId: uuid(601) },
      { status: 202 },
    ),
) {
  const bodies: Record<string, unknown>[] = [];
  server.use(
    http.get(apiUrl("/v1/admin/index-options"), () => options()),
    http.post(apiUrl("/v1/admin/indexes"), async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      bodies.push(body);
      return create(body);
    }),
  );
  return bodies;
}

describe("New index", () => {
  test("every field starts from defaults; Create index sends them and opens the index with a toast", async () => {
    const bodies = api(() => HttpResponse.json(indexOptions()));
    const { user, router } = renderAdmin(ROUTES, "/admin/indexes/new");
    expect(screen.getByRole("heading", { level: 1, name: "New index" })).toBeInTheDocument();
    expect(document.title).toBe("New index · DocChat");
    const method = await screen.findByRole("combobox", { name: /Ingestion method/ });
    expect(method).toHaveTextContent("By headings and paragraphs");
    expect(screen.getByText(/Splits by headings, then paragraphs/)).toBeInTheDocument();
    expect(
      screen.getByText(/How documents are split into searchable passages/),
    ).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: /Passage size \(tokens\)/ })).toHaveValue("512");
    expect(screen.getByRole("combobox", { name: /Embedding model/ })).toHaveTextContent(
      "text-embedding-3-large",
    );
    expect(screen.getByRole("combobox", { name: /Vector dimensions/ })).toHaveTextContent("1024");
    expect(screen.getByRole("combobox", { name: /Language/ })).toHaveTextContent("Any language");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Policy library");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "Policies");
    await user.click(screen.getByRole("combobox", { name: /Language/ }));
    await user.click(await screen.findByRole("option", { name: "French" }));
    await user.click(screen.getByRole("button", { name: "Create index" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/admin/indexes/${uuid(600)}`);
    });
    expect(bodies[0]).toEqual({
      displayName: "Policy library",
      description: "Policies",
      methodKey: "recursive",
      methodVersion: 1,
      methodParameters: { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 },
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "fr.microsoft",
    });
    expect(
      await findToast("Index created. Upload documents to start ingestion."),
    ).toBeInTheDocument();
  });

  test("choosing another method takes its settings; another model its dimensions", async () => {
    const bodies = api(() => HttpResponse.json(indexOptions()));
    const { user } = renderAdmin(ROUTES, "/admin/indexes/new");
    await user.click(await screen.findByRole("combobox", { name: /Ingestion method/ }));
    await user.click(await screen.findByRole("option", { name: "Page by page" }));
    expect(screen.getByRole("switch", { name: /Summarise tables/ })).toBeChecked();
    await user.click(screen.getByRole("combobox", { name: /Embedding model/ }));
    await user.click(await screen.findByRole("option", { name: "text-embedding-3-small" }));
    expect(screen.getByRole("combobox", { name: /Vector dimensions/ })).toHaveTextContent("256");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Scans");
    await user.click(screen.getByRole("button", { name: "Create index" }));
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    });
    expect(bodies[0]).toMatchObject({
      methodKey: "pagewise",
      methodParameters: { summarizeTables: true, ocrLanguages: ["eng"] },
      embeddingModel: "text-embedding-3-small",
      embeddingDimensions: 256,
    });
    expect(bodies[0]).not.toHaveProperty("description");
  });

  test("T-401 a method schema with an unsupported keyword shows paramsUnsupported and disables Create index", async () => {
    api(() =>
      HttpResponse.json(
        indexOptions({
          methods: [
            methodOption({
              parameterSchema: {
                type: "object",
                additionalProperties: false,
                required: [],
                properties: { size: { type: "integer", exclusiveMinimum: 1 } },
              },
            }),
          ],
        }),
      ),
    );
    renderAdmin(ROUTES, "/admin/indexes/new");
    expect(
      await screen.findByText(
        "This ingestion method has settings this page can't show. Choose another method, or ask a developer to update the method's settings.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create index" })).toBeDisabled();
  });

  test("T-401 a 422 at body.methodParameters.targetTokens marks that field", async () => {
    api(
      () => HttpResponse.json(indexOptions()),
      () =>
        problemResponse(422, "invalid_method_parameters", {
          title: "Invalid method settings",
          errors: [{ path: "body.methodParameters.targetTokens", reason: "out_of_range" }],
        }),
    );
    const { user } = renderAdmin(ROUTES, "/admin/indexes/new");
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "Policy");
    await user.click(screen.getByRole("button", { name: "Create index" }));
    const size = screen.getByRole("spinbutton", { name: /Passage size \(tokens\)/ });
    await waitFor(() => {
      expect(size).toHaveAttribute("aria-invalid", "true");
    });
    expect(size).toHaveFocus();
    expect(
      screen.getByText("This value isn't valid. Check it and correct it."),
    ).toBeInTheDocument();
  });

  test("a setting out of range is caught before sending; 409 display_name_taken goes under Name", async () => {
    const bodies = api(
      () => HttpResponse.json(indexOptions()),
      () =>
        problemResponse(409, "display_name_taken", {
          detail: "Another index already uses this name. Choose a different name.",
        }),
    );
    const { user } = renderAdmin(ROUTES, "/admin/indexes/new");
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "Taken");
    const overlap = screen.getByRole("spinbutton", { name: /Overlap between passages/ });
    fireEvent.change(overlap, { target: { value: "27" } });
    fireEvent.blur(overlap);
    await user.click(screen.getByRole("button", { name: "Create index" }));
    expect(
      await screen.findByText("This value isn't valid. Check it and correct it."),
    ).toBeInTheDocument();
    expect(bodies).toHaveLength(0);
    fireEvent.change(overlap, { target: { value: "30" } });
    fireEvent.blur(overlap);
    await user.click(screen.getByRole("button", { name: "Create index" }));
    expect(
      await screen.findByText("Another index already uses this name. Choose a different name."),
    ).toBeInTheDocument();
  });

  test("other refusals show the error bar; a failed options load shows index.optionsError with Retry", async () => {
    let fail = true;
    api(
      () =>
        fail
          ? problemResponse(503, "dependency_unavailable", { title: "Down" })
          : HttpResponse.json(indexOptions()),
      () => problemResponse(409, "search_index_capacity", { title: "No room for another index" }),
    );
    const { user } = renderAdmin(ROUTES, "/admin/indexes/new");
    expect(await screen.findByText("The index options didn't load.")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "Policy");
    await user.click(screen.getByRole("button", { name: "Create index" }));
    expect(await screen.findByText("No room for another index")).toBeInTheDocument();
  });
});
