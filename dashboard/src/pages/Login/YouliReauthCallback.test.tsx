import { afterEach, describe, expect, it, vi } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";
import YouliReauthCallback from "./YouliReauthCallback";

const complete = vi.hoisted(() => vi.fn());
vi.mock("../../api/modules/auth", () => ({
  authApi: { completeYouliReauth: complete },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  cleanup();
  complete.mockReset();
  window.history.replaceState(null, "", "/");
});

describe("ERP Feishu callback", () => {
  it("removes callback parameters and returns proof only to the opener origin", async () => {
    const postMessage = vi.fn();
    Object.defineProperty(window, "opener", {
      configurable: true,
      value: { postMessage },
    });
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    window.history.replaceState(
      null,
      "",
      "/youli/reauth/callback?state=state-value&code=code-value",
    );
    complete.mockResolvedValue({ proof: "proof-value" });
    render(<YouliReauthCallback />);
    expect(window.location.search).toBe("");
    await waitFor(() =>
      expect(postMessage).toHaveBeenCalledWith(
        { type: "octop-youli-reauth", proof: "proof-value" },
        window.location.origin,
      ),
    );
    expect(complete).toHaveBeenCalledWith("state-value", "code-value");
    close.mockRestore();
  });

  it("fails closed and removes cancellation parameters", async () => {
    const postMessage = vi.fn();
    Object.defineProperty(window, "opener", {
      configurable: true,
      value: { postMessage },
    });
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    window.history.replaceState(
      null,
      "",
      "/youli/reauth/callback?error=access_denied&state=state-value",
    );
    render(<YouliReauthCallback />);
    expect(window.location.search).toBe("");
    expect(complete).not.toHaveBeenCalled();
    expect(postMessage).toHaveBeenCalledWith(
      { type: "octop-youli-reauth", proof: undefined },
      window.location.origin,
    );
    close.mockRestore();
  });
});
