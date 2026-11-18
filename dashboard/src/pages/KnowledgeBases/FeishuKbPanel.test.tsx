import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FeishuKbPanel from "./FeishuKbPanel";

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  sources: vi.fn(),
  authorize: vi.fn(),
  callback: vi.fn(),
  create: vi.fn(),
  configure: vi.fn(),
  sync: vi.fn(),
  remove: vi.fn(),
  revoke: vi.fn(),
  token: vi.fn(),
}));
vi.mock("../../api/modules/feishuKb", () => ({ feishuKbApi: mocks }));
vi.mock("../../api/request", () => ({ getAuthToken: mocks.token }));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("react-i18next", () => {
  const t = (key: string, values?: { time?: string; status?: string }) =>
    values?.time
      ? `${key}: ${values.time}`
      : values?.status
      ? `${key}: ${values.status}`
      : key;
  return { useTranslation: () => ({ t }) };
});
const identity = {
  provider_id: 1,
  provider_name: "Feishu",
  tenant: "tenant",
  identity: "person",
  consent: "authorized",
  requested_scopes: [],
};
const source = {
  source_id: "s1",
  provider_id: 1,
  document_id: "doc1",
  enabled: true,
  retrieval_enabled: false,
  interval_seconds: 900,
  next_sync: 0,
  status: "pending",
};
const popup = () => ({ closed: false, close: vi.fn(), location: { href: "" } });
const send = (
  sourceWindow: ReturnType<typeof popup>,
  origin = window.location.origin,
  code = "code",
) =>
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin,
        source: sourceWindow as unknown as Window,
        data: { type: "octop-feishu-kb-callback", state: "state", code },
      }),
    );
  });
async function setup() {
  render(<FeishuKbPanel />);
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.title" }));
  await screen.findByRole("button", { name: "feishuKb.authorize" });
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
    dispatchEvent: vi.fn(),
  }));
  mocks.token.mockReturnValue("original");
  mocks.status.mockResolvedValue({
    identities: [identity],
    scheduler_configured: false,
  });
  mocks.sources.mockResolvedValue([source]);
  mocks.callback.mockResolvedValue(undefined);
  mocks.authorize.mockResolvedValue({
    url: `https://accounts.feishu.cn/oauth/v3/authorize?state=state&redirect_uri=${encodeURIComponent(
      window.location.origin + "/feishu-kb/callback",
    )}`,
  });
  mocks.remove.mockResolvedValue({});
  mocks.revoke.mockResolvedValue({});
  mocks.sync.mockResolvedValue({});
  mocks.configure.mockResolvedValue({});
  mocks.create.mockResolvedValue({});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("accepts only the active popup and original session and ignores replay", async () => {
  const child = popup();
  vi.spyOn(window, "open").mockReturnValue(child as unknown as Window);
  await setup();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  await waitFor(() =>
    expect(child.location.href).toContain("accounts.feishu.cn"),
  );
  expect(mocks.authorize).toHaveBeenCalledTimes(1);
  send(popup());
  send(child, "https://evil.example");
  expect(mocks.callback).not.toHaveBeenCalled();
  send(child);
  await waitFor(() =>
    expect(mocks.callback).toHaveBeenCalledWith("original", "state", "code"),
  );
  send(child);
  expect(mocks.callback).toHaveBeenCalledTimes(1);
});
it("rejects changed sessions and cancellation and permits retry", async () => {
  const child = popup();
  vi.spyOn(window, "open").mockReturnValue(child as unknown as Window);
  await setup();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  await waitFor(() =>
    expect(child.location.href).toContain("accounts.feishu.cn"),
  );
  mocks.token.mockReturnValue("renewed");
  send(child);
  expect(mocks.callback).not.toHaveBeenCalled();
  fireEvent.click(
    await screen.findByRole("button", { name: "feishuKb.authorize" }),
  );
  send(child, window.location.origin, "");
  expect(mocks.callback).not.toHaveBeenCalled();
});
it("requires confirmation to delete or revoke and sends sync and configuration calls", async () => {
  await setup();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.sync" }));
  await waitFor(() => expect(mocks.sync).toHaveBeenCalledWith("s1"));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "common.save" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "common.save" }));
  await waitFor(() =>
    expect(mocks.configure).toHaveBeenCalledWith("s1", true, 900),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "feishuKb.delete" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.delete" }));
  expect(mocks.remove).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByText("OK"));
  await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith("s1"));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "feishuKb.revoke" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.revoke" }));
  expect(mocks.revoke).not.toHaveBeenCalled();
  const revokeConfirmation = (
    await screen.findByText("feishuKb.revokeConfirm")
  ).closest(".ant-popover")!;
  fireEvent.click(within(revokeConfirmation).getByText("OK"));
  await waitFor(() => expect(mocks.revoke).toHaveBeenCalledWith(1));
});

it("does not let a late cancelled authorization close a newer popup", async () => {
  let rejectFirst!: (reason: Error) => void;
  const first = new Promise<{ url: string }>((_, reject) => {
    rejectFirst = reject;
  });
  mocks.authorize.mockReturnValueOnce(first);
  const oldPopup = popup();
  const newPopup = popup();
  vi.spyOn(window, "open")
    .mockReturnValueOnce(oldPopup as unknown as Window)
    .mockReturnValueOnce(newPopup as unknown as Window);
  await setup();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  send(oldPopup, window.location.origin, "");
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  await waitFor(() =>
    expect(newPopup.location.href).toContain("accounts.feishu.cn"),
  );
  await act(async () => {
    rejectFirst(new Error("late"));
  });
  expect(newPopup.close).not.toHaveBeenCalled();
  send(newPopup);
  await waitFor(() =>
    expect(mocks.callback).toHaveBeenCalledWith("original", "state", "code"),
  );
});

it("creates only the explicitly entered docx source", async () => {
  await setup();
  fireEvent.mouseDown(
    screen.getByRole("combobox", { name: "feishuKb.provider" }),
  );
  fireEvent.click((await screen.findAllByText("Feishu (1)")).at(-1)!);
  fireEvent.change(
    screen.getByRole("textbox", { name: "feishuKb.documentId" }),
    { target: { value: "doc-123" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.add" }));
  await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(1, "doc-123"));
});

it("keeps finalization open and isolates the UI after the session changes", async () => {
  let finish!: () => void;
  mocks.callback.mockReturnValueOnce(
    new Promise<void>((resolve) => {
      finish = resolve;
    }),
  );
  const child = popup();
  vi.spyOn(window, "open").mockReturnValue(child as unknown as Window);
  await setup();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.authorize" }));
  await waitFor(() =>
    expect(child.location.href).toContain("accounts.feishu.cn"),
  );
  send(child);
  expect(await screen.findByText("feishuKb.finalizing")).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
  expect(screen.getByText("feishuKb.finalizing")).toBeInTheDocument();
  mocks.status.mockResolvedValue({
    identities: [],
    scheduler_configured: false,
  });
  mocks.sources.mockResolvedValue([]);
  mocks.token.mockReturnValue("other-user");
  await screen.findByText("feishuKb.sessionChanged");
  expect(screen.queryByText("Feishu (1)")).not.toBeInTheDocument();
  await act(async () => finish());
  expect(screen.getByText("feishuKb.sessionChanged")).toBeInTheDocument();
  expect(mocks.callback).toHaveBeenCalledTimes(1);
});

it("refreshes failed sync status and reflects the scheduler setting", async () => {
  mocks.sync.mockRejectedValueOnce(new Error("sensitive upstream error"));
  await setup();
  expect(
    screen.getByText("feishuKb.nextRun: feishuKb.schedulerDisabled"),
  ).toBeInTheDocument();
  mocks.status.mockResolvedValue({
    identities: [identity],
    scheduler_configured: true,
  });
  mocks.sources.mockResolvedValue([
    { ...source, status: "error", next_sync: 0 },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.sync" }));
  await waitFor(() => expect(mocks.sources).toHaveBeenCalledTimes(2));
  expect(
    screen.queryByText("sensitive upstream error"),
  ).not.toBeInTheDocument();
  expect(screen.getByText("feishuKb.schedulerOn")).toBeInTheDocument();
  expect(
    screen.getByText("feishuKb.nextRun: feishuKb.dueNow"),
  ).toBeInTheDocument();
  expect(screen.getByText("feishuKb.lastStatus: error")).toBeInTheDocument();
});

it.each(["status", "sources"] as const)(
  "clears stale data when sync fails and %s refresh fails",
  async (failedRequest) => {
    mocks.sync.mockRejectedValueOnce(new Error("sync failed"));
    await setup();
    expect(
      screen.getByText("feishuKb.lastStatus: pending"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("feishuKb.nextRun: feishuKb.schedulerDisabled"),
    ).toBeInTheDocument();
    mocks[failedRequest].mockRejectedValueOnce(new Error("refresh failed"));

    fireEvent.click(screen.getByRole("button", { name: "feishuKb.sync" }));

    await waitFor(() =>
      expect(
        screen.queryByText("feishuKb.lastStatus: pending"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(source.document_id)).not.toBeInTheDocument();
    expect(screen.queryByText(/feishuKb.nextRun/)).not.toBeInTheDocument();
    expect(screen.queryByText("feishuKb.schedulerOff")).not.toBeInTheDocument();
    expect(screen.queryByText("Feishu (1)")).not.toBeInTheDocument();
  },
);

it("requires a separate opt-in for semantic retrieval", async () => {
  await setup();
  const retrieval = screen.getByRole("switch", { name: "feishuKb.retrieval" });
  expect(retrieval).toHaveAttribute("aria-checked", "false");
  expect(screen.getByText("feishuKb.retrievalHint")).toBeInTheDocument();
  fireEvent.click(retrieval);
  await waitFor(() =>
    expect(mocks.configure).toHaveBeenCalledWith("s1", true, 900, true),
  );
});
