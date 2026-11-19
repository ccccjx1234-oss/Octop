import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { App } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "../../hooks/useCurrentUser";
import { knowledgeBasesApi } from "../../api/modules/knowledgeBases";
import { feishuKbApi } from "../../api/modules/feishuKb";
import type { KnowledgeCapability } from "../../api/modules/knowledgeBases";
import KnowledgeBasesPage from "./index";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

const translate = (key: string) => key;

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

it("keeps existing files visible when embedding is unavailable", async () => {
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

  expect(await screen.findByText("Existing knowledge base")).toBeVisible();
  expect(await screen.findByText("existing-file.pdf")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "knowledgeBases.downloadOriginal" }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: "knowledgeBases.upload" }),
  ).toBeDisabled();
});

it("shows collected files on the main page and keeps library maintenance discoverable", async () => {
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
  expect(await screen.findByText("collected-file")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "feishuKb.viewContent" }),
  ).toBeEnabled();
  expect(screen.queryByRole("dialog")).toBeNull();

  fireEvent.click(
    screen.getByRole("radio", { name: "knowledgeBases.librarySectionCount" }),
  );
  expect(
    screen.queryByRole("button", { name: "feishuKb.viewContent" }),
  ).toBeNull();
  expect(
    screen.getByRole("button", { name: "knowledgeBases.create" }),
  ).toBeDisabled();
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
  await screen.findByText("feishuKb.noSources");
  fireEvent.click(
    screen.getByRole("button", { name: "knowledgeBases.settingsTitle" }),
  );
  expect(await screen.findByText("knowledgeBases.settingsOpen")).toBeVisible();
  expect(embeddingOptions).toHaveBeenCalled();
  expect(
    screen.queryByText("knowledgeBases.settingsPermissionHint"),
  ).toBeNull();
});
