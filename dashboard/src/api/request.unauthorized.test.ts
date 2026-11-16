import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("../i18n", () => ({
  default: { language: "zh" },
}));

function unauthorizedResponse(): Response {
  return new Response("{}", {
    status: 401,
    headers: { "content-type": "application/json" },
  });
}

describe("401 handling", () => {
  const replace = vi.fn();
  let mod: typeof import("./request");

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    sessionStorage.clear();
    replace.mockClear();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/chat", replace },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(unauthorizedResponse())),
    );
    mod = await import("./request");
    mod.setAuthToken("expired-token");
  });

  it("lets an in-app listener take over the redirect instead of reloading", async () => {
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(mod.UNAUTHORIZED_EVENT, listener);

    await expect(mod.request("/agents")).rejects.toThrow();

    expect(listener).toHaveBeenCalledOnce();
    expect(replace).not.toHaveBeenCalled();
    expect(mod.getAuthToken()).toBe("");

    window.removeEventListener(mod.UNAUTHORIZED_EVENT, listener);
  });

  it("falls back to a full-page navigation when nothing handles the event", async () => {
    await expect(mod.request("/agents")).rejects.toThrow();

    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("keeps the local session when a 401 is a peer credential error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "BRIDGE_AUTH_FAILED",
                message: "peer rejected credentials",
                details: {},
              },
            }),
            {
              status: 401,
              headers: { "content-type": "application/json" },
            },
          ),
        ),
      ),
    );

    await expect(
      mod.request("/bridge/probe", { method: "POST" }),
    ).rejects.toThrow(/BRIDGE_AUTH_FAILED/);

    expect(replace).not.toHaveBeenCalled();
    expect(mod.getAuthToken()).toBe("expired-token");
  });

  it("redirects only once when several requests fail concurrently", async () => {
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(mod.UNAUTHORIZED_EVENT, listener);

    await Promise.allSettled([
      mod.request("/agents"),
      mod.request("/skills"),
      mod.request("/cron"),
    ]);

    expect(listener).toHaveBeenCalledOnce();

    window.removeEventListener(mod.UNAUTHORIZED_EVENT, listener);
  });

  it("resets redirect guard after setAuthToken and preserves kick reason", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "AUTH_FAILED",
                message: "认证失败。",
                details: { reason: "session_replaced" },
              },
            }),
            {
              status: 401,
              headers: { "content-type": "application/json" },
            },
          ),
        ),
      ),
    );

    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(mod.UNAUTHORIZED_EVENT, listener);

    await expect(mod.request("/agents")).rejects.toThrow();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(mod.consumeAuthNotice()).toEqual({
      reason: "session_replaced",
      message: "认证失败。",
    });

    // Re-login via SPA without full reload must allow a future 401 redirect.
    mod.setAuthToken("fresh-token");
    await expect(mod.request("/agents")).rejects.toThrow();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(mod.consumeAuthNotice()?.reason).toBe("session_replaced");

    expect(
      mod.parseWsAuthCloseNotice({
        code: 4001,
        reason: "auth:session_replaced",
      }),
    ).toEqual({ reason: "session_replaced" });

    window.removeEventListener(mod.UNAUTHORIZED_EVENT, listener);
  });
});
