import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { sharedFeishuApi } from "../../api/modules/sharedFeishu";
import SharedFeishuSources from "./SharedFeishuSources";

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
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
  render(
    <SharedFeishuSources
      kbId="knowledge1"
      usable
      onDocumentsChanged={vi.fn()}
    />,
  );
  const click = (name: string) =>
    fireEvent.click(screen.getByRole("button", { name, exact: true }));

  click("sharedFeishu.connect");
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "sharedFeishu.addScope" }),
    ).toBeEnabled(),
  );
  click("sharedFeishu.addScope");
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
    await screen.findByRole("button", { name: "sharedFeishu.addScope" }),
  ).toBeInTheDocument();
});
