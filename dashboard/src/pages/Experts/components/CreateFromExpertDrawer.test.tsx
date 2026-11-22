import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import CreateFromExpertDrawer from "./CreateFromExpertDrawer";

const requestMock = vi.hoisted(() => vi.fn());

vi.mock("../../../api/request", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/request")>()),
  request: requestMock,
}));

vi.mock("../../../hooks/useAgentFormResources", () => ({
  useAgentFormResources: () => ({
    models: [{ provider_name: "test", model: "default" }],
    modelsLoading: false,
    backends: [],
    backendsLoading: false,
  }),
}));

vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t, i18n: { language: "zh" } }) };
});

it("shows the duplicate-name error on the name field and keeps the drawer open", async () => {
  requestMock.mockImplementation(async (path: string) => {
    if (path === "/agents/from-expert/default") {
      throw new Error(
        'Request failed: 409 - {"error":{"code":"AGENT_NAME_TAKEN"}}',
      );
    }
    return [];
  });
  const onCreated = vi.fn();
  render(
    <MemoryRouter>
      <App>
        <CreateFromExpertDrawer
          open
          source={{
            kind: "builtin",
            expert: {
              id: "default",
              label: { zh: "Existing expert" },
              description: {},
            },
          }}
          lang="zh"
          onCreated={onCreated}
          onClose={vi.fn()}
        />
      </App>
    </MemoryRouter>,
  );
  const name = await screen.findByLabelText("experts.agentName");
  const submit = screen.getByRole("button", {
    name: "common.create",
    exact: true,
  });
  await waitFor(() => expect(submit).toBeEnabled());
  fireEvent.change(name, { target: { value: "" } });
  fireEvent.click(submit);
  expect(await screen.findByText("experts.pleaseEnterName")).toBeVisible();

  fireEvent.change(name, { target: { value: "Existing expert" } });
  fireEvent.click(submit);
  expect(await screen.findByText("apiErrors.AGENT_NAME_TAKEN")).toBeVisible();
  expect(name).toHaveValue("Existing expert");
  expect(onCreated).not.toHaveBeenCalled();
});
