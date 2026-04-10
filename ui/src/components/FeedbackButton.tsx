import { useState } from "react";
import { Button, Tooltip } from "@fluentui/react-components";
import {
  ThumbLikeRegular,
  ThumbLikeFilled,
  ThumbDislikeRegular,
  ThumbDislikeFilled,
} from "@fluentui/react-icons";
import { apiClient } from "../api/client";

interface Props {
  sessionId: string;
  timestamp: string;
}

export function FeedbackButton({ sessionId, timestamp }: Props) {
  const [feedback, setFeedback] = useState<"positive" | "negative" | null>(null);

  const submitFeedback = async (type: "positive" | "negative") => {
    const newFeedback = feedback === type ? null : type;
    setFeedback(newFeedback);

    if (newFeedback) {
      await apiClient("/feedback", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          timestamp,
          feedback_type: newFeedback,
          feedback_message: "",
        }),
      });
    }
  };

  return (
    <span style={{ display: "inline-flex", gap: 2 }}>
      <Tooltip content="Helpful" relationship="label">
        <Button
          appearance="subtle"
          size="small"
          icon={
            feedback === "positive" ? (
              <ThumbLikeFilled />
            ) : (
              <ThumbLikeRegular />
            )
          }
          onClick={() => submitFeedback("positive")}
        />
      </Tooltip>
      <Tooltip content="Not helpful" relationship="label">
        <Button
          appearance="subtle"
          size="small"
          icon={
            feedback === "negative" ? (
              <ThumbDislikeFilled />
            ) : (
              <ThumbDislikeRegular />
            )
          }
          onClick={() => submitFeedback("negative")}
        />
      </Tooltip>
    </span>
  );
}
