// New project (`/admin/projects/new`, 10-web-client.md section 7.6): the Members picker and the
// project form; Create project calls `adminCreateProject`, and its `201` opens the new project's
// Details tab with the toast `project.created`.

import { Button, Spinner, makeStyles, tokens } from "@fluentui/react-components";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { request } from "../../api/http";
import { projectCreateInitialMemberIds } from "../../api/generated/limits.gen";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { useRetryWaits } from "../../ui/retryWait";
import {
  problemLines,
  unmatchedLines,
  useFieldFocus,
  type FieldMessages,
} from "../shared/formErrors";
import { PeoplePicker, personName } from "../shared/PeoplePicker";
import { adminKeys, type UserSummary } from "../shared/queries";
import { useAdminToast } from "../shared/toast";
import { ProjectForm } from "./ProjectForm";
import {
  EMPTY_PROJECT_FORM,
  checkProjectForm,
  createProjectBody,
  projectFieldOrder,
  projectSchemaFields,
  routeProjectErrors,
  type ProjectFormValues,
} from "./projectForm";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  actions: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalS },
});

const MEMBERS_FIELD = "initialMemberIds";

/** The Members picker's error for `422` entries at `body.initialMemberIds.<i>` with reason
 * `not_found`: one sentence per person, naming the person at position `<i>` of the list sent. */
export function membersNotFound(error: ApiError, sent: readonly UserSummary[]): string | null {
  const names = error.errors.flatMap((entry) => {
    const match = /^body\.initialMemberIds\.(\d+)$/.exec(entry.path);
    if (match?.[1] === undefined || entry.reason !== "not_found") return [];
    const person = sent[Number(match[1])];
    return person === undefined ? [] : [personName(person)];
  });
  return names.length === 0
    ? null
    : names.map((name) => fillText(en["project.form.members.notFound"], { name })).join(" ");
}

export function NewProjectScreen() {
  const styles = useStyles();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { register, focusFirst } = useFieldFocus();
  useDocumentTitle(en["project.new.title"]);
  const [values, setValues] = useState<ProjectFormValues>(EMPTY_PROJECT_FORM);
  const [members, setMembers] = useState<UserSummary[]>([]);
  const [errors, setErrors] = useState<FieldMessages>(new Map());
  const [failure, setFailure] = useState<{ error: ApiError; lines: string[] } | null>(null);
  const [busy, setBusy] = useState(false);

  const order = [MEMBERS_FIELD, ...projectFieldOrder(values)];

  const showErrors = (messages: FieldMessages) => {
    setErrors(messages);
    focusFirst(order, messages);
  };

  const create = async () => {
    setFailure(null);
    const body = createProjectBody(
      values,
      members.map((person) => person.userId),
    );
    const checked = checkProjectForm(values);
    const messages = checked.size > 0 ? checked : projectSchemaFields(body, values, true);
    if (messages.size > 0) {
      showErrors(messages);
      return;
    }
    setErrors(new Map());
    setBusy(true);
    try {
      const project = await request("adminCreateProject", { body });
      queryClient.setQueryData(adminKeys.project(project.projectId), project);
      void queryClient.invalidateQueries({ queryKey: ["admin", "projects"] });
      void navigate(`/admin/projects/${project.projectId}`);
      toast(en["project.created"]);
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return;
      waits.waitFor("create", error);
      if (isProblem(error, 409, "project_name_taken")) {
        showErrors(new Map([["name", error.detail ?? ""]]));
        return;
      }
      if (isProblem(error, 422)) {
        const notFound = membersNotFound(error, members);
        const memberPaths = members.map((_, index) => `${MEMBERS_FIELD}.${String(index)}`);
        const routed = routeProjectErrors(error, values, memberPaths);
        const fieldMessages = new Map(
          [...routed.messages].filter(([field]) => !field.startsWith(`${MEMBERS_FIELD}.`)),
        );
        if (notFound !== null) fieldMessages.set(MEMBERS_FIELD, notFound);
        else if (routed.messages.size > fieldMessages.size) {
          fieldMessages.set(MEMBERS_FIELD, en["error.validation.field"]);
        }
        showErrors(fieldMessages);
        if (routed.unmatched.length > 0 || fieldMessages.size === 0) {
          setFailure({ error, lines: unmatchedLines(routed.unmatched) });
        }
        return;
      }
      setFailure({ error, lines: problemLines(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.root}>
      <PageHeader
        title={en["project.new.title"]}
        breadcrumbs={[
          { label: en["projects.title"], to: "/admin/projects" },
          { label: en["project.new.title"] },
        ]}
      />
      <ProjectForm
        values={values}
        onChange={(name, value) => {
          setValues((current) => ({ ...current, [name]: value }));
        }}
        errors={errors}
        register={register}
        members={
          <PeoplePicker
            label={en["project.form.members"]}
            hint={en["project.form.members.hint"]}
            placeholder={en["members.picker.placeholder"]}
            selected={members}
            maxPeople={projectCreateInitialMemberIds.maxItems}
            error={errors.get(MEMBERS_FIELD) ?? null}
            rootRef={register(MEMBERS_FIELD)}
            onChange={(next) => {
              setMembers(next);
              if (errors.has(MEMBERS_FIELD)) {
                setErrors((current) => {
                  const without = new Map(current);
                  without.delete(MEMBERS_FIELD);
                  return without;
                });
              }
            }}
          />
        }
        actions={
          <>
            {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
            <div className={styles.actions}>
              <Button
                appearance="primary"
                disabled={busy || waits.isWaiting("create")}
                {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
                onClick={() => {
                  void create();
                }}
              >
                {en["project.create"]}
              </Button>
              <Button
                onClick={() => {
                  void navigate("/admin/projects");
                }}
              >
                {en["project.cancel"]}
              </Button>
            </div>
          </>
        }
      />
    </div>
  );
}
