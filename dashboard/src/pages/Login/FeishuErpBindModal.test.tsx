import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  setToken: vi.fn(),
  clearToken: vi.fn(),
  staffLogin: vi.fn(),
  msgError: vi.fn(),
  msgSuccess: vi.fn(),
}));

vi.mock("../../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));
vi.mock("../../api", () => ({
  clearAuthToken: mocks.clearToken,
  setAuthToken: mocks.setToken,
  setRememberLoginPreference: vi.fn(),
}));
vi.mock("../../api/modules/auth", () => ({
  isMfaChallenge: () => false,
  authApi: {
    getAuthStatus: () => Promise.resolve({ setup_required: false }),
    getOauthStatus: () =>
      Promise.resolve({
        providers: [{ kind: "feishu", enabled: true, display_name: "飞书" }],
      }),
    getYouliStatus: () => Promise.resolve({ enabled: true }),
    staffLogin: mocks.staffLogin,
  },
}));
vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve("zh"),
}));
vi.mock("../../i18n", () => ({ refreshServerLabels: vi.fn() }));
vi.mock("./CaptchaField", () => ({ default: () => null }));
vi.mock("@/utils/antdMessage", () => ({
  message: {
    error: mocks.msgError,
    success: mocks.msgSuccess,
    warning: vi.fn(),
  },
}));

import LoginPage from "./index";
import {
  PENDING_ERP_BIND_STORAGE_KEY,
  savePendingErpBind,
} from "./pendingErpBindStorage";
import type { ErpBindChallengeResponse } from "../../api/modules/auth";
import { SSO_MESSAGE_TYPE } from "../../utils/ssoPopup";

const CHALLENGE: ErpBindChallengeResponse = {
  erp_bind_required: true,
  bind_ticket: "ticket-abc",
  reason: "erp_not_found",
  provider: "feishu",
  provider_name: "飞书",
  display_name: "张三",
  mobile_masked: "139****2222",
  expires_in: 600,
};

function renderStaffLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage mode="staff" />} />
        <Route path="/chat" element={<div>chat workspace</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("FeishuErpBindModal in LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("opens on popup erp_bind postMessage without firing generic error toast", async () => {
    renderStaffLogin();

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: window.location.origin,
          data: {
            type: SSO_MESSAGE_TYPE,
            ok: false,
            erp_bind: CHALLENGE,
          },
        }),
      );
    });

    await waitFor(() =>
      expect(screen.getByTestId("feishu-erp-bind-modal")).toBeInTheDocument(),
    );
    expect(mocks.msgError).not.toHaveBeenCalled();
    expect(screen.getByText("张三")).toBeInTheDocument();
  });

  it("restores from sessionStorage, allows retry on wrong password, then binds and enters /chat", async () => {
    const user = userEvent.setup();
    savePendingErpBind(CHALLENGE);

    mocks.staffLogin
      .mockRejectedValueOnce(new Error('401 - {"error":{"code":"AUTH_FAILED","message":"手机号或密码错误"}}'))
      .mockResolvedValueOnce({
        access_token: "erp-bound-token",
        user: { locale: "zh" },
      });

    renderStaffLogin();

    const modal = await screen.findByTestId("feishu-erp-bind-modal");
    expect(modal).toBeInTheDocument();

    const mobileInput = screen.getByLabelText(/login\.feishuErpBind\.mobileLabel/);
    const passwordInput = screen.getByLabelText(
      /login\.feishuErpBind\.passwordLabel/,
    );
    const submitBtn = screen.getByRole("button", {
      name: /login\.feishuErpBind\.submitButton/,
    });

    await user.type(mobileInput, "13800001234");
    await user.type(passwordInput, "wrong-pass");
    await user.click(submitBtn);

    await waitFor(() =>
      expect(screen.getByText("手机号或密码错误")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("feishu-erp-bind-modal")).toBeInTheDocument();
    expect(mocks.setToken).not.toHaveBeenCalled();

    await user.clear(passwordInput);
    await user.type(passwordInput, "right-pass");
    await user.click(submitBtn);

    await waitFor(() =>
      expect(screen.getByText("chat workspace")).toBeInTheDocument(),
    );
    expect(mocks.staffLogin).toHaveBeenNthCalledWith(
      2,
      "13800001234",
      "right-pass",
      "ticket-abc",
    );
    expect(mocks.setToken).toHaveBeenCalledWith("erp-bound-token", true);
    expect(sessionStorage.getItem(PENDING_ERP_BIND_STORAGE_KEY)).toBeNull();
  });

  it("stays unauthenticated on /login when user cancels binding", async () => {
    const user = userEvent.setup();
    savePendingErpBind(CHALLENGE);

    renderStaffLogin();

    await screen.findByTestId("feishu-erp-bind-modal");
    await user.click(
      screen.getByRole("button", { name: /login\.feishuErpBind\.cancelButton/ }),
    );

    await waitFor(() =>
      expect(
        screen.queryByTestId("feishu-erp-bind-modal"),
      ).not.toBeInTheDocument(),
    );
    expect(mocks.setToken).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(PENDING_ERP_BIND_STORAGE_KEY)).toBeNull();
    expect(screen.queryByText("chat workspace")).not.toBeInTheDocument();
  });
});
