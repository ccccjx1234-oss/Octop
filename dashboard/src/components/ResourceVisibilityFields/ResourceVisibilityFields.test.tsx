import { useState } from "react";
import { App } from "antd";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  emptyScopeSelection,
  type ResourceScopeSelection,
  type ScopedResourceVisibilityMode,
} from "../../api/modules/erpOrg";

const { audienceMock, listGroupsMock, erpMocks, t } = vi.hoisted(() => ({
  audienceMock: vi.fn(),
  listGroupsMock: vi.fn(),
  erpMocks: {
    status: vi.fn(),
    departments: vi.fn(),
    employees: vi.fn(),
    localUsers: vi.fn(),
    sync: vi.fn(),
  },
  t: (key: string, opts?: Record<string, unknown>) =>
    opts ? `${key}:${JSON.stringify(opts)}` : key,
}));

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t }) }));
vi.mock("../../api/modules/userGroups", () => ({
  userGroupsApi: {
    audience: audienceMock,
    list: listGroupsMock,
  },
}));
vi.mock("../../api/modules/erpOrg", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../api/modules/erpOrg")>();
  return {
    ...actual,
    erpOrgApi: {
      ...actual.erpOrgApi,
      status: erpMocks.status,
      departments: erpMocks.departments,
      employees: erpMocks.employees,
      localUsers: erpMocks.localUsers,
      sync: erpMocks.sync,
    },
  };
});

import ResourceVisibilityFields, {
  resetResourceVisibilityCachesForTest,
  type ResourceVisibilityMode,
} from "./index";

function setupErpCatalog() {
  erpMocks.status.mockResolvedValue({
    integration_enabled: true,
    company_ids: [10],
    department_count: 2,
    employee_count: 2,
    last_synced_at: 1700000000,
  });
  erpMocks.departments.mockResolvedValue({
    departments: [
      {
        company_id: 10,
        org_id: 100,
        parent_id: 1,
        name: "研发中心",
        org_code: "RD",
        sort: 1,
        status: 1,
      },
      {
        company_id: 10,
        org_id: 101,
        parent_id: 100,
        name: "AI 平台组",
        org_code: "AI",
        sort: 2,
        status: 1,
      },
    ],
    tree: [
      {
        company_id: 10,
        org_id: 100,
        parent_id: 1,
        name: "研发中心",
        org_code: "RD",
        sort: 1,
        status: 1,
        children: [
          {
            company_id: 10,
            org_id: 101,
            parent_id: 100,
            name: "AI 平台组",
            org_code: "AI",
            sort: 2,
            status: 1,
            children: [],
          },
        ],
      },
    ],
  });
  erpMocks.employees.mockResolvedValue({
    employees: [
      {
        company_id: 10,
        employee_id: 501,
        account_user_id: 9001,
        user_id: null,
        has_octop_account: false,
        nick_name: "李未登",
        phone: "13800000001",
        employee_no: "HH-009",
        status: 1,
        is_admin: false,
        org_ids: [100],
        admin_org_ids: [],
        org_names: ["研发中心"],
      },
      {
        company_id: 10,
        employee_id: 502,
        account_user_id: 9002,
        user_id: 2,
        has_octop_account: true,
        nick_name: "张已绑",
        phone: "13800000002",
        employee_no: "HH-010",
        status: 1,
        is_admin: false,
        org_ids: [101],
        admin_org_ids: [],
        org_names: ["AI 平台组"],
      },
    ],
  });
  erpMocks.localUsers.mockResolvedValue({
    users: [
      {
        user_id: 2,
        username: "zhang",
        display_name: "张已绑",
        role: "member",
      },
    ],
  });
  erpMocks.sync.mockResolvedValue({ results: [], summary: {} });
}

beforeEach(() => {
  resetResourceVisibilityCachesForTest();
  vi.clearAllMocks();
  setupErpCatalog();
});

afterEach(() => {
  cleanup();
});

function LegacyHarness() {
  const [vis, setVis] = useState<ResourceVisibilityMode>("owner");
  const [gids, setGids] = useState<string[]>([]);
  return (
    <App>
      <ResourceVisibilityFields
        allowOwner
        visibility={vis}
        groupIds={gids}
        onChange={(next) => {
          setVis(next.visibility);
          setGids(next.groupIds);
        }}
      />
    </App>
  );
}

function ScopedHarness({
  isAdmin,
  initialVisibility = "owner",
  initialScope = emptyScopeSelection(),
  onScopeUpdate,
}: {
  isAdmin: boolean;
  initialVisibility?: ScopedResourceVisibilityMode;
  initialScope?: ResourceScopeSelection;
  onScopeUpdate?: (next: ResourceScopeSelection) => void;
}) {
  const [vis, setVis] =
    useState<ScopedResourceVisibilityMode>(initialVisibility);
  const [gids, setGids] = useState<string[]>([]);
  const [scope, setScope] = useState<ResourceScopeSelection>(initialScope);
  return (
    <App>
      <ResourceVisibilityFields
        allowOwner
        enableErpScope
        isAdmin={isAdmin}
        visibility={vis}
        groupIds={gids}
        scope={scope}
        onChange={(next) => {
          setVis(next.visibility);
          setGids(next.groupIds);
        }}
        onScopeChange={(next) => {
          setScope(next);
          onScopeUpdate?.(next);
        }}
      />
    </App>
  );
}

describe("ResourceVisibilityFields", () => {
  it("keeps legacy behavior when enableErpScope is false", async () => {
    audienceMock.mockResolvedValue({ user_count: 3, group_count: 1 });
    listGroupsMock.mockResolvedValue([
      {
        user_group_id: "g1",
        name: "Ops",
        description: null,
        enabled: true,
        member_count: 2,
        created_at: 1,
        updated_at: 1,
      },
    ]);

    render(<LegacyHarness />);
    expect(
      await screen.findByRole("radio", { name: "resourceVisibility.all" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: "resourceVisibility.company" }),
    ).toBeNull();
    expect(
      screen.queryByRole("radio", { name: "resourceVisibility.scoped" }),
    ).toBeNull();
    expect(erpMocks.status).not.toHaveBeenCalled();
    expect(erpMocks.departments).not.toHaveBeenCalled();
  });

  it("hides legacy picker when only one user account exists", async () => {
    audienceMock.mockResolvedValue({ user_count: 1, group_count: 0 });
    render(<LegacyHarness />);
    await waitFor(() => expect(audienceMock).toHaveBeenCalledOnce());
    expect(screen.queryByRole("radio")).toBeNull();
    expect(erpMocks.status).not.toHaveBeenCalled();
  });

  it("shows ERP scope options for non-admin users when enableErpScope is true and displays department counts", async () => {
    audienceMock.mockResolvedValue({ user_count: 3, group_count: 0 });
    render(<ScopedHarness isAdmin={false} />);
    expect(
      await screen.findByRole("radio", { name: "resourceVisibility.owner" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "resourceVisibility.company" }),
    ).toBeInTheDocument();
    const scopedRadio = screen.getByRole("radio", {
      name: "resourceVisibility.scoped",
    });
    expect(scopedRadio).toBeInTheDocument();

    fireEvent.click(scopedRadio);
    await waitFor(() => expect(erpMocks.departments).toHaveBeenCalledOnce());
    expect(await screen.findByText("研发中心")).toBeInTheDocument();
    expect(screen.getByText("AI 平台组")).toBeInTheDocument();
    expect(screen.getByText("(2)")).toBeInTheDocument();
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("shows ERP scope panel for admins even with userCount === 1, renders pre-login badge, toggles dept children, and syncs", async () => {
    audienceMock.mockResolvedValue({ user_count: 1, group_count: 0 });
    const onScopeUpdate = vi.fn();
    const initialScope: ResourceScopeSelection = {
      ...emptyScopeSelection(),
      companyId: 10,
      departmentGrants: [
        { company_id: 10, org_id: 100, include_children: true },
      ],
      erpEmployees: [{ company_id: 10, employee_id: 501 }],
    };

    render(
      <ScopedHarness
        isAdmin
        initialVisibility="owner"
        initialScope={initialScope}
        onScopeUpdate={onScopeUpdate}
      />,
    );

    const scopedRadio = await screen.findByRole("radio", {
      name: "resourceVisibility.scoped",
    });
    expect(scopedRadio).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "resourceVisibility.company" }),
    ).toBeInTheDocument();
    expect(erpMocks.status).not.toHaveBeenCalled();

    fireEvent.click(scopedRadio);
    await waitFor(() => expect(erpMocks.departments).toHaveBeenCalledOnce());
    expect((await screen.findAllByText("研发中心")).length).toBeGreaterThanOrEqual(
      1,
    );

    // Selected pre-login employee displays the pre-login grant badge
    expect(
      await screen.findByText("resourceVisibility.preLoginBadge"),
    ).toBeInTheDocument();

    // Toggle include_children switch on 研发中心
    const deptSwitch = screen.getByRole("switch");
    fireEvent.click(deptSwitch);
    expect(onScopeUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        departmentGrants: [
          { company_id: 10, org_id: 100, include_children: false },
        ],
      }),
    );

    // Open Deny overrides section
    fireEvent.click(
      screen.getByRole("button", { name: /resourceVisibility\.showDenyPanel/ }),
    );
    expect(
      await screen.findByText("resourceVisibility.denyHint"),
    ).toBeInTheDocument();

    // Trigger manual ERP sync
    fireEvent.click(
      screen.getByRole("button", { name: /resourceVisibility\.erpSyncButton/ }),
    );
    await waitFor(() => expect(erpMocks.sync).toHaveBeenCalledWith(10));
    await waitFor(() => expect(erpMocks.departments).toHaveBeenCalledTimes(2));
  });
});
