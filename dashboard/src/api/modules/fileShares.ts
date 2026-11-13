import { request, requestBlob } from "../request";
import { routerBasename } from "../../utils/publicUrl";

export interface FileShareInfo {
  filename: string;
  size: number;
  can_revoke: boolean;
}

export const fileSharesApi = {
  capabilities: () =>
    request<{ enabled: boolean }>("/file-shares/capabilities"),
  create: (agentId: string, path: string, usernames: string[]) =>
    request<{ share_id: string }>(`/agents/${agentId}/file-shares`, {
      method: "POST",
      body: JSON.stringify({ path, recipient_usernames: usernames }),
    }),
  info: (id: string) => request<FileShareInfo>(`/file-shares/${id}`),
  download: (id: string) => requestBlob(`/file-shares/${id}/download`),
  revoke: (id: string) =>
    request<void>(`/file-shares/${id}`, { method: "DELETE" }),
  link: (id: string) =>
    new URL(`${routerBasename()}/file-shares/${id}`, window.location.origin)
      .href,
};
