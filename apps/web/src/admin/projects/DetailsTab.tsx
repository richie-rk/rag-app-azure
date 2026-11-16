// The Details tab (10-web-client.md sections 7.4 and 7.6): the project form without Members,
// prefilled; Save changes sends only the changed fields with the kept `rowVersion` and follows the
// edit-conflict steps of section 7.4. The form's state lives in the Project screen, so a draft
// survives switching tabs, and a member or mapping change can move its kept version.

import { Button, Spinner, makeStyles, tokens } from "@fluentui/react-components";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { useRetryWaits } from "../../ui/retryWait";
import { RouterLink } from "../../ui/RouterLink";
import type { EditForm } from "../shared/conflict";
import {
  problemLines,
  unmatchedLines,
  useFieldFocus,
  type FieldMessages,
} from "../shared/formErrors";
import { adminKeys, adminProjectQuery, type AdminProject } from "../shared/queries";
import { useAdminToast } from "../shared/toast";
import { ProjectForm } from "./ProjectForm";
import {
  checkProjectForm,
  projectFieldOrder,
  projectFormValues,
  projectSchemaFields,
  routeProjectErrors,
  updateProjectBody,
  type ProjectFormValues,
} from "./projectForm";

const useStyles = makeStyles({
  actions: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  buttons: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalS },
});

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
  readonly backToList?: boolean;
}

export interface DetailsTabProps {
  readonly projectId: string;
  readonly form: EditForm<ProjectFormValues>;
}

export function DetailsTab({ projectId, form }: DetailsTabProps) {
  const styles = useStyles();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { register, focusFirst } = useFieldFocus();
  const [errors, setErrors] = useState<FieldMessages>(new Map());
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  // A blank example row alone changes nothing the API would store.
  const hasChanges = Object.keys(updateProjectBody(form.base, form.draft)).length > 0;

  const showErrors = (messages: FieldMessages) => {
    setErrors(messages);
    focusFirst(projectFieldOrder(form.draft), messages);
  };

  /** Step 4: read the project once more and show what changed under the kept draft. */
  const conflict = async (error: ApiError) => {
    setFailure({ error, lines: [en["admin.conflict.body"]] });
    try {
      const latest: AdminProject = await queryClient.query({
        ...adminProjectQuery(projectId),
        staleTime: 0,
      });
      form.conflict(projectFormValues(latest), latest.rowVersion);
    } catch {
      // The draft stays; the next Save meets the same 412 and reads once more.
    }
  };

  const save = async () => {
    setFailure(null);
    const checked = checkProjectForm(form.draft);
    const body = updateProjectBody(form.base, form.draft);
    const messages = checked.size > 0 ? checked : projectSchemaFields(body, form.draft, false);
    if (messages.size > 0) {
      showErrors(messages);
      return;
    }
    setErrors(new Map());
    setBusy(true);
    try {
      const project = await request("adminUpdateProject", {
        path: { projectId },
        body,
        ifMatch: form.rowVersion,
      });
      queryClient.setQueryData(adminKeys.project(projectId), project);
      form.saved(projectFormValues(project), project.rowVersion);
      void queryClient.invalidateQueries({ queryKey: ["admin", "projects"] });
      toast(en["project.savedToast"]);
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return;
      waits.waitFor("save", error);
      if (error.status === 412) {
        await conflict(error);
      } else if (isProblem(error, 409, "project_name_taken")) {
        showErrors(new Map([["name", error.detail ?? ""]]));
      } else if (isProblem(error, 422)) {
        const routed = routeProjectErrors(error, form.draft);
        showErrors(routed.messages);
        if (routed.unmatched.length > 0 || routed.messages.size === 0) {
          setFailure({ error, lines: unmatchedLines(routed.unmatched) });
        }
      } else {
        setFailure({
          error,
          lines: problemLines(error),
          backToList: isProblem(error, 404, "project_not_found"),
        });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <ProjectForm
      values={form.draft}
      onChange={(name, value) => {
        form.setField(name, value);
        if (errors.size > 0) {
          setErrors((current) => {
            const next = new Map(current);
            for (const field of current.keys()) {
              if (field === name || field.startsWith(`${name}.`)) next.delete(field);
            }
            return next;
          });
        }
      }}
      errors={errors}
      conflicts={form.conflicts}
      onUseLatest={(name) => {
        form.takeLatest(name);
      }}
      register={register}
      actions={
        <div className={styles.actions}>
          {failure === null ? null : (
            <ApiErrorBar
              error={failure.error}
              lines={failure.lines}
              {...(failure.backToList === true
                ? {
                    actions: (
                      <RouterLink to="/admin/projects">
                        {fillText(en["admin.backToList"], { list: en["projects.title"] })}
                      </RouterLink>
                    ),
                  }
                : {})}
            />
          )}
          <div className={styles.buttons}>
            <Button
              appearance="primary"
              disabled={busy || !hasChanges || waits.isWaiting("save")}
              {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={() => {
                void save();
              }}
            >
              {en["project.save"]}
            </Button>
          </div>
        </div>
      }
    />
  );
}
