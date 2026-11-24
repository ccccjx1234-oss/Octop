import { Form } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import {
  ResourcePolicyFields,
  rolePoliciesFromForm,
  type PolicyFormValues,
} from "./UsersListPanel";

vi.mock("react-pdf", () => ({
  Document: () => null,
  Page: () => null,
  pdfjs: { GlobalWorkerOptions: {} },
}));

it("saves 100 million tokens per three days and can switch back to lifetime", async () => {
  const user = userEvent.setup();
  const saved = vi.fn();
  render(
    <Form<PolicyFormValues>
      initialValues={{ limit_token_quota: true, token_quota: 100_000_000 }}
      onFinish={(values) =>
        saved(rolePoliciesFromForm(values, { workspaceRootAllowed: false }))
      }
    >
      <ResourcePolicyFields fsTreeRoots={[]} workspaceRootAllowed={false} />
      <button type="submit">Save</button>
    </Form>,
  );
  const cycle = screen.getByRole("combobox", { name: "adminUsers.quotaCycle" });
  await user.click(cycle);
  await user.click(
    await screen.findByText("adminUsers.quotaPeriodic", {
      selector: ".ant-select-item-option-content",
    }),
  );
  const days = screen.getByRole("spinbutton", { name: "adminUsers.quotaDays" });
  await user.clear(days);
  await user.type(days, "3");
  await user.click(screen.getByRole("button", { name: "Save" }));
  const amount = { name: "token_quota", value: "100000000" };
  await waitFor(() =>
    expect(saved).toHaveBeenLastCalledWith([
      amount,
      { name: "token_quota_days", value: "3" },
    ]),
  );
  await user.click(cycle);
  await user.click(
    await screen.findByText("adminUsers.quotaLifetime", {
      selector: ".ant-select-item-option-content",
    }),
  );
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(saved).toHaveBeenLastCalledWith([amount]));
});
