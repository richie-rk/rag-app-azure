// The routes (10-web-client.md sections 6.1, 6.2 and 6.5): React Router in data mode with
// browser-history paths. Memory and the admin screens are lazy modules loaded inside their guard;
// the chat screen, the shell, the access screens and the API client are in the initial bundle.
//
// Adding a screen: write its component under its feature directory, then add one route below;
// a lazy screen goes through `lazyScreen`, which turns a module that left with an earlier release
// into the "DocChat has been updated" screen.

import {
  Button,
  Skeleton,
  SkeletonItem,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { Suspense, lazy, useEffect, type ComponentType } from "react";
import {
  Navigate,
  createBrowserRouter,
  useParams,
  useRouteError,
  type RouteObject,
} from "react-router";

import { ChatScreen } from "../chat/ChatScreen";
import { en } from "../copy/en";
import { devLogOutcome } from "../platform/devLog";
import { ScreenHeading, useDocumentTitle } from "../ui/PageHeader";
import { AdminOnly } from "./AdminOnly";
import { AppShell } from "./AppShell";

/** A lazy module's file couldn't be loaded, typically because it left with an earlier release
 * (an excluded path answers `404`, never HTML; section 3.6). */
export class ModuleLoadError extends Error {
  constructor(cause: unknown) {
    super("app_updated", { cause });
    this.name = "ModuleLoadError";
  }
}

const useStyles = makeStyles({
  skeleton: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  error: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalM,
    maxWidth: "480px",
    padding: tokens.spacingHorizontalL,
  },
  heading: {
    fontSize: tokens.fontSizeBase600,
    lineHeight: tokens.lineHeightBase600,
    fontWeight: tokens.fontWeightSemibold,
  },
});

/** The loading state while a lazy screen's module arrives: a skeleton, never a blank area. */
function ScreenSkeleton() {
  const styles = useStyles();
  return (
    <Skeleton className={styles.skeleton}>
      <SkeletonItem size={32} />
      <SkeletonItem />
      <SkeletonItem />
    </Skeleton>
  );
}

/** A screen loaded on first use; `load` imports its module and picks the component. */
export function lazyScreen<M>(
  load: () => Promise<M>,
  pick: (module: M) => ComponentType,
): ComponentType {
  const Screen = lazy(() =>
    load().then(
      (module) => ({ default: pick(module) }),
      (cause: unknown) => {
        throw new ModuleLoadError(cause);
      },
    ),
  );
  return function LazyScreen() {
    return (
      <Suspense fallback={<ScreenSkeleton />}>
        <Screen />
      </Suspense>
    );
  };
}

const AdminLayout = lazyScreen(
  () => import("../admin/shared/AdminLayout"),
  (module) => module.AdminLayout,
);
const MemoryScreen = lazyScreen(
  () => import("../memory/MemoryScreen"),
  (module) => module.MemoryScreen,
);
const ProjectsScreen = lazyScreen(
  () => import("../admin/projects/ProjectsScreen"),
  (module) => module.ProjectsScreen,
);
const NewProjectScreen = lazyScreen(
  () => import("../admin/projects/NewProjectScreen"),
  (module) => module.NewProjectScreen,
);
const ProjectScreen = lazyScreen(
  () => import("../admin/projects/ProjectScreen"),
  (module) => module.ProjectScreen,
);
const IndexesScreen = lazyScreen(
  () => import("../admin/indexes/IndexesScreen"),
  (module) => module.IndexesScreen,
);
const NewIndexScreen = lazyScreen(
  () => import("../admin/indexes/NewIndexScreen"),
  (module) => module.NewIndexScreen,
);
const IndexScreen = lazyScreen(
  () => import("../admin/indexes/IndexScreen"),
  (module) => module.IndexScreen,
);
const DocumentsScreen = lazyScreen(
  () => import("../admin/documents/DocumentsScreen"),
  (module) => module.DocumentsScreen,
);
const RunsScreen = lazyScreen(
  () => import("../admin/runs/RunsScreen"),
  (module) => module.RunsScreen,
);
const RunScreen = lazyScreen(
  () => import("../admin/runs/RunScreen"),
  (module) => module.RunScreen,
);

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isCanonicalUuid(value: string | undefined): value is string {
  return value !== undefined && CANONICAL_UUID.test(value);
}

/** `/chat` and `/chat/:sessionId`, one route, so a new chat that takes its session's URL after
 * `meta` keeps its screen mounted; a `sessionId` that isn't a canonical UUID is replaced by
 * `/chat`. */
function ChatRoute() {
  const { sessionId } = useParams();
  return sessionId === undefined || isCanonicalUuid(sessionId) ? (
    <ChatScreen />
  ) : (
    <Navigate to="/chat" replace />
  );
}

/** The router's error element (section 6.5): a lazy module that didn't load, or any other
 * rendering error. Neither shows a reference ID, because no request failed. */
export function RouteError() {
  const styles = useStyles();
  const error = useRouteError();
  const updated = error instanceof ModuleLoadError;
  const title = updated ? en["app.updated.title"] : en["app.crashed.title"];
  useDocumentTitle(title);
  useEffect(() => {
    devLogOutcome(updated ? "app_updated" : "render_failed", null);
  }, [updated]);
  return (
    <div className={styles.error}>
      <ScreenHeading className={styles.heading}>{title}</ScreenHeading>
      <Text>{updated ? en["app.updated.body"] : en["app.crashed.body"]}</Text>
      <div>
        <Button
          appearance="primary"
          onClick={() => {
            window.location.reload();
          }}
        >
          {en["app.reload"]}
        </Button>
      </div>
    </div>
  );
}

const toChat = <Navigate to="/chat" replace />;

export const appRoutes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      {
        // A rendering error inside a screen keeps the shell around it.
        errorElement: <RouteError />,
        children: [
          { index: true, element: toChat },
          { path: "chat/:sessionId?", element: <ChatRoute /> },
          { path: "memory", element: <MemoryScreen /> },
          {
            path: "admin",
            children: [
              { index: true, element: toChat },
              {
                element: <AdminOnly />,
                children: [
                  {
                    // The admin toaster stays mounted across admin screens (section 7.1).
                    element: <AdminLayout />,
                    children: [
                      { path: "projects", element: <ProjectsScreen /> },
                      { path: "projects/new", element: <NewProjectScreen /> },
                      { path: "projects/:projectId", element: <ProjectScreen /> },
                      { path: "indexes", element: <IndexesScreen /> },
                      { path: "indexes/new", element: <NewIndexScreen /> },
                      { path: "indexes/:indexId", element: <IndexScreen /> },
                      { path: "indexes/:indexId/documents", element: <DocumentsScreen /> },
                      { path: "runs", element: <RunsScreen /> },
                      { path: "runs/:runId", element: <RunScreen /> },
                    ],
                  },
                ],
              },
            ],
          },
          { path: "*", element: toChat },
        ],
      },
    ],
  },
];

export type AppRouter = ReturnType<typeof createBrowserRouter>;

export function createAppRouter(): AppRouter {
  return createBrowserRouter(appRoutes);
}
