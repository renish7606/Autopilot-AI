export type EventName =
  | "session_created" | "task_started" | "planning" | "step_start" | "step_done"
  | "approval_required" | "clarification" | "task_completed" | "task_failed" | "error";

export interface AgentEvent {
  event: EventName;
  session_id?: string;
  goal?: string;
  planner?: string;
  step_index?: number;
  plan?: PlanStep;
  tool_result?: ToolResult;
  verification?: Verification;
  attempts?: number;
  report?: AgentReport;
  question?: string;
  candidates?: unknown[];
  message?: string;
  [key: string]: unknown;
}

export interface PlanStep {
  step_id?: string;
  description: string;
  tool_name: string;
  arguments?: Record<string, unknown>;
  expected_result?: string;
  risk_level?: "low" | "medium" | "high" | string;
  requires_approval?: boolean;
}

export interface ToolResult {
  status: "success" | "error" | "denied" | string;
  output: unknown;
  error_code?: string | null;
}

export interface Verification {
  passed: boolean;
  summary: string;
  evidence: unknown;
}

export interface StepReport {
  plan?: PlanStep;
  tool_result?: ToolResult;
  verification?: Verification;
  attempts?: number;
}

export interface AgentReport {
  status: string;
  goal: string;
  session_id?: string;
  planner?: string;
  approved_roots?: string[];
  plan?: PlanStep;
  tool_result?: ToolResult;
  verification?: Verification;
  steps?: StepReport[];
  message?: string;
  planner_note?: string;
  workflow?: string;
  observations?: Array<Record<string, unknown>>;
  summary?: string;
}

export interface SessionInfo {
  session_id: string;
  task_count: number;
  last_goal: string;
}

export interface HistoryEntry {
  id: number;
  session_id: string;
  timestamp: string;
  goal: string;
  tool_name: string;
  status: string;
  summary: string;
  output: unknown;
}

export interface RunOptions {
  planner?: "ollama" | "heuristic";
  workspace?: string;
}
