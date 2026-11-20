import { afterEach, expect, it, vi } from "vitest";
import { feishuKbApi } from "./feishuKb";

vi.mock("../config", () => ({ getApiUrl: (path: string) => `/api${path}` }));

afterEach(() => vi.unstubAllGlobals());

it("posts the original bearer once and does not retry a failed callback", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false });
  vi.stubGlobal("fetch", fetchMock);
  await expect(
    feishuKbApi.callback("original", "state", "code"),
  ).rejects.toThrow("callback failed");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith("/api/feishu-kb/callback", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer original",
    },
    body: JSON.stringify({ state: "state", code: "code" }),
  });
});
