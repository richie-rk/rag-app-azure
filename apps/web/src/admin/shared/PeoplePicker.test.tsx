import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { useState } from "react";
import { describe, expect, test } from "vitest";

import { page, userSummary, uuid } from "../../test/adminData";
import { apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp, renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { PeoplePicker, personName } from "./PeoplePicker";
import type { UserSummary } from "./queries";

function Picker({ projectId, maxPeople }: { projectId?: string; maxPeople?: number }) {
  const [selected, setSelected] = useState<UserSummary[]>([]);
  return (
    <>
      <PeoplePicker
        label="People to add"
        placeholder="Search by name or email"
        selected={selected}
        onChange={setSelected}
        {...(projectId === undefined ? {} : { projectId })}
        {...(maxPeople === undefined ? {} : { maxPeople })}
      />
      <output data-testid="chosen">{selected.map((person) => person.userId).join(",")}</output>
    </>
  );
}

function searchApi(respond: (url: URL) => Response) {
  const seen: URL[] = [];
  server.use(
    http.get(apiUrl("/v1/admin/users"), ({ request }) => {
      const url = new URL(request.url);
      seen.push(url);
      return respond(url);
    }),
  );
  return seen;
}

describe("the people picker", () => {
  test("searches from 2 characters, after the debounce, with limit=20 and the project", async () => {
    configureTestHttp();
    const seen = searchApi(() =>
      HttpResponse.json(
        page([userSummary(1, { isMember: false }), userSummary(2, { isMember: true })]),
      ),
    );
    const { user } = renderWithProviders(<Picker projectId={uuid(1)} />);
    const input = screen.getByRole("combobox", { name: "People to add" });
    await user.type(input, "p");
    expect(screen.getByText("Type at least 2 characters to search.")).toBeInTheDocument();
    await user.type(input, "e");
    const option = await screen.findByRole("option", { name: /Person 1/ });
    expect(seen).toHaveLength(1);
    expect(Object.fromEntries(seen[0]?.searchParams ?? [])).toEqual({
      q: "pe",
      limit: "20",
      projectId: uuid(1),
    });
    expect(screen.getByRole("option", { name: /Person 2/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.click(option);
    expect(screen.getByTestId("chosen")).toHaveTextContent(uuid(101));
    expect(input).toHaveValue("");
  });

  test("no one found, a failed search with Retry, and removing a chosen person", async () => {
    configureTestHttp();
    let fail = true;
    searchApi((url) => {
      if (url.searchParams.get("q") === "zz") return HttpResponse.json(page([]));
      if (fail)
        return problemResponse(503, "dependency_unavailable", { title: "Service unavailable" });
      return HttpResponse.json(page([userSummary(3)]));
    });
    const { user } = renderWithProviders(<Picker maxPeople={1} />);
    const input = screen.getByRole("combobox", { name: "People to add" });
    await user.type(input, "zz");
    expect(
      await screen.findByText(
        "No one found. People appear here after they first sign in to DocChat.",
      ),
    ).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "pe");
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(screen.queryByText("Service unavailable")).toBeNull();
    });
    await user.click(input);
    await user.click(await screen.findByRole("option", { name: /Person 3/ }));
    expect(screen.getByTestId("chosen")).toHaveTextContent(uuid(103));
    const chosen = screen.getByRole("listbox", { name: "People to add" });
    await user.click(within(chosen).getByRole("option", { name: /Person 3/ }));
    await waitFor(() => {
      expect(screen.getByTestId("chosen")).toHaveTextContent("");
    });
  });

  test("a person without a display name goes by e-mail", () => {
    expect(personName({ displayName: "", email: "a@example.test" })).toBe("a@example.test");
    expect(personName({ displayName: "Ada", email: "a@example.test" })).toBe("Ada");
  });
});
