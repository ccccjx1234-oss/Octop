import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkProgressPage from "./WorkProgress";
import DeliverablesPage from "./Deliverables";
import DataDashboardPage from "./DataDashboard";
import ChatDeliveryCard from "../Chat/components/ChatDeliveryCard";

const mocks = vi.hoisted(() => ({
  getProgress: vi.fn(),
  listDeliverables: vi.fn(),
  getDashboard: vi.fn(),
  createDeliverable: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
  deleteTask: vi.fn(),
  syncMcn: vi.fn(),
  msgError: vi.fn(),
  msgSuccess: vi.fn(),
  msgWarning: vi.fn(),
  msgInfo: vi.fn(),
}));

vi.mock("@/utils/antdMessage", () => ({
  message: {
    error: mocks.msgError,
    success: mocks.msgSuccess,
    warning: mocks.msgWarning,
    info: mocks.msgInfo,
  },
}));

vi.mock("../../api/modules/workbench", () => ({
  isAbortError: (err: unknown) =>
    err instanceof Error && err.name === "AbortError",
  workbenchErrorMessage: (err: unknown, fallback: string) =>
    err instanceof Error && err.message
      ? `${fallback}：${err.message}`
      : fallback,
  workbenchApi: {
    getProgress: mocks.getProgress,
    listDeliverables: mocks.listDeliverables,
    getDashboard: mocks.getDashboard,
    createDeliverable: mocks.createDeliverable,
    updateTask: mocks.updateTask,
    deleteTask: mocks.deleteTask,
    syncMcn: mocks.syncMcn,
    createTask: mocks.createTask,
    updateDeliverable: vi.fn(),
    deleteDeliverable: vi.fn(),
    updateGoal: vi.fn(),
  },
}));

const ROLES = [
  {
    key: "business",
    label: "商务",
    workspace_title: "商务工作台",
    target_type: "客户",
    goal_tagline: "聚焦商机",
  },
  {
    key: "customer_service",
    label: "客户服务",
    workspace_title: "客户服务工作台",
    target_type: "客户",
    goal_tagline: "实时同步 MCN 订单、报价与项目交付节点。",
  },
];

describe("Workbench UI pages & ChatDeliveryCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getProgress.mockResolvedValue({
      role: ROLES[1],
      roles: ROLES,
      range: "week",
      mcn_connected: true,
      data_sources: ["MCN 订单", "MCN 全案子项目"],
      kpis: {
        core_work: 2,
        in_progress: 1,
        delivered_this_week: 3,
        overall_completion: 72,
      },
      modules: [
        {
          module_key: "project_progress",
          module_title: "项目进度",
          target_type: "客户",
          task_count: 1,
          items: [
            {
              id: -1001,
              role_key: "customer_service",
              module_key: "project_progress",
              module_title: "项目进度",
              target_type: "客户",
              target_name: "茶颜",
              title: "[订单] 茶颜双十一投放执行单",
              due_label: "今天 10:30",
              status: "in_progress",
              progress: 72,
              source: "mcn",
              source_label: "MCN ERP",
              readonly: true,
              external_ref: "mcn:order:9001",
              meta: { orderNo: "ORD-9001" },
              created_at: 1,
              updated_at: 1,
            },
          ],
        },
        {
          module_key: "quote_order",
          module_title: "报价下单",
          target_type: "客户",
          task_count: 0,
          items: [],
        },
      ],
    });
    mocks.listDeliverables.mockResolvedValue({
      mcn_connected: true,
      kpis: { total: 1, pending_review: 1, approved: 0, added_this_week: 1 },
      items: [
        {
          id: 10,
          role_key: "customer_service",
          deliverable_type: "document",
          target_name: "超级飞侠",
          title: "十周年品牌合作策划方案",
          summary: "围绕新品上市节点制定的内容传播方案",
          content: "方案内容",
          review_status: "approved",
          source: "chat",
          source_label: "对话交付",
          readonly: false,
          source_thread_id: "thread-9",
          source_message_id: "msg-9",
          linked_task_id: null,
          delivered_at: 1,
          delivered_label: "今天 10:32",
          created_at: 1,
          updated_at: 1,
        },
      ],
    });
    mocks.getDashboard.mockResolvedValue({
      role: {
        key: "business",
        label: "商务",
        workspace_title: "商务工作台",
        badge: "商务实绩",
      },
      roles: ROLES,
      range: "7d",
      mcn_connected: true,
      data_sources: ["MCN 经营概览", "MCN 商机与报价"],
      trend_metric_label: "商单与报价数",
      kpis: [
        {
          label: "累计成单金额",
          value: "¥ 1,327,353",
          delta: "MCN 经营概览实时",
        },
        { label: "有效合作客户", value: "5", delta: "本月新增 2" },
        { label: "待办待完结", value: "8", delta: "MCN 工作台待办" },
        { label: "订单利润率", value: "31.6%", delta: "回款 ¥ 500,000" },
      ],
      trend: [
        { label: "11-08", value: 1 },
        { label: "11-09", value: 2 },
        { label: "11-10", value: 0 },
        { label: "11-11", value: 3 },
        { label: "11-12", value: 1 },
        { label: "11-13", value: 4 },
        { label: "11-14", value: 2 },
      ],
      goals: [
        {
          id: 1,
          role_key: "business",
          title: "订单完结率",
          target_value: 100,
          current_value: 82,
          unit: "%",
          period: "month",
          percent: 82,
          updated_at: 1,
        },
      ],
    });
    mocks.createDeliverable.mockResolvedValue({
      deliverable: { id: 99 },
      progress_synced: true,
      updated_task: null,
      updated: false,
    });
    mocks.createTask.mockResolvedValue({ id: 201 });
  });

  it("renders WorkProgress live ERP items, empty module hint, and ERP detail modal", async () => {
    const user = userEvent.setup();
    render(<WorkProgressPage />);
    await waitFor(() => expect(screen.getByText("茶颜")).toBeInTheDocument());
    expect(screen.getAllByText("72%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/已直连：MCN 订单/)).toBeInTheDocument();
    expect(screen.getByText(/当前暂无任务/)).toBeInTheDocument();

    await user.click(screen.getByText("茶颜"));
    await waitFor(() =>
      expect(screen.getByText(/ERP \/ 外部系统明细/)).toBeInTheDocument(),
    );
    await user.click(
      screen.getByRole("button", { name: "转为本地可编辑任务" }),
    );
    await waitFor(() => expect(mocks.createTask).toHaveBeenCalled());
  });

  it("renders Deliverables cards and passes search + status params", async () => {
    const user = userEvent.setup();
    render(<DeliverablesPage />);
    await waitFor(() =>
      expect(screen.getByText("十周年品牌合作策划方案")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByPlaceholderText("搜索交付对象或成果名称"),
      "超级飞侠",
    );
    await waitFor(() =>
      expect(mocks.listDeliverables).toHaveBeenCalledWith(
        {
          type: "all",
          review_status: "all",
          q: "超级飞侠",
        },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });

  it("renders DataDashboard live MCN KPIs, trend, and monthly goals", async () => {
    render(<DataDashboardPage />);
    await waitFor(() =>
      expect(screen.getByText("¥ 1,327,353")).toBeInTheDocument(),
    );
    expect(screen.getByText(/订单完结率/)).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
  });

  it("infers document for proposal with markdown table, passes threadId, and surfaces error details", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <ChatDeliveryCard
          threadId="thread-abc"
          messageId="msg-1"
          answerText={
            "# 青禾科技合作方案 (V1.0)\n| 指标 | 首年目标 |\n| --- | --- |\n| 签约额 | 1200万 |\n请按 Word 方案输出。"
          }
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("对话交付")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /保存为交付成果/i }));
    await waitFor(() => expect(mocks.createDeliverable).toHaveBeenCalled());
    expect(mocks.createDeliverable).toHaveBeenCalledWith(
      expect.objectContaining({
        deliverable_type: "document",
        source_thread_id: "thread-abc",
        source_message_id: "msg-1",
      }),
    );

    expect(screen.getByRole("button", { name: "已保存" })).toBeDisabled();
    await user.type(screen.getByPlaceholderText("成果标题"), "修订");
    mocks.createDeliverable.mockRejectedValueOnce(
      new Error("HTTP 503: 后端忙"),
    );
    await user.click(screen.getByRole("button", { name: /保存为交付成果/i }));
    await waitFor(() =>
      expect(mocks.msgError).toHaveBeenCalledWith(
        "保存交付成果失败：HTTP 503: 后端忙",
      ),
    );
  });

  it("hides ChatDeliveryCard on model identity Q&A, file lists, receipts, and tool queries, while keeping collapsed trigger for real candidates", async () => {
    const user = userEvent.setup();
    const irrelevantReplies: Array<{ text: string; toolNames?: string[] }> = [
      { text: "到点啦，该吃东西了 🥪 别忘了好好吃一顿。" },
      {
        text: "已为你设置每周二 12:00 吃东西提醒：\n- **时间**：每周二 12:00\n- **内容**：「到点啦，该吃东西了 🥪 别忘了好好吃一顿。」",
      },
      {
        text: "你好！很高兴为你服务，你可以随时让我查客户、看报价或安排日程。",
      },
      {
        // Exact model identity answer from live screenshot
        text: "我运行在 **Octop harness** 里的通用 AI Agent，底层的具体模型标识（厂商、版本号）运行时并没有直接暴露给我，所以我没法凭「自我感知」给你一个可靠的型号——任何这类自报都容易编。\n\n能确定的是：\n- 记忆：受结构化记忆规则约束，长期信息写进 `USER.md` / `MEMORY.md`。\n- 不知道的：参数规模、训练数据截止日、具体版本号。\n\n如果你需要确切型号，最靠谱的办法是看运行这套 Octop 的服务端配置或启动日志——需要我去工作区或环境变量里找找看吗？",
      },
      {
        // Markdown heading + table for model/config explanation must NOT trigger deliverable card
        text: "### 模型信息说明\n| 维度 | 当前状态 |\n| --- | --- |\n| 运行环境 | Octop harness |\n| 具体型号 | 未暴露 |",
      },
      {
        // Workspace file listing mentioning .xlsx/.pdf/.docx must NOT trigger deliverable card
        text: "你的工作区中有以下文件：\n- 客户报价单.xlsx (12 KB)\n- 合作方案.docx (320 KB)\n- 品牌介绍.pdf (1.4 MB)",
        toolNames: ["ls"],
      },
      {
        // MCN quote/order creation receipt must NOT trigger deliverable card
        text: "瑞幸咖啡的报价单已创建并保存（报价单号 BJ202611160001），总金额 ¥18,000，已同步到 MCN ERP。",
        toolNames: ["mcn_youli_create_package_quote"],
      },
      {
        // Clarifying question mentioning deliverable keywords must NOT trigger deliverable card
        text: "好的，制作报价单前请先提供以下信息：\n1. 目标客户名称\n2. 投放平台与预算范围\n3. 期望交付时间",
      },
    ];
    for (const item of irrelevantReplies) {
      const { container, unmount } = render(
        <MemoryRouter>
          <ChatDeliveryCard
            threadId="thread-chat"
            messageId="msg-irrelevant"
            answerText={item.text}
            toolNames={item.toolNames}
          />
        </MemoryRouter>,
      );
      expect(container.firstChild).toBeNull();
      unmount();
    }

    const mediumCandidate =
      "针对青禾科技本季度抖音达人投放的报价方案说明如下，目前已完成达人筛选、档期确认与成本核算，整体预算控制在客户预期范围内并预留了返点空间，以下为执行阶段建议与交付清单明细，如需调整账号组合可随时告知。";
    render(
      <MemoryRouter>
        <ChatDeliveryCard
          threadId="thread-work"
          messageId="msg-candidate"
          answerText={mediumCandidate}
        />
      </MemoryRouter>,
    );
    const trigger = screen.getByRole("button", { name: /整理为对话交付成果/i });
    expect(trigger).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /保存为交付成果/i }),
    ).toBeNull();
    await user.click(trigger);
    expect(
      screen.getByRole("button", { name: /保存为交付成果/i }),
    ).toBeInTheDocument();
  });

  it("ignores stale out-of-order WorkProgress responses during fast role switching", async () => {
    const user = userEvent.setup();
    let resolveSlow!: (v: unknown) => void;
    const slowPromise = new Promise((r) => {
      resolveSlow = r;
    });
    const baseKpis = {
      core_work: 1,
      in_progress: 1,
      delivered_this_week: 0,
      overall_completion: 50,
    };

    let csCalls = 0;
    mocks.getProgress.mockImplementation(async (role: string) => {
      if (role === "business") {
        return slowPromise;
      }
      csCalls += 1;
      if (csCalls === 1) {
        return {
          role: ROLES[1],
          roles: ROLES,
          mcn_connected: true,
          data_sources: ["MCN 订单"],
          range: "week",
          kpis: baseKpis,
          modules: [],
        };
      }
      return {
        role: ROLES[1],
        roles: ROLES,
        mcn_connected: true,
        data_sources: ["MCN 订单"],
        range: "week",
        kpis: baseKpis,
        modules: [
          {
            module_key: "script_pack",
            module_title: "客服最新模块",
            target_type: "客户",
            task_count: 1,
            items: [
              {
                id: -99,
                role_key: "customer_service",
                module_key: "script_pack",
                module_title: "客服最新模块",
                target_type: "客户",
                target_name: "最新目标客户",
                title: "应该保留的最新角色任务",
                due_label: "实时",
                progress: 60,
                status: "in_progress",
                source: "mcn",
                readonly: true,
                external_ref: "mcn:s:99",
                created_at: 2,
                updated_at: 2,
              },
            ],
          },
        ],
      };
    });

    render(<WorkProgressPage />);
    const bizTab = await screen.findByText("商务");

    await user.click(bizTab);
    await waitFor(() =>
      expect(mocks.getProgress).toHaveBeenCalledWith(
        "business",
        "week",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    await user.click(screen.getByText("客户服务"));

    await waitFor(() =>
      expect(screen.getByText("最新目标客户")).toBeInTheDocument(),
    );

    resolveSlow({
      role: ROLES[0],
      roles: ROLES,
      mcn_connected: true,
      data_sources: [],
      range: "week",
      kpis: baseKpis,
      modules: [
        {
          module_key: "old",
          module_title: "过期模块",
          target_type: "客户",
          task_count: 1,
          items: [
            {
              id: -88,
              role_key: "business",
              module_key: "old",
              module_title: "过期模块",
              target_type: "客户",
              target_name: "过期任务不应该出现",
              title: "过期返回",
              due_label: "实时",
              progress: 10,
              status: "in_progress",
              source: "mcn",
              readonly: true,
              external_ref: "mcn:old:88",
              created_at: 1,
              updated_at: 1,
            },
          ],
        },
      ],
    });

    await waitFor(() => {
      expect(screen.getByText("最新目标客户")).toBeInTheDocument();
      expect(screen.queryByText("过期任务不应该出现")).not.toBeInTheDocument();
    });
  });

  it("does not retry a write after an uncertain gateway failure", async () => {
    vi.stubGlobal("BASE_URL", "");
    const actual = await vi.importActual<
      typeof import("../../api/modules/workbench")
    >("../../api/modules/workbench");
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response("Bad Gateway", { status: 502, statusText: "Bad Gateway" }),
      );

    try {
      await expect(
        actual.workbenchApi.createTask({
          role_key: "business",
          module_key: "contract_sign",
          target_name: "测试",
          title: "重试创建任务",
        }),
      ).rejects.toThrow("操作结果暂不确定");
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(
        actual.workbenchErrorMessage(
          new Error("Request failed: 502 Bad Gateway"),
          "保存失败",
        ),
      ).toBe("保存失败：Bad Gateway");
    } finally {
      fetchSpy.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("retries a read after a transient gateway failure", async () => {
    vi.stubGlobal("BASE_URL", "");
    const actual = await vi.importActual<
      typeof import("../../api/modules/workbench")
    >("../../api/modules/workbench");
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("Bad Gateway", { status: 502 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    try {
      await actual.workbenchApi.listDeliverables();
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    } finally {
      fetchSpy.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("caches GET responses for SWR switching, clears cache on mutation, and honors caller AbortSignal", async () => {
    vi.stubGlobal("BASE_URL", "");
    const actual = await vi.importActual<
      typeof import("../../api/modules/workbench")
    >("../../api/modules/workbench");
    actual.invalidateWorkbenchCache();

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ role: { key: "planning" }, kpis: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    try {
      await actual.workbenchApi.getDashboard("planning", "7d");
      expect(
        actual.workbenchApi.getCachedDashboard("planning", "7d"),
      ).toMatchObject({ role: { key: "planning" } });

      await actual.workbenchApi.syncMcn("planning");
      expect(
        actual.workbenchApi.getCachedDashboard("planning", "7d"),
      ).toBeNull();

      const ac = new AbortController();
      ac.abort();
      await expect(
        actual.workbenchApi.getDashboard("planning", "7d", {
          signal: ac.signal,
        }),
      ).rejects.toMatchObject({ name: "AbortError" });
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    } finally {
      fetchSpy.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("isolates and clears workbench response cache across auth token changes and updates KPI label per range", async () => {
    vi.stubGlobal("BASE_URL", "");
    const actual = await vi.importActual<
      typeof import("../../api/modules/workbench")
    >("../../api/modules/workbench");
    const reqMod = await vi.importActual<typeof import("../../api/request")>(
      "../../api/request",
    );
    reqMod.clearAuthToken();
    actual.invalidateWorkbenchCache();

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ kpis: { core_work: 3 } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    try {
      reqMod.setAuthToken("token-user-a");
      await actual.workbenchApi.getProgress("customer_service", "week");
      expect(
        actual.workbenchApi.getCachedProgress("customer_service", "week"),
      ).toMatchObject({ kpis: { core_work: 3 } });

      reqMod.setAuthToken("token-user-b");
      expect(
        actual.workbenchApi.getCachedProgress("customer_service", "week"),
      ).toBeNull();
    } finally {
      reqMod.clearAuthToken();
      actual.invalidateWorkbenchCache();
      fetchSpy.mockRestore();
      vi.unstubAllGlobals();
    }

    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <WorkProgressPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("本周已交付 / 完成")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "今日重点" }));
    expect(await screen.findByText("今日已交付 / 完成")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "本月全量" }));
    expect(await screen.findByText("本月已交付 / 完成")).toBeInTheDocument();
  });
});
