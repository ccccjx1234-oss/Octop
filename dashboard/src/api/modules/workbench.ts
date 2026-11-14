import { getAuthToken, request } from "../request";

const WORKBENCH_TIMEOUT_MS = 15_000;
const CLIENT_CACHE_TTL_MS = 45_000;
const responseCache = new Map<string, { ts: number; data: unknown }>();
let cachedAuthToken = "";

function syncCacheTokenScope(): void {
  const token = getAuthToken();
  if (token !== cachedAuthToken) {
    responseCache.clear();
    cachedAuthToken = token;
  }
}

export function invalidateWorkbenchCache(): void {
  responseCache.clear();
}

function readCached<T>(path: string): T | null {
  syncCacheTokenScope();
  const hit = responseCache.get(path);
  if (!hit) return null;
  if (Date.now() - hit.ts > CLIENT_CACHE_TTL_MS) {
    responseCache.delete(path);
    return null;
  }
  return hit.data as T;
}

function writeCached(path: string, data: unknown): void {
  syncCacheTokenScope();
  responseCache.set(path, { ts: Date.now(), data });
}

export function workbenchErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) {
    const clean = err.message
      .replace(/^Request failed:\s*\d+\s*/i, "")
      .replace(/^HTTP\s+\d+:\s*/i, "")
      .trim();
    if (clean) return `${fallback}：${clean}`;
  }
  return fallback;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException
    ? err.name === "AbortError"
    : err instanceof Error && err.name === "AbortError";
}

function shouldRetryWorkbenchRequest(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const status = (err as { status?: unknown }).status;
  if (status === 502 || status === 503 || status === 504) return true;
  if (!(err instanceof Error)) return false;
  const msg = err.message.toLowerCase();
  return (
    msg.includes("failed to fetch") ||
    msg.includes("network") ||
    msg.includes("timeout") ||
    msg.includes("超时") ||
    msg.includes("bad gateway") ||
    msg.includes("service unavailable") ||
    msg.includes("gateway timeout") ||
    /\b50[234]\b/.test(msg)
  );
}

async function workbenchRequest<T>(
  path: string,
  options?: RequestInit,
  retries = 1,
): Promise<T> {
  const callerSignal = options?.signal;
  if (callerSignal?.aborted) {
    throw new DOMException("Aborted", "AbortError");
  }

  const attemptCtrl = new AbortController();
  let timedOut = false;
  const onCallerAbort = () => attemptCtrl.abort();
  callerSignal?.addEventListener("abort", onCallerAbort, { once: true });
  const timer = globalThis.setTimeout(() => {
    timedOut = true;
    attemptCtrl.abort();
  }, WORKBENCH_TIMEOUT_MS);

  try {
    return await request<T>(path, { ...options, signal: attemptCtrl.signal });
  } catch (err) {
    if (callerSignal?.aborted) {
      throw err;
    }
    const normalizedErr =
      timedOut || isAbortError(err)
        ? new Error("请求超时，请检查网络或稍后重试")
        : err;
    if (retries > 0 && shouldRetryWorkbenchRequest(normalizedErr)) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      return workbenchRequest<T>(path, options, retries - 1);
    }
    throw normalizedErr;
  } finally {
    globalThis.clearTimeout(timer);
    callerSignal?.removeEventListener("abort", onCallerAbort);
  }
}

export interface WorkbenchRoleMeta {
  key: string;
  label: string;
  workspace_title: string;
  target_type: string;
  goal_tagline: string;
}

export interface WorkbenchTaskItem {
  id: number;
  role_key: string;
  module_key: string;
  module_title: string;
  target_type: string;
  target_name: string;
  title: string;
  due_label: string;
  status: "pending" | "in_progress" | "pending_delivery" | "done" | string;
  progress: number;
  source: string;
  source_label?: string;
  readonly?: boolean;
  external_ref: string;
  meta?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface WorkbenchModuleGroup {
  module_key: string;
  module_title: string;
  target_type: string;
  task_count: number;
  items: WorkbenchTaskItem[];
}

export interface WorkbenchProgressResponse {
  role: WorkbenchRoleMeta;
  roles: WorkbenchRoleMeta[];
  range: string;
  mcn_connected: boolean;
  mcn_error?: string;
  data_sources?: string[];
  kpis: {
    core_work: number;
    in_progress: number;
    delivered_this_week: number;
    overall_completion: number;
  };
  modules: WorkbenchModuleGroup[];
}

export type DeliverableType = "document" | "link" | "sheet" | "ppt";
export type DeliverableReviewStatus =
  | "pending_review"
  | "approved"
  | "needs_revision";

export interface WorkbenchDeliverableItem {
  id: number;
  role_key: string;
  deliverable_type: DeliverableType;
  target_name: string;
  title: string;
  summary: string;
  content: string;
  review_status: DeliverableReviewStatus;
  source_thread_id: string;
  source_message_id: string;
  source?: string;
  source_label?: string;
  readonly?: boolean;
  external_url?: string;
  linked_task_id: number | null;
  delivered_at: number;
  delivered_label: string;
  created_at: number;
  updated_at: number;
}

export interface WorkbenchDeliverablesResponse {
  kpis: {
    total: number;
    pending_review: number;
    approved: number;
    added_this_week: number;
  };
  items: WorkbenchDeliverableItem[];
  mcn_connected?: boolean;
  mcn_error?: string;
}

export interface WorkbenchDashboardResponse {
  role: {
    key: string;
    label: string;
    workspace_title: string;
    badge: string;
  };
  roles: WorkbenchRoleMeta[];
  range: string;
  mcn_connected: boolean;
  mcn_error?: string;
  data_sources?: string[];
  trend_metric_label: string;
  kpis: Array<{ label: string; value: string; delta: string }>;
  trend: Array<{ label: string; value: number }>;
  goals: Array<{
    id: number;
    role_key: string;
    title: string;
    target_value: number;
    current_value: number;
    unit: string;
    period: string;
    percent: number;
    updated_at: number;
  }>;
}

function progressPath(role = "customer_service", range = "week"): string {
  return `/workbench/progress?role=${encodeURIComponent(role)}&range=${encodeURIComponent(range)}`;
}

function deliverablesPath(params?: {
  type?: string;
  q?: string;
  review_status?: string;
}): string {
  const search = new URLSearchParams();
  if (params?.type) search.set("type", params.type);
  if (params?.q) search.set("q", params.q);
  if (params?.review_status) search.set("review_status", params.review_status);
  const qs = search.toString();
  return `/workbench/deliverables${qs ? `?${qs}` : ""}`;
}

function dashboardPath(role = "business", range = "7d"): string {
  return `/workbench/dashboard?role=${encodeURIComponent(role)}&range=${encodeURIComponent(range)}`;
}

export const workbenchApi = {
  getCachedProgress: (role = "customer_service", range = "week") =>
    readCached<WorkbenchProgressResponse>(progressPath(role, range)),

  getProgress: async (
    role = "customer_service",
    range = "week",
    options?: { signal?: AbortSignal },
  ) => {
    const path = progressPath(role, range);
    const res = await workbenchRequest<WorkbenchProgressResponse>(path, options);
    writeCached(path, res);
    return res;
  },

  createTask: async (body: {
    role_key: string;
    module_key?: string;
    module_title?: string;
    target_type?: string;
    target_name: string;
    title: string;
    due_label?: string;
    status?: string;
    progress?: number;
    source?: string;
    external_ref?: string;
  }) => {
    const res = await workbenchRequest<WorkbenchTaskItem>("/workbench/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    });
    invalidateWorkbenchCache();
    return res;
  },

  updateTask: async (
    taskId: number,
    body: Partial<{
      title: string;
      target_name: string;
      due_label: string;
      status: string;
      progress: number;
    }>,
  ) => {
    const res = await workbenchRequest<WorkbenchTaskItem>(
      `/workbench/tasks/${taskId}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    );
    invalidateWorkbenchCache();
    return res;
  },

  deleteTask: async (taskId: number) => {
    const res = await workbenchRequest<{ ok: boolean }>(
      `/workbench/tasks/${taskId}`,
      {
        method: "DELETE",
      },
    );
    invalidateWorkbenchCache();
    return res;
  },

  getCachedDeliverables: (params?: {
    type?: string;
    q?: string;
    review_status?: string;
  }) => readCached<WorkbenchDeliverablesResponse>(deliverablesPath(params)),

  listDeliverables: async (
    params?: {
      type?: string;
      q?: string;
      review_status?: string;
    },
    options?: { signal?: AbortSignal },
  ) => {
    const path = deliverablesPath(params);
    const res = await workbenchRequest<WorkbenchDeliverablesResponse>(
      path,
      options,
    );
    writeCached(path, res);
    return res;
  },

  createDeliverable: async (body: {
    role_key?: string;
    deliverable_type: DeliverableType;
    target_name: string;
    title: string;
    summary?: string;
    content: string;
    review_status: DeliverableReviewStatus;
    source_thread_id?: string;
    source_message_id?: string;
  }) => {
    const res = await workbenchRequest<{
      deliverable: WorkbenchDeliverableItem;
      progress_synced: boolean;
      updated_task: WorkbenchTaskItem | null;
      updated?: boolean;
    }>("/workbench/deliverables", {
      method: "POST",
      body: JSON.stringify(body),
    });
    invalidateWorkbenchCache();
    return res;
  },

  updateDeliverable: async (
    deliverableId: number,
    body: Partial<{
      deliverable_type: DeliverableType;
      target_name: string;
      title: string;
      summary: string;
      content: string;
      review_status: DeliverableReviewStatus;
    }>,
  ) => {
    const res = await workbenchRequest<WorkbenchDeliverableItem>(
      `/workbench/deliverables/${deliverableId}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    );
    invalidateWorkbenchCache();
    return res;
  },

  deleteDeliverable: async (deliverableId: number) => {
    const res = await workbenchRequest<{ ok: boolean }>(
      `/workbench/deliverables/${deliverableId}`,
      {
        method: "DELETE",
      },
    );
    invalidateWorkbenchCache();
    return res;
  },

  getCachedDashboard: (role = "business", range = "7d") =>
    readCached<WorkbenchDashboardResponse>(dashboardPath(role, range)),

  getDashboard: async (
    role = "business",
    range = "7d",
    options?: { signal?: AbortSignal },
  ) => {
    const path = dashboardPath(role, range);
    const res = await workbenchRequest<WorkbenchDashboardResponse>(
      path,
      options,
    );
    writeCached(path, res);
    return res;
  },

  updateGoal: async (
    goalId: number,
    body: { current_value?: number; target_value?: number },
  ) => {
    const res = await workbenchRequest<WorkbenchDashboardResponse["goals"][number]>(
      `/workbench/goals/${goalId}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    );
    invalidateWorkbenchCache();
    return res;
  },

  syncMcn: async (roleKey = "customer_service") => {
    const res = await workbenchRequest<{
      synced: number;
      mcn_connected: boolean;
      detail: string;
    }>("/workbench/sync-mcn", {
      method: "POST",
      body: JSON.stringify({ role_key: roleKey }),
    });
    invalidateWorkbenchCache();
    return res;
  },
};
