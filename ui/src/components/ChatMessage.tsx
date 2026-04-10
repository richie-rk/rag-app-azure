import { makeStyles, tokens, Text } from "@fluentui/react-components";
import { PersonRegular, BotRegular } from "@fluentui/react-icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage as ChatMessageType } from "../api/types";

const useStyles = makeStyles({
  container: {
    display: "flex",
    gap: "12px",
    padding: "16px",
    maxWidth: "800px",
  },
  userMessage: {
    flexDirection: "row-reverse",
  },
  icon: {
    width: "32px",
    height: "32px",
    borderRadius: "50%",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
  },
  userIcon: {
    backgroundColor: tokens.colorBrandBackground,
    color: tokens.colorNeutralForegroundOnBrand,
  },
  botIcon: {
    backgroundColor: tokens.colorNeutralBackground4,
  },
  bubble: {
    padding: "12px 16px",
    borderRadius: "8px",
    maxWidth: "680px",
    "& p": { margin: "0 0 8px 0" },
    "& p:last-child": { marginBottom: 0 },
    "& pre": {
      backgroundColor: tokens.colorNeutralBackground4,
      padding: "12px",
      borderRadius: "4px",
      overflowX: "auto",
    },
    "& code": { fontSize: tokens.fontSizeBase200 },
    "& table": {
      borderCollapse: "collapse",
      width: "100%",
    },
    "& th, & td": {
      border: `1px solid ${tokens.colorNeutralStroke1}`,
      padding: "6px 10px",
      textAlign: "left",
    },
  },
  userBubble: {
    backgroundColor: tokens.colorBrandBackground2,
  },
  botBubble: {
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke1}`,
  },
});

interface Props {
  message: ChatMessageType;
}

export function ChatMessage({ message }: Props) {
  const styles = useStyles();
  const isUser = message.role === "user";

  return (
    <div className={`${styles.container} ${isUser ? styles.userMessage : ""}`}>
      <div
        className={`${styles.icon} ${isUser ? styles.userIcon : styles.botIcon}`}
      >
        {isUser ? <PersonRegular /> : <BotRegular />}
      </div>
      <div
        className={`${styles.bubble} ${isUser ? styles.userBubble : styles.botBubble}`}
      >
        {isUser ? (
          <Text>{message.content}</Text>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        )}
      </div>
    </div>
  );
}
