import { request } from "../request";
import { getApiUrl } from "../config";

export interface FeishuIdentity {
  provider_id: number;
  provider_name: string;
  tenant: string;
  identity: string;
  consent: "authorized" | "required";
  requested_scopes: string[];
}
export interface FeishuSource {
  source_id: string;
  provider_id: number;
  document_id: string;
  enabled: boolean;
  retrieval_enabled: boolean;
  interval_seconds: number;
  next_sync: number;
  status: string;
  has_staged_content?: boolean;
}
const base = "/feishu-kb";
export const feishuKbApi = {
  status: () =>
    request<{ identities: FeishuIdentity[]; scheduler_configured: boolean }>(
      `${base}/status`,
    ),
  sources: () => request<FeishuSource[]>(`${base}/sources`),
  authorize: (provider_id: number) =>
    request<{ url: string }>(`${base}/authorize`, {
      method: "POST",
      body: JSON.stringify({ provider_id }),
    }),
  // This request deliberately uses the captured bearer; it must never retry with a renewed session.
  callback: async (session: string, state: string, code: string) => {
    const response = await fetch(getApiUrl(`${base}/callback`), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session}`,
      },
      body: JSON.stringify({ state, code }),
    });
    if (!response.ok) throw new Error("callback failed");
  },
  create: (provider_id: number, document_id: string) =>
    request(`${base}/sources`, {
      method: "POST",
      body: JSON.stringify({ provider_id, document_id }),
    }),
  configure: (
    id: string,
    enabled: boolean,
    interval_seconds: number,
    retrieval_enabled?: boolean,
  ) =>
    request(`${base}/sources/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ enabled, interval_seconds, retrieval_enabled }),
    }),
  sync: (id: string) =>
    request<{ status: string }>(`${base}/sources/${encodeURIComponent(id)}/sync`, {
      method: "POST",
    }),
  content: (id: string) =>
    request<{ content: string }>(
      `${base}/sources/${encodeURIComponent(id)}/content`,
    ),
  remove: (id: string) =>
    request(`${base}/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),
  revoke: (id: number) => request(`${base}/grants/${id}`, { method: "DELETE" }),
};
