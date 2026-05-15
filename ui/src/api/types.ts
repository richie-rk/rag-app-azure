export interface ChatTurn {
  user: string;
  bot?: string;
}

export interface ChatOverrides {
  temperature?: number;
  top_k?: number;
  suggest_followup_questions?: boolean;
  prompt_template?: string;
  file_name?: string;
}

export interface ChatRequest {
  history: ChatTurn[];
  overrides?: ChatOverrides;
  search_index: string;
  username: string;
  app?: string;
  deployment?: string;
}

export interface DataPoint {
  content: string;
  sourcepage: string;
  sourcefile: string;
  id: string;
}

export interface StreamChunk {
  choices: {
    delta: { content?: string; role?: string };
    context?: {
      data_points?: string[];
      thoughts?: string;
      model?: string;
      followup_questions?: string[];
      retrieved_docs?: { sourcepage: string; id: string; sourcefile: string }[];
      prompt_tokens?: number;
      completion_tokens?: number;
      total_tokens?: number;
    };
    finish_reason: string | null;
    index: number;
  }[];
  object: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  dataPoints?: string[];
  followupQuestions?: string[];
  timestamp: string;
}

export interface Session {
  session_id: string;
  session_name: string;
  timestamp: string;
  username: string;
}

export interface Project {
  id: number;
  name: string;
  display_name: string;
  index_name: string;
  department: string;
  system_prompt: string;
  example_questions: string[];
  chunking_strategy: string;
  llm_deployment: string;
  is_default: boolean;
}

export interface UserInfo {
  id: number;
  email: string;
  display_name: string;
  role: string;
}
