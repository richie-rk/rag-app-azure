import {
  makeStyles,
  tokens,
  Text,
  Button,
  Divider,
  mergeClasses,
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
    borderRight: `1px solid ${tokens.colorNeutralStroke2}`,
    display: "flex",
    flexDirection: "column",
    backgroundColor: tokens.colorNeutralBackground2,
    flexShrink: 0,
  },
  header: {
    padding: "14px 16px",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    height: "52px",
    boxSizing: "border-box",
  },
  headerTitle: {
    fontSize: "14px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
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
    transitionProperty: "background-color",
    transitionDuration: "0.15s",
    "&:hover": {
      backgroundColor: tokens.colorNeutralBackground3Hover,
    },
  },
  itemActive: {
    backgroundColor: tokens.colorNeutralBackground3Selected,
  },
  itemIcon: {
    fontSize: "16px",
    color: tokens.colorNeutralForeground3,
    flexShrink: 0,
  },
  itemText: {
    flex: 1,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontSize: "13px",
    color: tokens.colorNeutralForeground2,
  },
  deleteBtn: {
    opacity: 0,
    transitionProperty: "opacity",
    transitionDuration: "0.15s",
    "&:hover": {
      opacity: 1,
    },
  },
  itemHover: {
    "&:hover button": {
      opacity: 1,
    },
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
        <Text className={styles.headerTitle}>Sessions</Text>
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
            className={mergeClasses(
              styles.item,
              styles.itemHover,
              session.session_id === activeSessionId && styles.itemActive,
            )}
            onClick={() => onSelect(session.session_id)}
          >
            <ChatRegular className={styles.itemIcon} />
            <span className={styles.itemText}>
              {session.session_name || "Untitled"}
            </span>
            <Button
              appearance="subtle"
              icon={<DeleteRegular />}
              size="small"
              className={styles.deleteBtn}
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
