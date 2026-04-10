import { Dropdown, Option } from "@fluentui/react-components";
import type { Project } from "../api/types";

interface Props {
  projects: Project[];
  selected: Project | null;
  onSelect: (project: Project) => void;
}

export function ProjectSelector({ projects, selected, onSelect }: Props) {
  return (
    <Dropdown
      placeholder="Select a project"
      value={selected?.display_name || ""}
      selectedOptions={selected ? [selected.name] : []}
      onOptionSelect={(_, data) => {
        const project = projects.find((p) => p.name === data.optionValue);
        if (project) onSelect(project);
      }}
      style={{ minWidth: "200px" }}
    >
      {projects.map((p) => (
        <Option key={p.name} value={p.name}>
          {p.display_name || p.name}
        </Option>
      ))}
    </Dropdown>
  );
}
