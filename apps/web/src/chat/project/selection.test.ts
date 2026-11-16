import { describe, expect, test } from "vitest";

import type { ProjectSummary } from "../../api/queryClient";
import { IDS } from "../../test/handlers";
import { chooseProject, sortedProjects } from "./selection";

const alpha: ProjectSummary = {
  projectId: IDS.projectA,
  name: "alpha",
  description: "",
  exampleQuestions: [],
  hasDefaultIndex: true,
};
const beta: ProjectSummary = { ...alpha, projectId: IDS.projectB, name: "Beta" };
const none = {
  sessionId: null,
  sessionProjectId: null,
  projectParam: null,
  newestSessionProjectId: null,
};

describe("the project selection", () => {
  test("on /chat: ?project= first, when listed", () => {
    expect(chooseProject({ ...none, projects: [alpha, beta], projectParam: IDS.projectB })).toBe(
      beta,
    );
  });

  test("then the newest loaded session's project, when listed", () => {
    expect(
      chooseProject({
        ...none,
        projects: [alpha, beta],
        projectParam: "77777777-7777-4777-8777-777777777777",
        newestSessionProjectId: IDS.projectA,
      }),
    ).toBe(alpha);
  });

  test("then the only project; otherwise none", () => {
    expect(chooseProject({ ...none, projects: [alpha] })).toBe(alpha);
    expect(chooseProject({ ...none, projects: [alpha, beta] })).toBeNull();
    expect(chooseProject({ ...none, projects: [] })).toBeNull();
  });

  test("on /chat/<id>: the session's project, or none when it isn't listed", () => {
    const session = { ...none, sessionId: IDS.session, projectParam: IDS.projectA };
    expect(
      chooseProject({ ...session, projects: [alpha, beta], sessionProjectId: IDS.projectB }),
    ).toBe(beta);
    expect(
      chooseProject({ ...session, projects: [alpha], sessionProjectId: IDS.projectB }),
    ).toBeNull();
    expect(chooseProject({ ...session, projects: [alpha] })).toBeNull();
  });

  test("projects sort by name at base sensitivity", () => {
    const gamma = { ...alpha, name: "Gamma" };
    expect(sortedProjects([gamma, beta, alpha]).map((project) => project.name)).toEqual([
      "alpha",
      "Beta",
      "Gamma",
    ]);
  });
});
