import { makeStyles, tokens, Text, mergeClasses } from "@fluentui/react-components";
import { PersonRegular, BotRegular } from "@fluentui/react-icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import type { ChatMessage as ChatMessageType } from "../api/types";

const useStyles = makeStyles({
  row: {
    display: "flex",
    gap: "12px",
    padding: "12px 0",
    maxWidth: "800px",
  },
  rowUser: {
    flexDirection: "row-reverse",
    marginLeft: "auto",
  },
  avatar: {
    width: "32px",
    height: "32px",
    borderRadius: "50%",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    fontSize: "16px",
  },
  avatarUser: {
    backgroundColor: tokens.colorBrandBackground,
    color: tokens.colorNeutralForegroundOnBrand,
  },
  avatarBot: {
    backgroundColor: tokens.colorNeutralBackground4,
    color: tokens.colorNeutralForeground2,
  },
  bubble: {
    padding: "12px 16px",
    borderRadius: "12px",
    maxWidth: "680px",
    lineHeight: "1.5",
    fontSize: "14px",
    "& p": { margin: "0 0 8px 0" },
    "& p:last-child": { marginBottom: 0 },
    "& pre": {
      backgroundColor: tokens.colorNeutralBackground4,
      padding: "12px",
      borderRadius: "6px",
      overflowX: "auto",
      margin: "8px 0",
    },
    "& code": {
      fontSize: "13px",
      fontFamily: "'Cascadia Code', 'Fira Code', monospace",
    },
    "& table": {
      borderCollapse: "collapse",
      width: "100%",
      margin: "8px 0",
    },
    "& th, & td": {
      border: `1px solid ${tokens.colorNeutralStroke2}`,
      padding: "6px 10px",
      textAlign: "left",
      fontSize: "13px",
    },
    "& th": {
      backgroundColor: tokens.colorNeutralBackground3,
      fontWeight: tokens.fontWeightSemibold,
    },
    "& ul, & ol": {
      paddingLeft: "20px",
      margin: "4px 0 8px 0",
    },
    "& li": {
      marginBottom: "4px",
    },
  },
  bubbleUser: {
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorNeutralForeground1,
    borderBottomRightRadius: "4px",
  },
  bubbleBot: {
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderBottomLeftRadius: "4px",
  },
  citation: {
    display: "inline-flex",
    alignItems: "center",
    gap: "4px",
    padding: "2px 8px",
    borderRadius: "4px",
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground1,
    fontSize: "12px",
    fontWeight: tokens.fontWeightSemibold,
    cursor: "pointer",
    marginRight: "4px",
    "&:hover": {
      backgroundColor: tokens.colorBrandBackground2Hover,
    },
  },
});

interface Props {
  message: ChatMessageType;
}

export function ChatMessage({ message }: Props) {
  const styles = useStyles();
  const isUser = message.role === "user";

  return (
    <div className={mergeClasses(styles.row, isUser && styles.rowUser)}>
      <div
        className={mergeClasses(
          styles.avatar,
          isUser ? styles.avatarUser : styles.avatarBot,
        )}
      >
        {isUser ? <PersonRegular /> : <BotRegular />}
      </div>
      <div
        className={mergeClasses(
          styles.bubble,
          isUser ? styles.bubbleUser : styles.bubbleBot,
        )}
      >
        {isUser ? (
          <Text>{message.content}</Text>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
            {message.content}
          </ReactMarkdown>
        )}
      </div>
    </div>
  );
}
