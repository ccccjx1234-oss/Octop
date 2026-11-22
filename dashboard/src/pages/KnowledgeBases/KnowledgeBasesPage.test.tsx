import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { App } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "../../hooks/useCurrentUser";
import { knowledgeBasesApi } from "../../api/modules/knowledgeBases";
import { feishuKbApi } from "../../api/modules/feishuKb";
import { sharedFeishuApi } from "../../api/modules/sharedFeishu";
import type { KnowledgeCapability } from "../../api/modules/knowledgeBases";
import KnowledgeBasesPage from "./index";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: translate, i18n: { language: "zh" } }),
}));

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

const translate = (key: string, values?: { count?: number }) =>
  key === "knowledgeBases.documentCount" ? `${key} ${values?.count}` : key;

const unavailableCapability: KnowledgeCapability = {
  feature_enabled: true,
  usable: false,
  backend: "onnx",
  selected_model: "existing-model",
  provider_id: "",
  prerequisites_ok: false,
  checks: {
    model_selected: true,
    model_downloaded: false,
    deps_available: true,
    provider_ready: false,
  },
  ocr: {
    enabled: false,
    usable: false,
    backend: "onnx",
    model: "",
    provider_id: "",
    prerequisites_ok: false,
    checks: { deps_available: false, provider_ready: false },
  },
};

function renderPage(permissions = ["knowledge_bases"]) {
  render(
    <MemoryRouter initialEntries={["/knowledge-bases"]}>
      <CurrentUserProvider
        user={{
          id: 2,
          username: "owner",
          display_name: null,
          role: "user",
          locale: "zh",
          permissions,
        }}
        setUser={vi.fn()}
      >
        <App>
          <KnowledgeBasesPage />
        </App>
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shows required and duplicate name errors in the creation drawer", async () => {
  vi.spyOn(knowledgeBasesApi, "list").mockResolvedValue([]);
  vi.spyOn(knowledgeBasesApi, "getCapability").mockResolvedValue({
    ...unavailableCapability,
    usable: true,
  });
  const create = vi
    .spyOn(knowledgeBasesApi, "create")
    .mockRejectedValue(
      new Error(
        'Request failed: 409 - {"error":{"code":"KNOWLEDGE_NAME_TAKEN"}}',
      ),
    );
  renderPage();
  fireEvent.click(
    await screen.findByRole("button", { name: "knowledgeBases.create" }),
  );
  const drawer = within(await screen.findByRole("dialog"));
  const submit = drawer.getByRole("button", { name: "common.create" });
  fireEvent.click(submit);
  expect(await drawer.findByText("knowledgeBases.nameRequired")).toBeVisible();
  expect(create).not.toHaveBeenCalled();

  fireEvent.change(drawer.getByLabelText("knowledgeBases.name"), {
    target: { value: "Existing name" },
  });
  fireEvent.click(submit);
  expect(
    await drawer.findByText("apiErrors.KNOWLEDGE_NAME_TAKEN"),
  ).toBeVisible();
  expect(create).toHaveBeenCalledOnce();
});

it("updates the sidebar count after creating a text file", async () => {
  const base = {
    id: "kb1",
    owner_user_id: 2,
    name: "Local files",
    description: "",
    default_open: false,
    shared: false,
    icon_name: "",
    embedding_model: "existing-model",
    embedding_dim: 384,
    doc_count: 0,
    max_documents: 10,
    created_at: 1,
    updated_at: 1,
  };
  const document = {
    id: "doc1",
    kb_id: base.id,
    filename: "notes.md",
    content_type: "text/markdown",
    byte_size: 0,
    content_hash: "",
    status: "ready" as const,
    error_message: "",
    chunk_count: 0,
    created_at: 1,
    updated_at: 1,
  };
  let created = false;
  const currentBase = () => ({ ...base, doc_count: Number(created) });
  vi.spyOn(knowledgeBasesApi, "list").mockImplementation(async () => [
    currentBase(),
  ]);
  vi.spyOn(knowledgeBasesApi, "get").mockImplementation(async () =>
    currentBase(),
  );
  vi.spyOn(knowledgeBasesApi, "listDocuments").mockImplementation(async () =>
    created ? [document] : [],
  );
  vi.spyOn(knowledgeBasesApi, "getCapability").mockResolvedValue({
    ...unavailableCapability,
    usable: true,
  });
  vi.spyOn(sharedFeishuApi, "list").mockResolvedValue([]);
  vi.spyOn(sharedFeishuApi, "connections").mockResolvedValue({
    connections: [],
    providers: [],
  });
  vi.spyOn(knowledgeBasesApi, "createTextDocument").mockImplementation(
    async () => {
      created = true;
      return document;
    },
  );

  renderPage();
  expect(
    await screen.findByText("knowledgeBases.documentCount 0"),
  ).toBeVisible();
  const [createButton] = await screen.findAllByRole("button", {
    name: "knowledgeBases.createFile",
  });
  await waitFor(() => expect(createButton).toBeEnabled());
  fireEvent.click(createButton);
  const drawer = within(await screen.findByRole("dialog"));
  fireEvent.change(
    drawer.getByRole("textbox", { name: "knowledgeBases.filename" }),
    {
      target: { value: "notes" },
    },
  );
  fireEvent.click(drawer.getByRole("button", { name: "common.save" }));

  expect(await screen.findByText("notes.md")).toBeVisible();
  expect(
    await screen.findByText("knowledgeBases.documentCount 1"),
  ).toBeVisible();
});

it("keeps existing files visible when embedding is unavailable", async () => {
  vi.spyOn(sharedFeishuApi, "list").mockResolvedValue([]);
  vi.spyOn(sharedFeishuApi, "connections").mockResolvedValue({
    connections: [],
    providers: [{ id: 1, name: "Feishu" }],
  });
  const base = {
    id: "kb1",
    owner_user_id: 2,
    name: "Existing knowledge base",
    description: "",
    default_open: false,
    shared: false,
    icon_name: "",
    embedding_model: "existing-model",
    embedding_dim: 384,
    doc_count: 1,
    max_documents: 10,
    created_at: 1,
    updated_at: 1,
  };
  vi.spyOn(knowledgeBasesApi, "list").mockResolvedValue([base]);
  vi.spyOn(knowledgeBasesApi, "get").mockResolvedValue(base);
  vi.spyOn(knowledgeBasesApi, "getCapability").mockResolvedValue(
    unavailableCapability,
  );
  vi.spyOn(knowledgeBasesApi, "listDocuments").mockResolvedValue([
    {
      id: "doc1",
      kb_id: base.id,
      filename: "existing-file.pdf",
      content_type: "application/pdf",
      byte_size: 128,
      content_hash: "",
      status: "ready",
      error_message: "",
      chunk_count: 1,
      created_at: 1,
      updated_at: 1,
      has_original: true,
    },
  ]);

  renderPage();

  expect(
    await screen.findByRole("heading", { name: "Existing knowledge base" }),
  ).toBeVisible();
  expect(await screen.findByText("existing-file.pdf")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "knowledgeBases.downloadOriginal" }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: "sharedFeishu.localFiles" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "sharedFeishu.connect" }));
  expect(await screen.findByText("sharedFeishu.newGrantHint")).toBeVisible();
  expect(
    await screen.findByRole("button", { name: "sharedFeishu.authorize" }),
  ).toBeEnabled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(
    screen.queryByRole("button", { name: "knowledgeBases.downloadOriginal" }),
  ).toBeNull();
});

it("keeps old private collection accessible separately from the main library", async () => {
  vi.spyOn(knowledgeBasesApi, "list").mockResolvedValue([]);
  vi.spyOn(knowledgeBasesApi, "getCapability").mockResolvedValue(
    unavailableCapability,
  );
  const embeddingOptions = vi.spyOn(knowledgeBasesApi, "getEmbeddingOptions");
  vi.spyOn(feishuKbApi, "status").mockResolvedValue({
    identities: [],
    scheduler_configured: false,
  });
  vi.spyOn(feishuKbApi, "sources").mockResolvedValue([
    {
      source_id: "source-1",
      provider_id: 1,
      document_id: "collected-file",
      enabled: true,
      retrieval_enabled: false,
      interval_seconds: 900,
      next_sync: 0,
      status: "updated",
      has_staged_content: true,
    },
  ]);

  renderPage();
  expect(
    await screen.findByRole("button", { name: "knowledgeBases.create" }),
  ).toBeDisabled();
  expect(screen.queryByText("collected-file")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "feishuKb.legacyTitle" }));
  expect(await screen.findByText("collected-file")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "feishuKb.viewContent" }),
  ).toBeEnabled();
  const legacy = screen.getByRole("dialog", { name: "feishuKb.legacyTitle" });
  fireEvent.click(within(legacy).getByRole("button", { name: "Close" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("dialog", { name: "feishuKb.legacyTitle" }),
    ).toBeNull(),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "knowledgeBases.settingsTitle" }),
  );
  await waitFor(() =>
    expect(
      screen.getByText("knowledgeBases.settingsPermissionHint"),
    ).toBeVisible(),
  );
  expect(embeddingOptions).not.toHaveBeenCalled();
});

it("opens the original instance settings for an account with settings permission", async () => {
  vi.spyOn(knowledgeBasesApi, "list").mockResolvedValue([]);
  vi.spyOn(knowledgeBasesApi, "getCapability").mockResolvedValue(
    unavailableCapability,
  );
  vi.spyOn(feishuKbApi, "status").mockResolvedValue({
    identities: [],
    scheduler_configured: false,
  });
  vi.spyOn(feishuKbApi, "sources").mockResolvedValue([]);
  const embeddingOptions = vi
    .spyOn(knowledgeBasesApi, "getEmbeddingOptions")
    .mockResolvedValue({ onnx: [], remote: [] });
  vi.spyOn(knowledgeBasesApi, "getOcrOptions").mockResolvedValue({
    local: { id: "rapidocr", name: "RapidOCR" },
    remote: [],
  });
  vi.spyOn(knowledgeBasesApi, "getRerankOptions").mockResolvedValue({
    providers: [],
  });
  vi.spyOn(knowledgeBasesApi, "getGraphOptions").mockResolvedValue({
    remote: [],
  });
  vi.spyOn(knowledgeBasesApi, "getOnnxDownloadStatus").mockResolvedValue({
    status: "idle",
    progress: 0,
    model_name: "",
  });

  renderPage(["knowledge_bases", "knowledge_settings"]);
  await screen.findByText("knowledgeBases.enableGuideDesc");
  fireEvent.click(
    screen.getAllByRole("button", { name: "knowledgeBases.settingsTitle" })[0],
  );
  expect(await screen.findByText("knowledgeBases.settingsOpen")).toBeVisible();
  expect(embeddingOptions).toHaveBeenCalled();
  expect(
    screen.queryByText("knowledgeBases.settingsPermissionHint"),
  ).toBeNull();
});
