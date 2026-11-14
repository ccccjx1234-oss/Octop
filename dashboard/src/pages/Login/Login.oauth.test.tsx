import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const {
  getAuthStatus,
  getYouliStatus,
  getOauthStatus,
  getLdapStatus,
  getPasskeyLoginStatus,
  getCaptcha,
  startOauth,
} = vi.hoisted(() => ({
  getAuthStatus: vi.fn(),
  getYouliStatus: vi.fn(),
  getOauthStatus: vi.fn(),
  getLdapStatus: vi.fn(),
  getPasskeyLoginStatus: vi.fn(),
  getCaptcha: vi.fn(),
  startOauth: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      typeof opts?.name === "string" ? `${key}:${opts.name}` : key,
    i18n: { language: "zh" },
  }),
  Trans: ({ children }: { children: unknown }) => children,
}));

vi.mock("../../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../api", () => ({
  clearAuthToken: vi.fn(),
  setAuthToken: vi.fn(),
  setRememberLoginPreference: vi.fn(),
}));

vi.mock("../../api/modules/auth", () => ({
  isMfaChallenge: () => false,
  authApi: {
    getAuthStatus,
    getYouliStatus,
    getOauthStatus,
    getLdapStatus,
    getPasskeyLoginStatus,
    getCaptcha,
    startOauth,
  },
}));

vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve("zh"),
}));

vi.mock("../../i18n", () => ({ refreshServerLabels: vi.fn() }));
vi.mock("./CaptchaField", () => ({ default: () => null }));

vi.mock("@/utils/antdMessage", () => ({
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

import LoginPage from "./index";

describe("<LoginPage /> Feishu and DingTalk OAuth", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAuthStatus.mockResolvedValue({
      setup_required: false,
      local_account: false,
    });
    getYouliStatus.mockResolvedValue({
      enabled: true,
      base_url: "http://mcn.local",
    });
    getOauthStatus.mockResolvedValue({
      providers: [
        { kind: "feishu", enabled: true, display_name: "飞书" },
        { kind: "dingtalk", enabled: true, display_name: "钉钉" },
        { kind: "wecom", enabled: false, display_name: "企业微信" },
      ],
    });
    getLdapStatus.mockResolvedValue({ enabled: false, display_name: "LDAP" });
    getPasskeyLoginStatus.mockResolvedValue({ passwordless_login: false });
    getCaptcha.mockResolvedValue({ provider: "slider" });
  });

  it("renders enabled Feishu and DingTalk buttons on staff /login and falls back cleanly when popups are blocked", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    startOauth.mockResolvedValue({
      authorization_url: "https://accounts.feishu.cn/mock-auth",
      state: "s1",
    });
    const originalLocation = window.location;
    let redirectedTo = "";
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...originalLocation,
        get href() {
          return redirectedTo || originalLocation.href;
        },
        set href(val: string) {
          redirectedTo = val;
        },
      },
    });

    try {
      render(
        <MemoryRouter initialEntries={["/login"]}>
          <LoginPage mode="staff" />
        </MemoryRouter>,
      );

      const feishuBtn = await screen.findByRole("button", { name: /飞书/ });
      const dingBtn = await screen.findByRole("button", { name: /钉钉/ });
      expect(feishuBtn).toBeInTheDocument();
      expect(dingBtn).toBeInTheDocument();

      await user.click(feishuBtn);
      await waitFor(() =>
        expect(startOauth).toHaveBeenCalledWith("feishu", "/chat"),
      );
      expect(redirectedTo).toBe("https://accounts.feishu.cn/mock-auth");
    } finally {
      openSpy.mockRestore();
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
    }
  });

  it("renders Feishu and DingTalk buttons on /manageLogin and starts DingTalk OAuth", async () => {
    const user = userEvent.setup();
    const fakePopup = {
      closed: false,
      location: { href: "" },
      close: vi.fn(),
    } as unknown as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(fakePopup);
    startOauth.mockResolvedValue({
      authorization_url: "https://login.dingtalk.com/mock-auth",
      state: "s2",
    });

    try {
      render(
        <MemoryRouter initialEntries={["/manageLogin"]}>
          <LoginPage />
        </MemoryRouter>,
      );

      const dingBtn = await screen.findByRole("button", { name: /钉钉/ });
      await user.click(dingBtn);
      await waitFor(() =>
        expect(startOauth).toHaveBeenCalledWith("dingtalk", "/chat"),
      );
      expect(fakePopup.location.href).toBe(
        "https://login.dingtalk.com/mock-auth",
      );
    } finally {
      openSpy.mockRestore();
    }
  });
});
