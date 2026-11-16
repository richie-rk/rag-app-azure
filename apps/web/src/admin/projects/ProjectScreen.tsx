// Project (`/admin/projects/:projectId`, 10-web-client.md section 7.6): the breadcrumb, the name
// with its status badge and Archive project or Restore project, and the tabs Details, Members and
// Indexes, mirrored to `?tab=`. While the Members or Indexes tab is shown the project query doesn't
// refetch on its own: those tabs read the project first on every load (section 5.6 step 3).

import { Button, SkeletonItem, Tab, TabList, makeStyles, tokens } from "@fluentui/react-components";
import { Archive20Regular, ArrowUndo20Regular } from "@fluentui/react-icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useParams } from "react-router";

import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { useRetryWaits } from "../../ui/retryWait";
import { NotFoundBar } from "../shared/AdminStates";
import { useEditForm } from "../shared/conflict";
import { useUrlFilters, type UrlFilter } from "../shared/filters";
import { problemLines } from "../shared/formErrors";
import { adminKeys, adminProjectQuery } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { useAdminToast } from "../shared/toast";
import { DetailsTab } from "./DetailsTab";
import { IndexesTab } from "./IndexesTab";
import { MembersTab } from "./MembersTab";
import { EMPTY_PROJECT_FORM, projectFormValues } from "./projectForm";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  title: { display: "inline-flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
  skeleton: { width: "240px" },
});

const TABS = ["details", "members", "indexes"] as const;
type ProjectTab = (typeof TABS)[number];
const TAB_FILTER: readonly UrlFilter[] = [{ name: "tab", allowed: TABS, fallback: "details" }];

export function ProjectScreen() {
  const { projectId = "" } = useParams();
  // Another project starts from nothing: its own form, tabs and dialogs.
  return <ProjectView key={projectId} projectId={projectId} />;
}

function ProjectView({ projectId }: { readonly projectId: string }) {
  const styles = useStyles();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { values, setFilter } = useUrlFilters(TAB_FILTER);
  const tab = (values["tab"] ?? "details") as ProjectTab;
  const query = useQuery({
    ...adminProjectQuery(projectId),
    // The Members and Indexes tabs load the project themselves, before their rows.
    ...(tab === "details" ? {} : { staleTime: Infinity, refetchOnWindowFocus: false }),
  });
  const project = query.data;
  // Empty until the project arrives: a pristine form takes the loaded values.
  const form = useEditForm(EMPTY_PROJECT_FORM, "");
  const { reload } = form;
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ error: ApiError; lines: string[] } | null>(null);
  useDocumentTitle(...(project === undefined ? [] : [project.name]), en["projects.title"]);

  // Newer data from a tab's reload replaces the Details form only while nothing was typed.
  useEffect(() => {
    if (project !== undefined) reload(projectFormValues(project), project.rowVersion);
  }, [project, reload]);

  /** Archive project or Restore project: `adminUpdateProject` with the status alone. */
  const setStatus = async (status: "active" | "archived") => {
    if (project === undefined) return;
    const sent = project.rowVersion;
    setBusy(true);
    setFailure(null);
    try {
      const updated = await request("adminUpdateProject", {
        path: { projectId },
        body: { status },
        ifMatch: sent,
      });
      queryClient.setQueryData(adminKeys.project(projectId), updated);
      form.moveRowVersion(sent, updated.rowVersion);
      void queryClient.invalidateQueries({ queryKey: ["admin", "projects"] });
      toast(status === "archived" ? en["project.archived"] : en["project.restored"]);
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") {
        waits.waitFor("status", error);
        const conflict = error.status === 412;
        setFailure({
          error,
          lines: conflict ? [en["admin.conflict.action"]] : problemLines(error),
        });
        if (conflict) void query.refetch();
      }
    } finally {
      setBusy(false);
      setConfirmArchive(false);
    }
  };

  const onVersionMoved = (sent: string, next: string) => {
    form.moveRowVersion(sent, next);
  };

  const archived = project?.status === "archived";
  const loadError = project === undefined && query.isError ? apiErrorOf(query.error) : null;

  let body;
  if (project === undefined) {
    if (loadError === null) body = <SkeletonItem size={32} />;
    else if (isProblem(loadError, 404)) {
      body = <NotFoundBar error={loadError} list={en["projects.title"]} to="/admin/projects" />;
    } else {
      body = (
        <ApiErrorBar
          error={loadError}
          onRetry={() => {
            void query.refetch();
          }}
        />
      );
    }
  } else {
    body = (
      <>
        {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
        <TabList
          selectedValue={tab}
          onTabSelect={(_, data) => {
            setFilter("tab", String(data.value));
          }}
        >
          <Tab value="details">{en["project.tabs.details"]}</Tab>
          <Tab value="members">{en["project.tabs.members"]}</Tab>
          <Tab value="indexes">{en["project.tabs.indexes"]}</Tab>
        </TabList>
        <div role="tabpanel" aria-label={en[`project.tabs.${tab}`]}>
          {tab === "details" && form.rowVersion !== "" ? (
            <DetailsTab projectId={projectId} form={form} />
          ) : null}
          {tab === "members" ? (
            <MembersTab project={project} onVersionMoved={onVersionMoved} />
          ) : null}
          {tab === "indexes" ? (
            <IndexesTab project={project} onVersionMoved={onVersionMoved} />
          ) : null}
        </div>
        <ConfirmDialog
          open={confirmArchive}
          title={fillText(en["project.archiveConfirm.title"], { name: project.name })}
          body={en["project.archiveConfirm.body"]}
          actionLabel={en["project.archiveConfirm.action"]}
          busy={busy}
          onConfirm={() => {
            void setStatus("archived");
          }}
          onCancel={() => {
            setConfirmArchive(false);
          }}
        />
      </>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader
        title={
          project === undefined ? (
            <SkeletonItem className={styles.skeleton} size={24} />
          ) : (
            <span className={styles.title}>
              {project.name}
              <StatusBadge kind="projectStatus" value={project.status} />
            </span>
          )
        }
        breadcrumbs={[
          { label: en["projects.title"], to: "/admin/projects" },
          ...(project === undefined ? [] : [{ label: project.name }]),
        ]}
        {...(project === undefined
          ? {}
          : {
              actions: (
                <Button
                  icon={archived ? <ArrowUndo20Regular /> : <Archive20Regular />}
                  disabled={busy || waits.isWaiting("status")}
                  onClick={() => {
                    if (archived) void setStatus("active");
                    else setConfirmArchive(true);
                  }}
                >
                  {archived ? en["project.restore"] : en["project.archive"]}
                </Button>
              ),
            })}
      />
      {body}
    </div>
  );
}
