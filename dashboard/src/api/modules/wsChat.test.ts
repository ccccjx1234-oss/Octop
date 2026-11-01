import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildDashboardChatWsUrl } from "./wsChat";

vi.mock("../request", () => ({ getAuthToken: () => "tok" }));

describe("buildDashboardChatWsUrl", () => {
  beforeEach(() => {
    vi.stubGlobal("BASE_URL", "");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("keeps the configured subpath", () => {
    vi.stubEnv("BASE_URL", "/octop/");

    expect(buildDashboardChatWsUrl("agent/1")).toBe(
      `ws://${window.location.host}/octop/api/agents/agent%2F1/chat/ws?token=tok`,
    );
  });

  it("falls back to the root api", () => {
    vi.stubEnv("BASE_URL", "/");

    expect(buildDashboardChatWsUrl("a")).toBe(
      `ws://${window.location.host}/api/agents/a/chat/ws?token=tok`,
    );
  });
});
