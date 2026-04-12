import { useState } from "react";
import {
  makeStyles,
  Text,
  Card,
  CardHeader,
  tokens,
  Badge,
  Switch,
  Dropdown,
  Option,
  Slider,
  Input,
  Field,
} from "@fluentui/react-components";
import {
  PersonRegular,
  PaintBrushRegular,
  ChatSettingsRegular,
  InfoRegular,
  WeatherMoonRegular,
  WeatherSunnyRegular,
} from "@fluentui/react-icons";
import { useAuth } from "../hooks/useAuth";
import { useThemeContext, type ThemeMode } from "../context/ThemeContext";
import { useProjects } from "../hooks/useProjects";

const useStyles = makeStyles({
  root: {
    padding: "32px",
    maxWidth: "720px",
    margin: "0 auto",
  },
  title: {
    fontSize: "24px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "24px",
    display: "block",
  },
  stack: {
    display: "flex",
    flexDirection: "column",
    gap: "16px",
  },
  cardBody: {
    padding: "16px",
    display: "flex",
    flexDirection: "column",
    gap: "16px",
  },
  profileRow: {
    display: "flex",
    alignItems: "center",
    gap: "16px",
  },
  avatar: {
    width: "48px",
    height: "48px",
    borderRadius: "50%",
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground1,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: "18px",
    fontWeight: tokens.fontWeightSemibold,
    flexShrink: 0,
  },
  profileDetails: {
    display: "flex",
    flexDirection: "column",
    gap: "2px",
  },
  profileName: {
    fontSize: "16px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
  },
  profileEmail: {
    fontSize: "13px",
    color: tokens.colorNeutralForeground3,
  },
  infoRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
  },
  infoLabel: {
    fontSize: "14px",
    color: tokens.colorNeutralForeground2,
  },
  themeRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
  },
  themeLabel: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    fontSize: "14px",
    color: tokens.colorNeutralForeground2,
  },
  themeDropdown: {
    minWidth: "140px",
  },
  aboutVersion: {
    fontSize: "14px",
    color: tokens.colorNeutralForeground1,
  },
  aboutDescription: {
    fontSize: "13px",
    color: tokens.colorNeutralForeground3,
    lineHeight: "1.5",
  },
  linkRow: {
    display: "flex",
    gap: "16px",
  },
});

const themeOptions: { value: ThemeMode; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

export function SettingsPage() {
  const styles = useStyles();
  const { user, role } = useAuth();
  const { mode, setMode } = useThemeContext();
  const { projects, selectedProject, setSelectedProject } = useProjects();
  const [temperature, setTemperature] = useState(0.7);
  const [topK, setTopK] = useState(5);
  const [followups, setFollowups] = useState(true);

  const initials = user?.displayName
    ? user.displayName
        .split(" ")
        .map((n) => n[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : "?";

  return (
    <div className={styles.root}>
      <Text className={styles.title}>Settings</Text>

      <div className={styles.stack}>
        {/* Profile */}
        <Card>
          <CardHeader
            image={<PersonRegular style={{ fontSize: 20, color: tokens.colorBrandForeground1 }} />}
            header={<Text weight="semibold">Profile</Text>}
          />
          <div className={styles.cardBody}>
            <div className={styles.profileRow}>
              <div className={styles.avatar}>{initials}</div>
              <div className={styles.profileDetails}>
                <Text className={styles.profileName}>
                  {user?.displayName || "N/A"}
                </Text>
                <Text className={styles.profileEmail}>
                  {user?.mail || "N/A"}
                </Text>
              </div>
              <Badge
                color={role === "admin" ? "brand" : "informative"}
                size="small"
                style={{ marginLeft: "auto" }}
              >
                {role}
              </Badge>
            </div>
          </div>
        </Card>

        {/* Appearance */}
        <Card>
          <CardHeader
            image={<PaintBrushRegular style={{ fontSize: 20, color: tokens.colorBrandForeground1 }} />}
            header={<Text weight="semibold">Appearance</Text>}
          />
          <div className={styles.cardBody}>
            <div className={styles.themeRow}>
              <div className={styles.themeLabel}>
                {mode === "dark" ? <WeatherMoonRegular /> : <WeatherSunnyRegular />}
                Theme
              </div>
              <Dropdown
                className={styles.themeDropdown}
                value={themeOptions.find((t) => t.value === mode)?.label || "System"}
                selectedOptions={[mode]}
                onOptionSelect={(_, d) => setMode(d.optionValue as ThemeMode)}
              >
                {themeOptions.map((t) => (
                  <Option key={t.value} value={t.value}>
                    {t.label}
                  </Option>
                ))}
              </Dropdown>
            </div>
          </div>
        </Card>

        {/* Default Chat Settings */}
        <Card>
          <CardHeader
            image={<ChatSettingsRegular style={{ fontSize: 20, color: tokens.colorBrandForeground1 }} />}
            header={<Text weight="semibold">Default Chat Settings</Text>}
          />
          <div className={styles.cardBody}>
            <Field label="Default Project">
              <Dropdown
                placeholder="Select a project"
                value={selectedProject?.display_name || ""}
                selectedOptions={selectedProject ? [selectedProject.name] : []}
                onOptionSelect={(_, data) => {
                  const p = projects.find((proj) => proj.name === data.optionValue);
                  if (p) setSelectedProject(p);
                }}
              >
                {projects.map((p) => (
                  <Option key={p.name} value={p.name}>
                    {p.display_name || p.name}
                  </Option>
                ))}
              </Dropdown>
            </Field>

            <Field label={`Temperature: ${temperature.toFixed(1)}`}>
              <Slider
                min={0}
                max={1}
                step={0.1}
                value={temperature}
                onChange={(_, d) => setTemperature(d.value)}
              />
            </Field>

            <Field label="Top K">
              <Input
                type="number"
                value={String(topK)}
                onChange={(_, d) => setTopK(Number(d.value) || 5)}
                style={{ maxWidth: "120px" }}
              />
            </Field>

            <Switch
              label="Suggest follow-up questions"
              checked={followups}
              onChange={(_, d) => setFollowups(d.checked)}
            />
          </div>
        </Card>

        {/* About */}
        <Card>
          <CardHeader
            image={<InfoRegular style={{ fontSize: 20, color: tokens.colorBrandForeground1 }} />}
            header={<Text weight="semibold">About</Text>}
          />
          <div className={styles.cardBody}>
            <Text className={styles.aboutVersion}>
              rag-app-azure v1.0.0
            </Text>
            <Text className={styles.aboutDescription}>
              A production-grade RAG application on Azure with hybrid search,
              NDJSON streaming, and MSAL SSO authentication.
            </Text>
          </div>
        </Card>
      </div>
    </div>
  );
}
