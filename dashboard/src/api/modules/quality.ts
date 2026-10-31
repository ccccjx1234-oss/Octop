import { request } from "../request";

export interface ModelHealthRow {
  ref: string;
  state: "healthy" | "open";
  cooldown_until: number | null;
  failures?: number;
  window_s: number;
  calls: number;
  success_rate: number | null;
}

export interface ModelHealthResponse {
  models: ModelHealthRow[];
  fallback_chain: string[];
}

export interface ModelPrice {
  model: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  currency: string;
  updated_at?: number;
}

export interface CostReport {
  window: string;
  currency: string;
  total_cost: number;
  by_model: {
    model: string;
    total_tokens: number;
    cost: number;
    priced: boolean;
  }[];
  by_user: {
    user_id: number;
    username: string;
    total_tokens: number;
    cost: number;
  }[];
}

export interface FeedbackSummary {
  days: number;
  up: number;
  down: number;
  satisfaction: number | null;
  per_agent: {
    agent_id: string;
    agent_name: string;
    up: number;
    down: number;
    satisfaction: number | null;
  }[];
  recent_down: {
    agent_id: string;
    agent_name: string;
    thread_id: string;
    message_id: string;
    username: string;
    comment: string;
    excerpt: string;
    updated_at: number;
  }[];
}

export interface EvalCase {
  eval_case_id: string;
  suite: string;
  prompt: string;
  expect_contains: string[];
  expect_not_contains: string[];
}

export type EvalCaseInput = Omit<EvalCase, "eval_case_id">;

export interface EvalRun {
  eval_run_id: string;
  agent_id: string;
  suite: string;
  status: "running" | "done" | "failed";
  total: number;
  passed: number;
  error: string;
  started_at: number;
  finished_at: number | null;
}

export interface EvalResult {
  eval_case_id: string;
  prompt: string;
  passed: boolean;
  output: string;
  detail: string;
  latency_ms: number;
}

export interface ToolPermissionRow {
  tool_name: string;
  builtin: boolean;
  group_ids: string[];
}

const BASE = "/admin/quality";
const json = (body: unknown): RequestInit => ({
  body: JSON.stringify(body),
  headers: { "Content-Type": "application/json" },
});

export const qualityApi = {
  modelHealth: () => request<ModelHealthResponse>(`${BASE}/model-health`),
  saveFallbackChain: (refs: string[]) =>
    request<{ fallback_chain: string[] }>(`${BASE}/fallback-chain`, {
      method: "PUT",
      ...json({ refs }),
    }),
  pricing: () => request<ModelPrice[]>(`${BASE}/pricing`),
  savePrice: (price: ModelPrice) =>
    request<void>(`${BASE}/pricing`, { method: "PUT", ...json(price) }),
  deletePrice: (model: string) =>
    request<void>(`${BASE}/pricing?model=${encodeURIComponent(model)}`, {
      method: "DELETE",
    }),
  cost: (window: string) =>
    request<CostReport>(`${BASE}/cost?window=${encodeURIComponent(window)}`),
  feedback: (days: number) =>
    request<FeedbackSummary>(`${BASE}/feedback?days=${days}`),
  evalCases: () => request<EvalCase[]>(`${BASE}/eval-cases`),
  createEvalCase: (input: EvalCaseInput) =>
    request<EvalCase>(`${BASE}/eval-cases`, { method: "POST", ...json(input) }),
  updateEvalCase: (id: string, input: EvalCaseInput) =>
    request<EvalCase>(`${BASE}/eval-cases/${encodeURIComponent(id)}`, {
      method: "PUT",
      ...json(input),
    }),
  deleteEvalCase: (id: string) =>
    request<void>(`${BASE}/eval-cases/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  evalRuns: () => request<EvalRun[]>(`${BASE}/eval-runs`),
  startEvalRun: (agentId: string, suite: string) =>
    request<EvalRun>(`${BASE}/eval-runs`, {
      method: "POST",
      ...json({ agent_id: agentId, suite }),
    }),
  evalRun: (id: string) =>
    request<EvalRun & { results: EvalResult[] }>(
      `${BASE}/eval-runs/${encodeURIComponent(id)}`,
    ),
  toolPermissions: () =>
    request<{ tools: ToolPermissionRow[] }>(`${BASE}/tool-permissions`),
  saveToolPermission: (toolName: string, groupIds: string[]) =>
    request<unknown>(
      `${BASE}/tool-permissions/${encodeURIComponent(toolName)}`,
      { method: "PUT", ...json({ group_ids: groupIds }) },
    ),
};

export const feedbackApi = {
  threadRatings: (agentId: string, threadId: string) =>
    request<{ ratings: Record<string, number> }>(
      `/agents/${encodeURIComponent(agentId)}/threads/${encodeURIComponent(
        threadId,
      )}/feedback`,
    ),
  rate: (
    agentId: string,
    threadId: string,
    messageId: string,
    rating: number,
    excerpt: string,
    comment = "",
  ) =>
    request<void>(
      `/agents/${encodeURIComponent(agentId)}/threads/${encodeURIComponent(
        threadId,
      )}/messages/${encodeURIComponent(messageId)}/feedback`,
      { method: "PUT", ...json({ rating, excerpt, comment }) },
    ),
};
