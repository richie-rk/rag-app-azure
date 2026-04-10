import {
  makeStyles,
  tokens,
  Text,
  Button,
  Divider,
} from "@fluentui/react-components";
import {
  ChatRegular,
  DeleteRegular,
  AddRegular,
} from "@fluentui/react-icons";
import type { Session } from "../api/types";

const useStyles = makeStyles({
  container: {
    width: "260px",
    borderRight: `1px solid ${tokens.colorNeutralStroke1}`,
    display: "flex",
    flexDirection: "column",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  header: {
    padding: "12px 16px",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  list: {
    flex: 1,
    overflowY: "auto",
    padding: "4px 8px",
  },
  item: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "8px 12px",
    borderRadius: "6px",
    cursor: "pointer",
    "&:hover": {
      backgroundColor: tokens.colorNeutralBackground3Hover,
    },
  },
  activeItem: {
    backgroundColor: tokens.colorNeutralBackground3Selected,
  },
  itemText: {
    flex: 1,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
});

interface Props {
  sessions: Session[];
  activeSessionId?: string;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  onNew: () => void;
}

export function SessionList({
  sessions,
  activeSessionId,
  onSelect,
  onDelete,
  onNew,
}: Props) {
  const styles = useStyles();

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <Text weight="semibold">Sessions</Text>
        <Button
          appearance="subtle"
          icon={<AddRegular />}
          onClick={onNew}
          size="small"
        />
      </div>
      <Divider />
      <div className={styles.list}>
        {sessions.map((session) => (
          <div
            key={session.session_id}
            className={`${styles.item} ${session.session_id === activeSessionId ? styles.activeItem : ""}`}
            onClick={() => onSelect(session.session_id)}
          >
            <ChatRegular />
            <span className={styles.itemText}>
              {session.session_name || "Untitled"}
            </span>
            <Button
              appearance="subtle"
              icon={<DeleteRegular />}
              size="small"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(session.session_id);
              }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
