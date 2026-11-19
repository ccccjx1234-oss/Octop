import { getApiUrl } from "../config";
import { request } from "../request";

export type ScopeKind = "wiki_space" | "wiki_node" | "drive_folder";
export type SyncSchedule = "manual" | "daily" | "interval";
export interface SharedFeishuScope {
  connection_id: string;
  kind: ScopeKind;
  root: string;
  title: string;
  space: string | null;
  include_children: boolean;
}
export interface SharedFeishuSource {
  id: string;
  connection_id: string;
  connection_label: string;
  title: string;
  scope_kind: ScopeKind;
  space_id: string;
  root_token: string;
  include_children: number;
  schedule: SyncSchedule;
  interval_minutes?: number | null;
  auto_add: number;
  active: number;
  status: "queued" | "syncing" | "synced" | "paused" | "error" | "disconnected";
  last_sync: number | null;
  last_error: string | null;
  pending_count: number;
  skipped_count: number;
  documents: number;
}
export interface SharedFeishuConnection {
  id: string;
  label: string;
  provider_id: number;
  tenant: string;
  connected: boolean;
}
export interface SharedFeishuReview {
  documents: { id: string; title: string; path: string[]; is_new?: boolean }[];
  skipped: number;
  existing_source?: boolean;
  new_count?: number;
  visibility: {
    visibility: "owner" | "groups" | "all";
    group_ids: string[];
    group_names: string[];
  };
  fingerprint: string;
}
export interface DirectoryEntry {
  id: string;
  title: string;
  type: string;
}

const base = "/feishu-kb/shared";
const basePath = (kbId: string) => `${base}/bases/${encodeURIComponent(kbId)}`;
const post = (body: unknown) => ({
  method: "POST",
  body: JSON.stringify(body),
});

export const sharedFeishuApi = {
  connections: () =>
    request<{
      connections: SharedFeishuConnection[];
      providers: { id: number; name: string }[];
    }>(`${base}/connections`),
  authorize: (provider_id: number, label: string) =>
    request<{ url: string }>(
      `${base}/connections/authorize`,
      post({ provider_id, label }),
    ),
  callback: async (session: string, state: string, code: string) => {
    const response = await fetch(getApiUrl(`${base}/connections/callback`), {
      ...post({ state, code }),
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session}`,
      },
    });
    if (!response.ok) throw new Error("Feishu connection authorization failed");
  },
  disconnect: (id: string) =>
    request(`${base}/connections/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  browse: (id: string, params: Record<string, string>) =>
    request<{ items: DirectoryEntry[]; next_page: string | null }>(
      `${base}/directories/${encodeURIComponent(id)}?${new URLSearchParams(
        params,
      )}`,
    ),
  list: (kbId: string) =>
    request<SharedFeishuSource[]>(`${basePath(kbId)}/sources`),
  preview: (kbId: string, scope: SharedFeishuScope) =>
    request<SharedFeishuReview>(`${basePath(kbId)}/preview`, post(scope)),
  approve: (
    kbId: string,
    scope: SharedFeishuScope,
    fingerprint: string,
    schedule: SyncSchedule,
    auto_add: boolean,
    interval_minutes?: number | null,
  ) =>
    request<{ id: string }>(
      `${basePath(kbId)}/sources`,
      post({
        ...scope,
        fingerprint,
        schedule,
        ...(schedule === "interval" && interval_minutes !== undefined
          ? { interval_minutes }
          : {}),
        auto_add,
        confirmed: true,
      }),
    ),
  setActive: (kbId: string, id: string, active: boolean) =>
    request(`${basePath(kbId)}/sources/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ active }),
    }),
};
