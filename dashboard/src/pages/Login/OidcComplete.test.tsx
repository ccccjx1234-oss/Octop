import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  exchangeOidcCode: vi.fn(),
  setAuthToken: vi.fn(),
  notifySsoOpener: vi.fn(),
}));

vi.mock("@/utils/antdMessage", () => ({
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));
vi.mock("../../api", () => ({
  getRememberLoginPreference: () => true,
  isSessionOnlyAuth: () => false,
  setAuthToken: mocks.setAuthToken,
}));
vi.mock("../../api/modules/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/modules/auth")>();
  return {
    ...actual,
    authApi: {
      ...actual.authApi,
      exchangeOidcCode: mocks.exchangeOidcCode,
    },
  };
});
vi.mock("../../utils/locale", () => ({
  applyUserLocale: () => Promise.resolve("zh"),
}));
vi.mock("../../i18n", () => ({ refreshServerLabels: vi.fn() }));
vi.mock("../../utils/ssoPopup", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../utils/ssoPopup")>();
  return {
    ...actual,
    notifySsoOpener: mocks.notifySsoOpener,
  };
});

import OidcComplete, {
  readOidcCompleteParams,
  safeRedirect,
} from "./OidcComplete";
import { loadPendingErpBind } from "./pendingErpBindStorage";

describe("safeRedirect", () => {
  it.each([
    ["//identity.example.com", "/chat"],
    ["http://identity.example.com", "/chat"],
    ["/chat://identity.example.com", "/chat"],
    ["/chat\\identity.example.com", "/chat"],
    ["chat", "/chat"],
    ["/agents", "/agents"],
  ])("allows only internal paths: %s", (redirect, expected) => {
    expect(safeRedirect(redirect)).toBe(expected);
  });
});

describe("readOidcCompleteParams", () => {
  it("prefers hash over query", () => {
    expect(
      readOidcCompleteParams(
        "#code=from-hash&redirect=%2Fsettings",
        "?code=from-query",
      ),
    ).toEqual({ code: "from-hash", redirect: "/settings", bind: false });
  });

  it("falls back to query for legacy links", () => {
    expect(readOidcCompleteParams("", "?code=legacy&redirect=%2Fchat")).toEqual(
      {
        code: "legacy",
        redirect: "/chat",
        bind: false,
      },
    );
  });
});

describe("OidcComplete ERP bind challenge", () => {
  const challenge = {
    erp_bind_required: true as const,
    bind_ticket: "ticket-123",
    reason: "erp_not_found",
    provider: "feishu",
    provider_name: "飞书",
    display_name: "Alice",
    mobile_masked: "138****1234",
    expires_in: 600,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    mocks.exchangeOidcCode.mockResolvedValue(challenge);
  });

  it("notifies the opener in popup mode and does not store an auth token", async () => {
    mocks.notifySsoOpener.mockReturnValue(true);
    window.history.replaceState(
      null,
      "",
      "/login/oidc/complete#code=popup-code&redirect=%2Fchat",
    );

    render(
      <MemoryRouter initialEntries={["/login/oidc/complete"]}>
        <Routes>
          <Route path="/login/oidc/complete" element={<OidcComplete />} />
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(mocks.notifySsoOpener).toHaveBeenCalledWith({
        ok: false,
        erp_bind: challenge,
      }),
    );
    expect(mocks.setAuthToken).not.toHaveBeenCalled();
  });

  it("saves pending bind and redirects to /login in full-page mode", async () => {
    mocks.notifySsoOpener.mockReturnValue(false);
    window.history.replaceState(
      null,
      "",
      "/login/oidc/complete#code=fullpage-code&redirect=%2Fchat",
    );

    render(
      <MemoryRouter initialEntries={["/login/oidc/complete"]}>
        <Routes>
          <Route path="/login/oidc/complete" element={<OidcComplete />} />
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("login page")).toBeInTheDocument(),
    );
    expect(loadPendingErpBind()).toEqual(challenge);
    expect(mocks.setAuthToken).not.toHaveBeenCalled();
  });
});
