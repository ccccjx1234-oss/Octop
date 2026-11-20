import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import FeishuKbCallback from "./FeishuKbCallback";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});
it("clears the callback URL and sends only to the opener origin", () => {
  const postMessage = vi.fn();
  Object.defineProperty(window, "opener", {
    configurable: true,
    value: { postMessage },
  });
  vi.spyOn(window, "close").mockImplementation(() => {});
  window.history.replaceState(
    null,
    "",
    "/feishu-kb/callback?state=state&code=code",
  );
  render(<FeishuKbCallback />);
  expect(window.location.search).toBe("");
  expect(postMessage).toHaveBeenCalledWith(
    { type: "octop-feishu-kb-callback", state: "state", code: "code" },
    window.location.origin,
  );
});
it("forwards cancellation without an authorization code", () => {
  const postMessage = vi.fn();
  Object.defineProperty(window, "opener", {
    configurable: true,
    value: { postMessage },
  });
  vi.spyOn(window, "close").mockImplementation(() => {});
  window.history.replaceState(
    null,
    "",
    "/feishu-kb/callback?error=access_denied&state=state&code=ignored",
  );
  render(<FeishuKbCallback />);
  expect(window.location.search).toBe("");
  expect(postMessage).toHaveBeenCalledWith(
    { type: "octop-feishu-kb-callback", state: "state", code: "" },
    window.location.origin,
  );
});
