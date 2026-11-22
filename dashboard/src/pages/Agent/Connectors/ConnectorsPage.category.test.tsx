import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { connectorsApi } from "../../../api/modules/connectors";

import type {
  ConnectorCatalogEntry,
  ConnectorInstance,
} from "../../../api/modules/connectors";

const { catalog, instances } = vi.hoisted(() => {
  const catalog: ConnectorCatalogEntry[] = [
    {
      kind: "notion",
      name: "Notion",
      description: "",
      auth_kind: "oauth",
      doc_url: "",
      icon: "",
      color: "#000",
      phase: "available",
      mcp_mode: "gateway",
      category: "knowledge",
    },
    {
      kind: "amap",
      name: "Amap",
      description: "",
      auth_kind: "api_key",
      doc_url: "",
      icon: "",
      color: "#000",
      phase: "available",
      mcp_mode: "gateway",
      category: "travel",
    },
    {
      kind: "tencent-docs",
      name: "Tencent Docs",
      description: "Read and write Tencent Docs",
      auth_kind: "personal_token",
      doc_url: "",
      icon: "",
      color: "#000",
      phase: "available",
      mcp_mode: "gateway",
      category: "office",
    },
  ];
  const instances: ConnectorInstance[] = [
    {
      instance_id: "notes",
      kind: "notion",
      display_name: "My Notion",
      status: "ready",
      mcp_server_name: "notion",
      has_credentials: true,
      shared: false,
      owner_user_id: 1,
      can_manage: true,
      created_at: 0,
      updated_at: 0,
    },
    {
      instance_id: "maps",
      kind: "amap",
      display_name: "My Maps",
      status: "ready",
      mcp_server_name: "amap",
      has_credentials: true,
      shared: false,
      owner_user_id: 1,
      can_manage: true,
      created_at: 0,
      updated_at: 0,
    },
  ];
  return { catalog, instances };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: "zh", changeLanguage: () => Promise.resolve() },
  }),
}));

vi.mock("@/utils/antdMessage", () => ({
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

vi.mock("./useConnectors", () => ({
  useConnectorInstances: () => ({
    catalog,
    instances,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({
    id: 1,
    username: "admin",
    role: "admin",
    display_name: "Admin",
    locale: "zh",
  }),
}));

vi.mock("./CustomMcpTab", () => ({
  CustomMcpTab: () => <div>custom-tab</div>,
}));

import ConnectorsPage from "./index";

function renderPage() {
  return render(
    <MemoryRouter>
      <ConnectorsPage />
    </MemoryRouter>,
  );
}

describe("ConnectorsPage category pills", () => {
  it("filters enabled instances and the built-in catalog", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByText("My Notion")).toBeInTheDocument();
    expect(screen.getByText("My Maps")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "connectors.category.knowledge" }),
    );
    expect(screen.getByText("My Notion")).toBeInTheDocument();
    expect(screen.queryByText("My Maps")).not.toBeInTheDocument();
    expect(
      screen.queryByText("connectors.categoryEmpty"),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "connectors.category.office" }),
    );
    expect(screen.queryByText("My Notion")).not.toBeInTheDocument();
    expect(screen.getByText("connectors.categoryEmpty")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "connectors.categoryAll" }),
    );
    expect(screen.getByText("My Notion")).toBeInTheDocument();
    expect(screen.getByText("My Maps")).toBeInTheDocument();

    await user.click(
      screen.getByRole("tab", { name: "connectors.tabBuiltin" }),
    );
    expect(screen.getByText("Notion")).toBeInTheDocument();
    expect(screen.getByText("Amap")).toBeInTheDocument();
    expect(screen.getByText("Tencent Docs")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "connectors.category.travel" }),
    );
    expect(screen.queryByText("Notion")).not.toBeInTheDocument();
    expect(screen.queryByText("Tencent Docs")).not.toBeInTheDocument();
    expect(screen.getByText("Amap")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "connectors.tabCustom" }));
    expect(
      screen.queryByRole("button", { name: "connectors.categoryAll" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("custom-tab")).toBeInTheDocument();
  });

  it.each(["", "   "])(
    "shows a required message for an empty token (%j)",
    async (token) => {
      const user = userEvent.setup();
      const authInfo = vi
        .spyOn(connectorsApi, "authInfo")
        .mockRejectedValue(new Error());
      const create = vi.spyOn(connectorsApi, "createInstance");
      try {
        renderPage();
        await user.click(
          screen.getByRole("tab", { name: "connectors.tabBuiltin" }),
        );
        await user.click(screen.getByText("Tencent Docs"));
        if (token)
          await user.type(screen.getByLabelText("connectors.token"), token);
        await user.click(screen.getByRole("button", { name: "common.save" }));
        expect(
          await screen.findByText("connectors.tokenRequired"),
        ).toBeVisible();
        expect(create).not.toHaveBeenCalled();
      } finally {
        authInfo.mockRestore();
        create.mockRestore();
      }
    },
  );

  it("lets an existing connection keep its saved token when the field is left empty", async () => {
    const user = userEvent.setup();
    const stored: ConnectorInstance = {
      ...instances[0],
      instance_id: "stored-docs",
      kind: "tencent-docs",
      display_name: "My Tencent Docs",
      description: "Office documents",
    };
    const authInfo = vi
      .spyOn(connectorsApi, "authInfo")
      .mockRejectedValue(new Error());
    const detail = vi.spyOn(connectorsApi, "getInstance").mockResolvedValue({
      ...stored,
      config: {},
      credentials_preview: { token_configured: true },
    });
    const save = vi
      .spyOn(connectorsApi, "patchInstance")
      .mockResolvedValue(stored);
    instances.push(stored);
    try {
      renderPage();
      await user.click(screen.getByText("My Tencent Docs"));
      await waitFor(() => {
        expect(screen.getByLabelText("connectors.displayName")).toHaveValue(
          "My Tencent Docs",
        );
      });
      expect(screen.getByLabelText("connectors.token")).toHaveValue("");
      await user.click(screen.getByRole("button", { name: "common.save" }));
      expect(save).toHaveBeenCalledWith(
        stored.instance_id,
        expect.objectContaining({ credentials: {} }),
      );
    } finally {
      instances.pop();
      authInfo.mockRestore();
      detail.mockRestore();
      save.mockRestore();
    }
  });
});
