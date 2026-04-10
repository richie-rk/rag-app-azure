import { useState, type KeyboardEvent } from "react";
import {
  makeStyles,
  tokens,
  Textarea,
  Button,
} from "@fluentui/react-components";
import { SendRegular } from "@fluentui/react-icons";

const useStyles = makeStyles({
  container: {
    display: "flex",
    gap: "8px",
    padding: "16px",
    borderTop: `1px solid ${tokens.colorNeutralStroke1}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  input: {
    flex: 1,
  },
});

interface Props {
  onSend: (message: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function ChatInput({ onSend, disabled, placeholder }: Props) {
  const styles = useStyles();
  const [value, setValue] = useState("");

  const handleSend = () => {
    const trimmed = value.trim();
    if (trimmed && !disabled) {
      onSend(trimmed);
      setValue("");
    }
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className={styles.container}>
      <Textarea
        className={styles.input}
        value={value}
        onChange={(_, data) => setValue(data.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder || "Ask a question..."}
        disabled={disabled}
        resize="vertical"
      />
      <Button
        appearance="primary"
        icon={<SendRegular />}
        onClick={handleSend}
        disabled={disabled || !value.trim()}
      >
        Send
      </Button>
    </div>
  );
}
