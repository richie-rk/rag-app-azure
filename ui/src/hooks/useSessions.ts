import { useCallback, useEffect, useState } from "react";
import {
  deleteSession,
  getSessionMessages,
  getSessions,
} from "../api/sessions";
import type { Session } from "../api/types";
import { useAuth } from "../auth/AuthProvider";

export function useSessions() {
  const { user } = useAuth();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(false);

  const loadSessions = useCallback(async () => {
    if (!user?.mail) return;
    setLoading(true);
    try {
      const data = await getSessions(user.mail);
      // Deduplicate by session_id, keep most recent
      const map = new Map<string, Session>();
      for (const s of data) {
        if (!map.has(s.session_id) || s.timestamp > map.get(s.session_id)!.timestamp) {
          map.set(s.session_id, s);
        }
      }
      setSessions(
        Array.from(map.values()).sort(
          (a, b) => b.timestamp.localeCompare(a.timestamp),
        ),
      );
    } catch (err) {
      console.error("Failed to load sessions:", err);
    } finally {
      setLoading(false);
    }
  }, [user?.mail]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const removeSession = useCallback(
    async (sessionId: string) => {
      await deleteSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    },
    [],
  );

  const loadSessionHistory = useCallback(async (sessionId: string) => {
    return getSessionMessages(sessionId);
  }, []);

  return {
    sessions,
    loading,
    refresh: loadSessions,
    removeSession,
    loadSessionHistory,
  };
}
