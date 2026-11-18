import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { YouliBindingCard } from "./AccountSecurityDrawer";

const mocks = vi.hoisted(() => ({
  start: vi.fn(),
  binding: vi.fn(),
  token: vi.fn(),
  error: vi.fn(),
}));
vi.mock("../api/modules/auth", () => ({
  authApi: { startYouliReauth: mocks.start, youliBinding: mocks.binding },
}));
vi.mock("../api/request", () => ({ getAuthToken: mocks.token }));
vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
vi.mock("@/utils/antdMessage", () => ({
  message: { error: mocks.error, success: vi.fn() },
}));

const url = "https://accounts.feishu.cn/oauth/v3/authorize?state=test";
function deferred() {
  let resolve!: (value: { authorization_url: string }) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<{ authorization_url: string }>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function popup() {
  return { closed: false, location: { href: "" }, close: vi.fn() };
}
function sendProof(source: ReturnType<typeof popup>) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: window.location.origin,
        source: source as unknown as Window,
        data: { type: "octop-youli-reauth", proof: "proof" },
      }),
    );
  });
}
async function setup() {
  render(<YouliBindingCard providerId={1} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "account.youliVerify" })).toBeEnabled());
  vi.useFakeTimers();
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn().mockReturnValue(false),
  }));
  mocks.binding.mockResolvedValue({ bound: false, delegation_active: false });
  mocks.token.mockReturnValue("session-1");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ERP binding verification", () => {
  it("starts only once on a synchronous double click", async () => {
    const request = deferred();
    mocks.start.mockReturnValue(request.promise);
    const open = vi.spyOn(window, "open").mockReturnValue(popup() as unknown as Window);
    await setup();
    const button = screen.getByRole("button", { name: "account.youliVerify" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(open).toHaveBeenCalledTimes(1);
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it("does not let a cancelled request clear a later popup", async () => {
    const first = deferred();
    const second = deferred();
    mocks.start.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const oldPopup = popup();
    const newPopup = popup();
    vi.spyOn(window, "open")
      .mockReturnValueOnce(oldPopup as unknown as Window)
      .mockReturnValueOnce(newPopup as unknown as Window);
    await setup();
    fireEvent.click(screen.getByRole("button", { name: "account.youliVerify" }));
    oldPopup.closed = true;
    act(() => vi.advanceTimersByTime(500));
    fireEvent.click(screen.getByRole("button", { name: "account.youliVerify" }));
    await act(async () => first.reject(new Error("late failure")));
    expect(newPopup.close).not.toHaveBeenCalled();
    await act(async () => second.resolve({ authorization_url: url }));
    sendProof(newPopup);
    expect(screen.getByLabelText("account.youliMobile")).toBeInTheDocument();
  });

  it("clears verification on bearer renewal and permits a retry with the current bearer", async () => {
    const first = deferred();
    mocks.start.mockReturnValueOnce(first.promise).mockResolvedValueOnce({ authorization_url: url });
    const oldPopup = popup();
    const newPopup = popup();
    vi.spyOn(window, "open")
      .mockReturnValueOnce(oldPopup as unknown as Window)
      .mockReturnValueOnce(newPopup as unknown as Window);
    await setup();
    fireEvent.click(screen.getByRole("button", { name: "account.youliVerify" }));
    mocks.token.mockReturnValue("session-2");
    await act(async () => first.resolve({ authorization_url: url }));
    expect(oldPopup.close).toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "account.youliVerify" })));
    sendProof(newPopup);
    expect(screen.getByLabelText("account.youliMobile")).toBeInTheDocument();
    mocks.token.mockReturnValue("session-3");
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByLabelText("account.youliMobile")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "account.youliVerify" })).toBeEnabled();
  });
});
