// Renders admin screens the way the app does: inside the admin layout, whose toaster stays mounted
// while the screens navigate, with `http.ts` configured against the MSW server.

import { screen } from "@testing-library/react";
import type { RouteObject } from "react-router";

import { AdminLayout } from "../admin/shared/AdminLayout";
import { configureTestHttp, renderRoutes, type Harness, type HarnessOptions } from "./render";

/** Element `index` of a query's matches, failing the test when it is missing. */
export function nth(elements: readonly HTMLElement[], index: number): HTMLElement {
  const element = elements[index];
  if (element === undefined) throw new Error(`no element at ${String(index)}`);
  return element;
}

/** The toast whose title is `text` (its text also reaches the status region, so a plain text
 * query can find it twice). */
export function findToast(text: string): Promise<HTMLElement> {
  return screen.findByRole("listitem", { name: text });
}

/** Renders `routes` under the admin layout at `route`; `onAdminRequired` and the other signals
 * are spies. */
export function renderAdmin(
  routes: RouteObject[],
  route: string,
  options: Omit<HarnessOptions, "route"> = {},
): Harness {
  configureTestHttp();
  return renderRoutes([{ element: <AdminLayout />, children: routes }], { ...options, route });
}
