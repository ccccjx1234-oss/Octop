import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  captcha: vi.fn().mockResolvedValue({ provider: "none" }),
  passkeyStatus: vi.fn().mockResolvedValue({ passwordless_login: false }),
  oauthStatus: vi.fn().mockResolvedValue({ providers: [] }),
  ldapStatus: vi.fn().mockResolvedValue({ enabled: false, display_name: "" }),
  setToken: vi.fn(),
  login: vi.fn().mockResolvedValue({
    access_token: "default-token",
    user: { locale: "zh" },
  }),
}));

vi.mock("../../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));
vi.mock("../../api", () => ({
  clearAuthToken: vi.fn(),
  setAuthToken: mocks.setToken,
  setRememberLoginPreference: vi.fn(),
}));
vi.mock("../../api/modules/auth", () => ({
  isMfaChallenge: () => false,
  authApi: {
    getAuthStatus: () => Promise.resolve({ setup_required: false }),
    getCaptcha: mocks.captcha,
    getPasskeyLoginStatus: mocks.passkeyStatus,
    getOauthStatus: mocks.oauthStatus,
    getLdapStatus: mocks.ldapStatus,
    login: mocks.login,
  },
}));
vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve("zh"),
}));
vi.mock("../../i18n", () => ({ refreshServerLabels: vi.fn() }));
vi.mock("./CaptchaField", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef(function MockCaptchaField(
      { onReadyChange }: { onReadyChange: (ready: boolean) => void },
      ref,
    ) {
      React.useImperativeHandle(ref, () => ({
        getToken: async () => undefined,
      }));
      React.useEffect(() => {
        onReadyChange(true);
      }, [onReadyChange]);
      return null;
    }),
  };
});

import LoginPage from "./index";

it("uses the unified default login flow on /login", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/chat" element={<div>workspace chat</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await user.type(screen.getByRole("textbox"), "admin");
  await user.type(
    screen.getByPlaceholderText(/login\.password|密码|password/i),
    "secret-password",
  );
  await user.click(
    screen.getByRole("button", { name: /login\.submit|登\s*录|sign in/i }),
  );
  await waitFor(() =>
    expect(screen.getByText("workspace chat")).toBeInTheDocument(),
  );
  expect(mocks.login).toHaveBeenCalledWith("admin", "secret-password", undefined);
  expect(mocks.setToken).toHaveBeenCalledWith("default-token", true);
  expect(mocks.captcha).toHaveBeenCalled();
  expect(mocks.passkeyStatus).toHaveBeenCalled();
});

it("shows a kick reason banner when redirected after another device signed in", async () => {
  sessionStorage.setItem(
    "octop:auth_notice",
    JSON.stringify({ reason: "session_replaced" }),
  );
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(
      screen.getByText(/login\.sessionReplaced|其他设备或窗口登录/),
    ).toBeInTheDocument(),
  );
});
