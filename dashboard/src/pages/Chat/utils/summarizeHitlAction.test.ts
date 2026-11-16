import { describe, expect, it } from "vitest";
import zh from "../../../locales/zh.json";
import { summarizeHitlAction, type HitlTranslate } from "./summarizeHitlAction";

function lookup(bundle: unknown, key: string): string | undefined {
  let node: unknown = bundle;
  for (const part of key.split(".")) {
    if (!node || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

function interpolate(template: string, vars: Record<string, unknown>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) =>
    name in vars ? String(vars[name]) : `{{${name}}}`,
  );
}

/** Mimic i18next: missing keys return the key; `{{name}}` is interpolated. */
function tFrom(bundle: unknown): HitlTranslate {
  return (key, options) => {
    const vars =
      options && typeof options === "object"
        ? (options as Record<string, unknown>)
        : {};
    const fallback = typeof options === "string" ? options : undefined;
    const found = lookup(bundle, key);
    if (found === undefined) return fallback ?? key;
    return interpolate(found, vars);
  };
}

const tZh = tFrom(zh);

describe("summarizeHitlAction", () => {
  it("explains browser_use dom_tree in Chinese instead of dumping JSON", () => {
    const view = summarizeHitlAction(
      "browser_use",
      { action: "dom_tree", level: "interactive" },
      tZh,
      "使用浏览器",
    );

    expect(view.toolLabel).toBe("使用浏览器");
    expect(view.summary).toBe("获取当前网页的可交互元素结构");
    expect(view.rows).toEqual([
      { label: "操作", value: "获取网页结构" },
      { label: "范围", value: "仅交互元素" },
    ]);
    const blob = [view.summary, ...view.rows.map((row) => row.value)].join(" ");
    expect(blob).not.toContain('"action"');
    expect(blob).not.toContain("{");
  });

  it("puts the target URL into the navigate summary", () => {
    const view = summarizeHitlAction(
      "browser_use",
      { action: "navigate", url: "https://news.example.com" },
      tZh,
      "使用浏览器",
    );

    expect(view.summary).toBe("打开网页 https://news.example.com");
    expect(view.rows).toEqual([
      { label: "操作", value: "打开网页" },
      { label: "网址", value: "https://news.example.com", mono: true },
    ]);
  });

  it("explains a shell command instead of wrapping it in JSON", () => {
    const view = summarizeHitlAction(
      "execute",
      { command: "ls -la inbound" },
      tZh,
      "执行指令",
    );

    expect(view.summary).toBe("执行命令：ls -la inbound");
    expect(view.rows).toEqual([
      { label: "命令", value: "ls -la inbound", mono: true },
    ]);
  });

  it("prefers the action description when the model already explained it", () => {
    const view = summarizeHitlAction(
      "custom_plugin_tool",
      { foo_bar: "secret.txt" },
      tZh,
      "custom_plugin_tool",
      "读取工作区里的密钥文件",
    );

    expect(view.summary).toBe("读取工作区里的密钥文件");
    expect(view.rows).toEqual([{ label: "Foo bar", value: "secret.txt" }]);
  });

  it("ignores the English HITL template and explains write_file in Chinese", () => {
    const view = summarizeHitlAction(
      "write_file",
      {
        file_path: "/.octop/workspaces/J7Y3TW/test.txt",
        content: "Hello, this is a test file!\nCreated at: 2025-01-25\n",
      },
      tZh,
      "写入文件",
      "Tool execution requires approval Tool: write_file Args: {'file_path': '/.octop/workspaces/J7Y3TW/test.txt', 'content': 'Hello, this is a test file!\\nCreated at: 2025-01-25\\n'}",
    );

    expect(view.summary).toBe("写入文件 /.octop/workspaces/J7Y3TW/test.txt");
    expect(view.summary).not.toContain("Tool execution");
    expect(view.rows).toEqual([
      {
        label: "文件路径",
        value: "/.octop/workspaces/J7Y3TW/test.txt",
        mono: true,
      },
      {
        label: "内容",
        value: "Hello, this is a test file!\nCreated at: 2025-01-25\n",
      },
    ]);
  });

  it("omits server-owned profile and skips empty args", () => {
    const view = summarizeHitlAction(
      "browser_use",
      { action: "screenshot", profile: "user-7" },
      tZh,
      "使用浏览器",
    );

    expect(view.summary).toBe("截取当前网页截图");
    expect(view.rows.map((row) => row.label)).toEqual(["操作"]);
    expect(view.rows.some((row) => row.value === "user-7")).toBe(false);
  });

  it("formats MCN write tools cleanly and hides confirm/confirm_summary rows", () => {
    const quoteView = summarizeHitlAction(
      "mcn_youli_create_package_quote",
      {
        name: "茶颜3月抖音报价",
        customer_id: "cust-1",
        expected_amount: 12800,
        confirm: true,
        confirm_summary: "为客户「茶颜悦色」创建拉包报价，应付 ¥12800",
      },
      tZh,
      "mcn_youli_create_package_quote",
    );

    expect(quoteView.toolLabel).toBe("创建拉包报价单");
    expect(quoteView.summary).toBe(
      "为客户「茶颜悦色」创建拉包报价，应付 ¥12800",
    );
    expect(quoteView.rows.map((r) => r.label)).toEqual([
      "名称",
      "客户 ID",
      "试算应付金额",
    ]);

    const orderView = summarizeHitlAction(
      "mcn_youli_create_order_from_quote",
      {
        quote_id: "q-99",
        commit_type: 1,
        confirm: true,
      },
      tZh,
      "mcn_youli_create_order_from_quote",
    );

    expect(orderView.toolLabel).toBe("从报价单创建销售订单");
    expect(orderView.summary).toBe("基于报价单 q-99 创建销售订单");
    expect(orderView.rows).toEqual([
      { label: "报价单 ID", value: "q-99" },
      { label: "下单类型", value: "水下单" },
    ]);
  });
});
