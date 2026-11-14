import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { OctopAgent } from "../../../context/AgentContext";

const { request, listResolvedModels, refresh, t } = vi.hoisted(() => ({
  request: vi.fn(),
  listResolvedModels: vi.fn(),
  refresh: vi.fn(),
  t: (key: string) => key,
}));

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t }) }));
vi.mock("../../../api/request", () => ({ request }));
vi.mock("../../../api/modules/provider", () => ({
  providerApi: { listResolvedModels },
}));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({ refresh }),
}));
vi.mock("../../../components/TextAssistBar", () => ({
  TextAssistBar: () => null,
}));
vi.mock("../../../components/ExpertColorPicker", () => ({
  default: () => null,
}));
vi.mock("../../../components/ResourceVisibilityFields", () => ({
  ResourceVisibilityFormItem: () => null,
}));
vi.mock("../../Agent/Workspace/components/WorkspaceDrawer", () => ({
  default: () => null,
}));
vi.mock("../../Agent/Skills/skillDisplayNames", () => ({
  useSkillDisplayName: () => (slug: string) => slug,
}));
vi.mock("./ExpertAvatarPicker", () => ({ default: () => null }));
vi.mock("./ExpertComposerDefaultsFields", () => ({ default: () => null }));
vi.mock("./FileEditModal", () => ({ default: () => null }));
vi.mock("./WelcomeConfig", () => ({ default: () => null }));
vi.mock("./SkillCatalogDrawer", () => ({ default: () => null }));
vi.mock("./SubagentCatalogDrawer", () => ({ default: () => null }));

import EditAgentDrawer from "./EditAgentDrawer";

const backend = {
  type: "local_shell",
  root_dir: "/private/user-workspace",
  virtual_mode: true,
  custom_option: "preserve-me",
};
const getComputedStyle = window.getComputedStyle.bind(window);

function renderEditor(configBackend: unknown = backend, bridge = false) {
  const agent = {
    agent_id: bridge ? "bridge:peer:expert" : "expert-1",
    name: "Original",
    state: "stopped",
    color: null,
    icon_url: null,
    bridge,
  } as OctopAgent;
  const onSaved = vi.fn();
  const onClose = vi.fn();
  request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path.startsWith("/filesystem") || path === "/storage-backends") {
      throw new Error(
        'Request failed: 403 Forbidden - {"error":{"code":"FORBIDDEN","message":"没有权限。","details":{}}}',
      );
    }
    if (path === `/agents/${agent.agent_id}` && !options?.method) {
      return {
        ...agent,
        config: {
          backend: configBackend,
          custom_config: "keep",
          enable_trajectory: true,
        },
        knowledge_base_ids: ["kb-1"],
        mcp_servers: ["mcn"],
      };
    }
    return {};
  });
  render(
    <App>
      <EditAgentDrawer open agent={agent} onSaved={onSaved} onClose={onClose} />
    </App>,
  );
  return { agent, onSaved, onClose };
}

async function loadedName() {
  const input = await screen.findByLabelText("experts.agentName");
  await waitFor(() => expect(input).toHaveValue("Original"));
  return input;
}

describe("EditAgentDrawer save", () => {
  // jsdom supports element styles but not the portal's pseudo-element query.
  const styleSpy = vi
    .spyOn(window, "getComputedStyle")
    .mockImplementation((element) => getComputedStyle(element));
  beforeEach(() => {
    request.mockReset();
    listResolvedModels.mockReset().mockResolvedValue([]);
    refresh.mockReset().mockResolvedValue(undefined);
  });
  afterAll(() => styleSpy.mockRestore());

  it.each([
    ["scoped local", backend, false],
    ["named", { type: "named", name: "private-store", extra: "keep" }, false],
    [
      "composite",
      { type: "composite", default: backend, routes: { "/data": backend } },
      false,
    ],
    ["peer", backend, true],
  ])(
    "saves %s experts without host APIs and preserves the backend",
    async (_name, configBackend, bridge) => {
      const { agent, onSaved, onClose } = renderEditor(configBackend, bridge);
      fireEvent.change(await loadedName(), { target: { value: "Changed" } });
      fireEvent.click(
        screen.getByRole("button", { name: "common.save", exact: true }),
      );
      await waitFor(() =>
        expect(onSaved).toHaveBeenCalledWith(
          expect.objectContaining({ name: "Changed" }),
        ),
      );
      const patch = request.mock.calls.find(
        ([, options]) => options?.method === "PATCH",
      );
      expect(patch?.[0]).toBe(`/agents/${agent.agent_id}`);
      expect(JSON.parse(patch?.[1].body as string)).toMatchObject({
        config: { backend: configBackend, custom_config: "keep" },
        knowledge_base_ids: ["kb-1"],
        mcp_servers: ["mcn"],
      });
      expect(
        request.mock.calls.some(
          ([path]) =>
            path.startsWith("/filesystem") || path === "/storage-backends",
        ),
      ).toBe(false);
      expect(listResolvedModels).toHaveBeenCalledWith(agent.agent_id);
      expect(onClose).toHaveBeenCalledOnce();
    },
  );

  it("shows validation errors and does not submit an empty name", async () => {
    const { onSaved, onClose } = renderEditor();
    fireEvent.change(await loadedName(), { target: { value: "" } });
    fireEvent.click(
      screen.getByRole("button", { name: "common.save", exact: true }),
    );
    await waitFor(() =>
      expect(
        document.querySelector(".ant-message-error")?.textContent,
      ).toContain("experts.pleaseEnterName"),
    );
    expect(
      request.mock.calls.some(([, options]) => options?.method === "PATCH"),
    ).toBe(false);
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "common.save", exact: true }),
    ).toBeEnabled();
  });

  it("disables saving while PATCH is pending and submits only once", async () => {
    const { onSaved } = renderEditor();
    await loadedName();
    let finishPatch!: (value: object) => void;
    const pendingPatch = new Promise((resolve) => {
      finishPatch = resolve;
    });
    const normalRequest = request.getMockImplementation()!;
    request.mockImplementation((path: string, options?: RequestInit) =>
      options?.method === "PATCH" ? pendingPatch : normalRequest(path, options),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "common.save", exact: true }),
    );
    const busyButton = await screen.findByRole("button", {
      name: "experts.creating",
      exact: true,
    });
    expect(busyButton).toBeDisabled();
    fireEvent.click(busyButton);
    await waitFor(() =>
      expect(
        request.mock.calls.filter(([, options]) => options?.method === "PATCH"),
      ).toHaveLength(1),
    );
    expect(onSaved).not.toHaveBeenCalled();
    finishPatch({});
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  });

  it("shows PATCH failures, keeps the drawer open, and permits retry", async () => {
    const { onSaved, onClose } = renderEditor();
    await loadedName();
    const normalRequest = request.getMockImplementation()!;
    request.mockImplementation((path: string, options?: RequestInit) =>
      options?.method === "PATCH"
        ? Promise.reject(
            new Error(
              'Request failed: 403 Forbidden - {"error":{"code":"FORBIDDEN","message":"没有权限。","details":{}}}',
            ),
          )
        : normalRequest(path, options),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "common.save", exact: true }),
    );
    await waitFor(() =>
      expect(
        document.querySelector(".ant-message-error")?.textContent,
      ).toContain("没有权限。"),
    );
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "common.save", exact: true }),
    ).toBeEnabled();
    request.mockImplementation(normalRequest);
    fireEvent.click(
      screen.getByRole("button", { name: "common.save", exact: true }),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  });
});
