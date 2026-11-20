import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sharedFeishuApi } from "../../api/modules/sharedFeishu";
import SharedFeishuSources, {
  type FeishuSourceView,
} from "./SharedFeishuSources";

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
beforeEach(() => {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
});
afterEach(() => vi.restoreAllMocks());

it("imports a reviewed directory using the selected department account and explicit consent", async () => {
  vi.spyOn(sharedFeishuApi, "list").mockResolvedValue([]);
  vi.spyOn(sharedFeishuApi, "connections").mockResolvedValue({
    providers: [],
    connections: [
      {
        id: "ops",
        label: "Operations",
        provider_id: 1,
        tenant: "team",
        connected: true,
      },
      {
        id: "fin",
        label: "Finance",
        provider_id: 1,
        tenant: "team",
        connected: true,
      },
    ],
  });
  const browse = vi.spyOn(sharedFeishuApi, "browse").mockResolvedValue({
    items: [{ id: "space1", title: "Finance Wiki", type: "space" }],
    next_page: null,
  });
  const preview = vi.spyOn(sharedFeishuApi, "preview").mockResolvedValue({
    documents: [{ id: "a", title: "Expense policy", path: [] }],
    skipped: 1,
    visibility: {
      visibility: "groups",
      group_ids: ["grp1"],
      group_names: ["Finance staff"],
    },
    fingerprint: "current-review",
  });
  const approve = vi
    .spyOn(sharedFeishuApi, "approve")
    .mockResolvedValue({ id: "source1" });
  function Page() {
    const [view, setView] = useState<FeishuSourceView>("connect");
    return (
      <>
        <span>{view}</span>
        <SharedFeishuSources
          kbId="knowledge1"
          usable
          view={view}
          onNavigate={setView}
          onSourceCount={vi.fn()}
          onDocumentsChanged={vi.fn()}
        />
      </>
    );
  }
  render(<Page />);
  const click = (name: string) =>
    fireEvent.click(screen.getByRole("button", { name, exact: true }));

  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "Finance" }));
  click("sharedFeishu.directory");
  expect(await screen.findByText("Finance Wiki")).toBeInTheDocument();
  expect(browse).toHaveBeenCalledWith("fin", { kind: "wiki_spaces" });
  click("sharedFeishu.select");
  click("sharedFeishu.review");
  expect(await screen.findByText("Expense policy")).toBeInTheDocument();
  const scope = {
    connection_id: "fin",
    kind: "wiki_space",
    root: "space1",
    title: "Finance Wiki",
    space: null,
    include_children: true,
  };
  expect(preview).toHaveBeenCalledWith("knowledge1", scope);
  expect(
    screen.getByRole("button", { name: "sharedFeishu.import" }),
  ).toBeDisabled();
  expect(approve).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "sharedFeishu.confirm" }),
  );
  click("sharedFeishu.import");
  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith(
      "knowledge1",
      scope,
      "current-review",
      "daily",
      false,
    ),
  );
  expect(
    await screen.findByText("content", { exact: true }),
  ).toBeInTheDocument();
});

it("highlights pending documents as confirm action and submits custom minute interval", async () => {
  const scope = {
    connection_id: "fin",
    kind: "drive_folder" as const,
    root: "folder1",
    title: "山河日记",
    space: null,
    include_children: true,
  };
  vi.spyOn(sharedFeishuApi, "list").mockResolvedValue([
    {
      id: "src1",
      connection_id: "fin",
      connection_label: "飞书",
      title: "山河日记",
      scope_kind: "drive_folder",
      space_id: "",
      root_token: "folder1",
      include_children: 1,
      schedule: "interval",
      interval_minutes: 15,
      auto_add: 0,
      active: 1,
      status: "synced",
      last_sync: 1700000000,
      last_error: null,
      pending_count: 1,
      skipped_count: 0,
      documents: 1,
    },
  ]);
  vi.spyOn(sharedFeishuApi, "connections").mockResolvedValue({
    providers: [],
    connections: [
      {
        id: "fin",
        label: "飞书",
        provider_id: 1,
        tenant: "team",
        connected: true,
      },
    ],
  });
  const preview = vi.spyOn(sharedFeishuApi, "preview").mockResolvedValue({
    documents: [
      { id: "b", title: "New doc", path: [], is_new: true },
      { id: "a", title: "Old doc", path: [], is_new: false },
    ],
    skipped: 0,
    existing_source: true,
    new_count: 1,
    visibility: {
      visibility: "all",
      group_ids: [],
      group_names: [],
    },
    fingerprint: "next-review",
  });
  const approve = vi
    .spyOn(sharedFeishuApi, "approve")
    .mockResolvedValue({ id: "src1" });
  const onPendingCount = vi.fn();

  function Page() {
    const [view, setView] = useState<FeishuSourceView>("sources");
    return (
      <>
        <span>{view}</span>
        <SharedFeishuSources
          kbId="knowledge1"
          usable
          view={view}
          onNavigate={setView}
          onSourceCount={vi.fn()}
          onPendingCount={onPendingCount}
          onDocumentsChanged={vi.fn()}
        />
      </>
    );
  }

  render(<Page />);

  expect(
    await screen.findByText("sharedFeishu.pendingBanner"),
  ).toBeInTheDocument();
  expect(screen.getByText("sharedFeishu.everyMinutes")).toBeInTheDocument();
  expect(onPendingCount).toHaveBeenCalledWith(1);
  expect(
    screen.queryByRole("button", { name: "sharedFeishu.reviewChanges" }),
  ).toBeNull();

  fireEvent.click(
    screen.getByRole("button", { name: "sharedFeishu.pendingAction" }),
  );

  expect(await screen.findByText("New doc")).toBeInTheDocument();
  expect(preview).toHaveBeenCalledWith("knowledge1", scope);
  expect(screen.getByText("sharedFeishu.reviewNewBadge")).toBeInTheDocument();
  expect(
    screen.getByText("sharedFeishu.reviewImportedBadge"),
  ).toBeInTheDocument();

  const minuteInput = screen.getByRole("spinbutton", {
    name: "sharedFeishu.intervalUnit",
  });
  expect(minuteInput).toHaveValue("15");
  fireEvent.change(minuteInput, { target: { value: "10" } });

  fireEvent.click(
    screen.getByRole("checkbox", { name: "sharedFeishu.autoAdd" }),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: "sharedFeishu.confirm" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "sharedFeishu.import" }));

  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith(
      "knowledge1",
      scope,
      "next-review",
      "interval",
      true,
      10,
    ),
  );
});

it("shows sync settings button when there are no pending documents", async () => {
  vi.spyOn(sharedFeishuApi, "list").mockResolvedValue([
    {
      id: "src1",
      connection_id: "fin",
      connection_label: "飞书",
      title: "山河日记",
      scope_kind: "drive_folder",
      space_id: "",
      root_token: "folder1",
      include_children: 1,
      schedule: "daily",
      interval_minutes: null,
      auto_add: 1,
      active: 1,
      status: "synced",
      last_sync: 1700000000,
      last_error: null,
      pending_count: 0,
      skipped_count: 0,
      documents: 2,
    },
  ]);
  vi.spyOn(sharedFeishuApi, "connections").mockResolvedValue({
    providers: [],
    connections: [
      {
        id: "fin",
        label: "飞书",
        provider_id: 1,
        tenant: "team",
        connected: true,
      },
    ],
  });
  const onPendingCount = vi.fn();

  render(
    <SharedFeishuSources
      kbId="knowledge1"
      usable
      view="sources"
      onNavigate={vi.fn()}
      onSourceCount={vi.fn()}
      onPendingCount={onPendingCount}
      onDocumentsChanged={vi.fn()}
    />,
  );

  expect(
    await screen.findByRole("button", { name: "sharedFeishu.reviewChanges" }),
  ).toBeInTheDocument();
  expect(onPendingCount).toHaveBeenCalledWith(0);
  expect(screen.queryByText("sharedFeishu.pendingBanner")).toBeNull();
});
