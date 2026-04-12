import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  makeStyles,
  Text,
  Button,
  Input,
  Textarea,
  Dropdown,
  Option,
  Switch,
  Field,
  tokens,
  Card,
} from "@fluentui/react-components";
import { createProject, updateProject } from "../api/projects";
import { useProjects } from "../hooks/useProjects";

const useStyles = makeStyles({
  root: {
    padding: "32px",
    maxWidth: "680px",
    margin: "0 auto",
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "24px",
    display: "block",
  },
  form: {
    display: "flex",
    flexDirection: "column",
    gap: "20px",
  },
  actions: {
    display: "flex",
    gap: "8px",
    justifyContent: "flex-end",
    marginTop: "8px",
  },
  cancelBtn: {
    minWidth: "80px",
  },
  submitBtn: {
    minWidth: "100px",
  },
});

export function CreateProjectPage() {
  const styles = useStyles();
  const navigate = useNavigate();
  const { projectId } = useParams();
  const { projects } = useProjects();
  const existing = projectId ? projects.find((p) => p.id === Number(projectId)) : null;

  const [name, setName] = useState(existing?.name || "");
  const [displayName, setDisplayName] = useState(existing?.display_name || "");
  const [department, setDepartment] = useState(existing?.department || "");
  const [systemPrompt, setSystemPrompt] = useState(existing?.system_prompt || "");
  const [chunking, setChunking] = useState(existing?.chunking_strategy || "page_wise");
  const [llm, setLlm] = useState(existing?.llm_deployment || "gpt-4o");
  const [isDefault, setIsDefault] = useState(existing?.is_default || false);
  const [saving, setSaving] = useState(false);

  const handleSubmit = async () => {
    setSaving(true);
    try {
      const data = {
        name,
        display_name: displayName || name,
        department,
        system_prompt: systemPrompt,
        chunking_strategy: chunking,
        llm_deployment: llm,
        is_default: isDefault,
      };

      if (existing) {
        await updateProject(existing.id, data);
      } else {
        await createProject(data);
      }
      navigate("/projects");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.root}>
      <Text className={styles.title}>
        {existing ? "Edit Project" : "New Project"}
      </Text>

      <Card>
        <div className={styles.form}>
          <Field
            label="Project Name"
            required
            hint="Used as a unique identifier. Cannot be changed after creation."
          >
            <Input
              value={name}
              onChange={(_, d) => setName(d.value)}
              disabled={!!existing}
              placeholder="my-project"
            />
          </Field>

          <Field label="Display Name">
            <Input
              value={displayName}
              onChange={(_, d) => setDisplayName(d.value)}
              placeholder="My Project"
            />
          </Field>

          <Field label="Department">
            <Input
              value={department}
              onChange={(_, d) => setDepartment(d.value)}
              placeholder="Engineering, Legal, HR..."
            />
          </Field>

          <Field label="System Prompt">
            <Textarea
              value={systemPrompt}
              onChange={(_, d) => setSystemPrompt(d.value)}
              rows={6}
              placeholder="You are an AI assistant that helps users find information..."
              resize="vertical"
            />
          </Field>

          <Field label="Chunking Strategy">
            <Dropdown
              value={chunking}
              selectedOptions={[chunking]}
              onOptionSelect={(_, d) => setChunking(d.optionValue || "page_wise")}
            >
              <Option value="page_wise">Page-wise</Option>
            </Dropdown>
          </Field>

          <Field label="LLM Deployment">
            <Input
              value={llm}
              onChange={(_, d) => setLlm(d.value)}
              placeholder="gpt-4o"
            />
          </Field>

          <Switch
            label="Default project (auto-assigned to new users)"
            checked={isDefault}
            onChange={(_, d) => setIsDefault(d.checked)}
          />

          <div className={styles.actions}>
            <Button
              appearance="secondary"
              className={styles.cancelBtn}
              onClick={() => navigate("/projects")}
            >
              Cancel
            </Button>
            <Button
              appearance="primary"
              className={styles.submitBtn}
              onClick={handleSubmit}
              disabled={saving || !name}
            >
              {saving ? "Saving..." : existing ? "Update" : "Create"}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
