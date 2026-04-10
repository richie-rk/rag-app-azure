import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { makeStyles, Text, Button, tokens } from "@fluentui/react-components";
import { v4 as uuidv4 } from "uuid";
import { ChatMessage } from "../components/ChatMessage";
import { ChatInput } from "../components/ChatInput";
import { CitationPanel } from "../components/CitationPanel";
import { SessionList } from "../components/SessionList";
import { ProjectSelector } from "../components/ProjectSelector";
import { FeedbackButton } from "../components/FeedbackButton";
import { useStreamChat } from "../hooks/useStreamChat";
import { useProjects } from "../hooks/useProjects";
import { useSessions } from "../hooks/useSessions";
import { useAuth } from "../hooks/useAuth";
import { saveSession } from "../api/sessions";
import type { ChatMessage as ChatMessageType, ChatTurn } from "../api/types";

const useStyles = makeStyles({
  root: {
    display: "flex",
    height: "100%",
  },
  main: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
  },
  toolbar: {
    padding: "8px 16px",
    display: "flex",
    alignItems: "center",
    gap: "12px",
    borderBottom: `1px solid ${tokens.colorNeutralStroke1}`,
  },
  messages: {
    flex: 1,
    overflowY: "auto",
    padding: "16px",
  },
  followup: {
    padding: "8px 16px",
    display: "flex",
    gap: "8px",
    flexWrap: "wrap",
  },
  empty: {
    flex: 1,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: tokens.colorNeutralForeground3,
  },
});

export function ChatPage() {
  const styles = useStyles();
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { projects, selectedProject, setSelectedProject } = useProjects();
  const { sessions, refresh: refreshSessions, removeSession, loadSessionHistory } = useSessions();
  const { sendMessage, isStreaming, streamingContent, dataPoints, followupQuestions } = useStreamChat();

  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState(sessionId || "");
  const [showCitations, setShowCitations] = useState(false);

  // Load session history when sessionId changes
  useEffect(() => {
    if (sessionId) {
      setCurrentSessionId(sessionId);
      loadSessionHistory(sessionId).then((history) => {
        const msgs: ChatMessageType[] = [];
        for (const h of history) {
          if (h.user_query) msgs.push({ role: "user", content: h.user_query, timestamp: h.timestamp });
          if (h.bot_response) msgs.push({ role: "assistant", content: h.bot_response, timestamp: h.timestamp });
        }
        setMessages(msgs);
      });
    }
  }, [sessionId, loadSessionHistory]);

  const handleSend = useCallback(
    async (text: string) => {
      if (!selectedProject || !user) return;

      const sid = currentSessionId || uuidv4();
      if (!currentSessionId) setCurrentSessionId(sid);

      // Add user message
      const userMsg: ChatMessageType = {
        role: "user",
        content: text,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg]);

      // Build history for API
      const history: ChatTurn[] = messages
        .reduce<ChatTurn[]>((acc, m) => {
          if (m.role === "user") {
            acc.push({ user: m.content });
          } else if (acc.length > 0) {
            acc[acc.length - 1].bot = m.content;
          }
          return acc;
        }, []);
      history.push({ user: text });

      // Stream response
      const response = await sendMessage({
        history,
        search_index: selectedProject.index_name,
        username: user.mail,
        overrides: { suggest_followup_questions: true },
      });

      if (response) {
        setMessages((prev) => [...prev, response]);

        // Save to session history
        const ts = new Date().toISOString();
        saveSession({
          session_id: sid,
          timestamp: ts,
          session_name: messages.length === 0 ? text.substring(0, 100) : "",
          username: user.mail,
          user_query: text,
          bot_response: response.content,
          scope: selectedProject.name,
          app: "rag-app-azure",
        });

        refreshSessions();
      }
    },
    [selectedProject, user, messages, currentSessionId, sendMessage, refreshSessions],
  );

  const handleNewSession = () => {
    setMessages([]);
    setCurrentSessionId("");
    navigate("/chat");
  };

  return (
    <div className={styles.root}>
      <SessionList
        sessions={sessions}
        activeSessionId={currentSessionId}
        onSelect={(id) => navigate(`/chat/${id}`)}
        onDelete={async (id) => {
          await removeSession(id);
          if (id === currentSessionId) handleNewSession();
        }}
        onNew={handleNewSession}
      />
      <div className={styles.main}>
        <div className={styles.toolbar}>
          <ProjectSelector
            projects={projects}
            selected={selectedProject}
            onSelect={setSelectedProject}
          />
          <Button
            appearance="subtle"
            onClick={() => setShowCitations(!showCitations)}
            size="small"
          >
            {showCitations ? "Hide Sources" : "Show Sources"}
          </Button>
        </div>

        {messages.length === 0 && !isStreaming ? (
          <div className={styles.empty}>
            <Text size={500}>Ask a question to get started</Text>
          </div>
        ) : (
          <div className={styles.messages}>
            {messages.map((msg, i) => (
              <div key={i}>
                <ChatMessage message={msg} />
                {msg.role === "assistant" && (
                  <div style={{ paddingLeft: 44 }}>
                    <FeedbackButton
                      sessionId={currentSessionId}
                      timestamp={msg.timestamp}
                    />
                  </div>
                )}
              </div>
            ))}
            {isStreaming && streamingContent && (
              <ChatMessage
                message={{
                  role: "assistant",
                  content: streamingContent,
                  timestamp: "",
                }}
              />
            )}
          </div>
        )}

        {followupQuestions.length > 0 && !isStreaming && (
          <div className={styles.followup}>
            {followupQuestions.map((q, i) => (
              <Button
                key={i}
                appearance="outline"
                size="small"
                onClick={() => handleSend(q)}
              >
                {q}
              </Button>
            ))}
          </div>
        )}

        <ChatInput
          onSend={handleSend}
          disabled={isStreaming}
          placeholder={
            selectedProject
              ? `Ask about ${selectedProject.display_name}...`
              : "Select a project first"
          }
        />
      </div>
      <CitationPanel dataPoints={dataPoints} visible={showCitations} />
    </div>
  );
}
