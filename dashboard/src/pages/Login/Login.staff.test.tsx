import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  captcha: vi.fn(),
  passkeyStatus: vi.fn(),
  setToken: vi.fn(),
  youliStatus: vi.fn().mockResolvedValue({ enabled: true }),
  staffLogin: vi.fn().mockResolvedValue({
    access_token: "employee-token",
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
    getYouliStatus: mocks.youliStatus,
    staffLogin: mocks.staffLogin,
  },
}));
vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve("zh"),
}));
vi.mock("../../i18n", () => ({ refreshServerLabels: vi.fn() }));
vi.mock("./CaptchaField", () => ({ default: () => null }));

import LoginPage from "./index";

it("keeps employee login independent of local captcha and passkey login", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage mode="staff" />} />
        <Route path="/chat" element={<div>employee chat</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await user.type(screen.getByRole("textbox"), "13800000000");
  await user.type(
    screen.getByPlaceholderText(/login\.password|密码|password/i),
    "employee-password",
  );
  await user.click(
    screen.getByRole("button", { name: /login\.submit|登\s*录|sign in/i }),
  );
  await waitFor(() =>
    expect(screen.getByText("employee chat")).toBeInTheDocument(),
  );
  expect(mocks.staffLogin).toHaveBeenCalledWith(
    "13800000000",
    "employee-password",
  );
  expect(mocks.setToken).toHaveBeenCalledWith("employee-token", true);
  expect(mocks.captcha).not.toHaveBeenCalled();
  expect(mocks.passkeyStatus).not.toHaveBeenCalled();
});

it("redirects to the system-account form when Youli is not configured", async () => {
  mocks.youliStatus.mockResolvedValueOnce({ enabled: false });
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage mode="staff" />} />
        <Route path="/manageLogin" element={<div>system login</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(screen.getByText("system login")).toBeInTheDocument(),
  );
});
