// The project picker (10-web-client.md section 8.3): every reachable project by name, a project
// without a default index marked as having no documents yet. In an open session it shows the
// session's project, and the hint announces that choosing another starts a new chat before it
// happens (WCAG 3.2.2).

import { Dropdown, Field, Option, Spinner, makeStyles, tokens } from "@fluentui/react-components";

import type { ProjectSummary } from "../../api/queryClient";
import { en } from "../../copy/en";
import { sortedProjects } from "./selection";

const useStyles = makeStyles({
  field: { minWidth: "0", flexGrow: 1, maxWidth: "420px" },
  loading: { display: "flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
  dropdown: { minWidth: "0", width: "100%" },
});

/** An option's text: the name, with the suffix when the project isn't ready. */
export function projectLabel(project: ProjectSummary): string {
  return project.hasDefaultIndex
    ? project.name
    : `${project.name} ${en["chat.project.notReadySuffix"]}`;
}

export interface ProjectPickerProps {
  readonly projects: readonly ProjectSummary[] | null;
  readonly selected: ProjectSummary | null;
  /** In an open session, where choosing another project starts a new chat. */
  readonly inSession: boolean;
  /** `listMyProjects` failed: the picker stays disabled, and the main panel shows the error. */
  readonly failed: boolean;
  readonly onSelect: (projectId: string) => void;
}

export function ProjectPicker({
  projects,
  selected,
  inSession,
  failed,
  onSelect,
}: ProjectPickerProps) {
  const styles = useStyles();
  let hint: string | undefined;
  if (inSession) hint = en["chat.project.changeHint"];
  else if (projects !== null && projects.length > 0 && selected === null) {
    hint = en["chat.project.chooseFirst"];
  }
  return (
    <Field
      label={en["chat.project.label"]}
      className={styles.field}
      {...(hint === undefined ? {} : { hint })}
    >
      {projects === null ? (
        <div className={styles.loading}>
          <Dropdown
            disabled
            placeholder={en["chat.project.placeholder"]}
            className={styles.dropdown}
          />
          {failed ? null : <Spinner size="tiny" aria-label={en["chat.project.label"]} />}
        </div>
      ) : (
        <Dropdown
          className={styles.dropdown}
          placeholder={en["chat.project.placeholder"]}
          value={selected === null ? "" : projectLabel(selected)}
          selectedOptions={selected === null ? [] : [selected.projectId]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined && data.optionValue !== selected?.projectId) {
              onSelect(data.optionValue);
            }
          }}
        >
          {sortedProjects(projects).map((project) => (
            <Option key={project.projectId} value={project.projectId} text={projectLabel(project)}>
              {projectLabel(project)}
            </Option>
          ))}
        </Dropdown>
      )}
    </Field>
  );
}
