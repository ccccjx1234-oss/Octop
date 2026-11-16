/**
 * Turn a HITL tool-call into a short explanation plus labelled rows.
 * Approval cards should never dump raw JSON as the primary view.
 */

export interface HitlArgRow {
  label: string;
  value: string;
  mono?: boolean;
}

export interface HitlActionView {
  toolLabel: string;
  summary: string;
  rows: HitlArgRow[];
}

export type HitlTranslate = (
  key: string,
  options?: string | Record<string, unknown>,
) => string;

const ALWAYS_HIDDEN = new Set([
  "session_id",
  "profile",
  "confirm",
  "confirm_summary",
]);
const TRUNCATE_KEYS = new Set([
  "content",
  "body",
  "html",
  "text",
  "old_string",
  "new_string",
  "code",
  "script",
  "payload",
]);
const BROWSER_TOOLS = new Set(["browser_use", "browser_control"]);
const SHELL_TOOLS = new Set(["execute", "bash", "run_terminal_cmd"]);
const MCN_WRITE_TOOLS = new Set([
  "youli_save_quote",
  "youli_create_package_quote",
  "youli_create_order_from_quote",
  "youli_create_combined_order",
  "youli_update_publish_link",
  "youli_export_delivery",
  "youli_create_project",
  "youli_create_sub_project",
  "youli_create_coupon_version",
  "youli_create_contract",
]);

function extractMcnTool(name: string): string | undefined {
  if (MCN_WRITE_TOOLS.has(name)) return name;
  for (const candidate of MCN_WRITE_TOOLS) {
    if (name.endsWith(`_${candidate}`)) return candidate;
  }
  return undefined;
}
const MONO_KEYS = new Set([
  "command",
  "cmd",
  "path",
  "file",
  "file_path",
  "url",
  "code",
  "script",
  "selector",
  "pattern",
]);
const MAX_TRUNCATED = 280;
const MAX_HARD = 2000;

const MACHINE_HITL_DESC = /tool execution requires approval/i;
const TOOL_ARGS_DUMP = /\bTool:\s+\S[\s\S]*\bArgs:\s*[{'"]/;

/** True when harness/langgraph stuffed a raw English args dump into ``description``. */
export function isMachineHitlDescription(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  return MACHINE_HITL_DESC.test(trimmed) || TOOL_ARGS_DUMP.test(trimmed);
}

function resolve(
  t: HitlTranslate,
  key: string,
  fallback: string,
  vars?: Record<string, unknown>,
): string {
  const result = vars
    ? t(key, { defaultValue: fallback, ...vars })
    : t(key, fallback);
  if (!result || result === key) return fallback;
  return result;
}

export function humanizeArgKey(key: string): string {
  const cleaned = key.replace(/[_-]+/g, " ").trim();
  if (!cleaned) return key;
  return cleaned[0].toUpperCase() + cleaned.slice(1);
}

function isEmpty(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}

function shouldHide(key: string, value: unknown): boolean {
  if (ALWAYS_HIDDEN.has(key) || isEmpty(value)) return true;
  return false;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

function formatPlain(value: unknown, t: HitlTranslate): string {
  if (typeof value === "boolean") {
    return resolve(
      t,
      value ? "chat.hitl.bool.true" : "chat.hitl.bool.false",
      value ? "Yes" : "No",
    );
  }
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .filter((item) => !isEmpty(item))
      .map((item) => formatPlain(item, t))
      .join(", ");
  }
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .filter(([k, v]) => !shouldHide(k, v))
      .map(([k, v]) => `${humanizeArgKey(k)}: ${formatPlain(v, t)}`)
      .join("; ");
  }
  return String(value);
}

function formatKnownValue(
  toolName: string,
  key: string,
  value: unknown,
  t: HitlTranslate,
): string | undefined {
  const mcnTool = extractMcnTool(toolName);
  if (!mcnTool) return undefined;
  const code =
    typeof value === "number" || typeof value === "string"
      ? String(value).trim()
      : "";
  if (!code) return undefined;
  if (key === "commit_type") {
    return resolve(t, `chat.hitl.mcn.commitTypes.${code}`, code);
  }
  if (key === "type" && mcnTool === "youli_create_contract") {
    return resolve(t, `chat.hitl.mcn.contractTypes.${code}`, code);
  }
  if (key === "mode" && mcnTool === "youli_export_delivery") {
    return resolve(t, `chat.hitl.mcn.exportModes.${code}`, code);
  }
  if (key === "main_platform") {
    return resolve(t, `chat.hitl.mcn.platforms.${code}`, code);
  }
  if (key === "enable_type" && mcnTool === "youli_create_contract") {
    return resolve(t, `chat.hitl.mcn.enableTypes.${code}`, code);
  }
  return undefined;
}

function formatKnownString(
  toolName: string,
  key: string,
  raw: string,
  t: HitlTranslate,
): string {
  const mcnValue = formatKnownValue(toolName, key, raw, t);
  if (mcnValue !== undefined) return mcnValue;
  if (BROWSER_TOOLS.has(toolName) || key === "action") {
    if (key === "action") {
      return resolve(t, `chat.hitl.browser.actions.${raw}`, raw);
    }
    if (key === "level") {
      return resolve(t, `chat.hitl.browser.levels.${raw}`, raw);
    }
    if (key === "direction") {
      return resolve(t, `chat.hitl.browser.directions.${raw}`, raw);
    }
  }
  return raw;
}

function formatArgValue(
  toolName: string,
  key: string,
  value: unknown,
  t: HitlTranslate,
): string {
  const mcnValue = formatKnownValue(toolName, key, value, t);
  if (mcnValue !== undefined) {
    return truncate(mcnValue, MAX_HARD);
  }
  if (typeof value === "string") {
    const labelled = formatKnownString(toolName, key, value, t);
    const max = TRUNCATE_KEYS.has(key) ? MAX_TRUNCATED : MAX_HARD;
    return truncate(labelled, max);
  }
  const plain = formatPlain(value, t);
  const max = TRUNCATE_KEYS.has(key) ? MAX_TRUNCATED : MAX_HARD;
  return truncate(plain, max);
}

function argLabel(key: string, t: HitlTranslate): string {
  return resolve(t, `chat.hitl.args.${key}`, humanizeArgKey(key));
}

function stringArg(
  args: Record<string, unknown>,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function browserSummary(
  args: Record<string, unknown>,
  t: HitlTranslate,
  toolLabel: string,
): string {
  const action = stringArg(args, "action") ?? "";
  const url = stringArg(args, "url");
  const level = stringArg(args, "level");
  if ((action === "navigate" || action === "open") && url) {
    return resolve(t, "chat.hitl.summaries.openUrl", `Open ${url}`, { url });
  }
  if (action === "new_tab" && url) {
    return resolve(t, "chat.hitl.summaries.openUrl", `Open ${url}`, { url });
  }
  if (action === "screenshot") {
    return resolve(
      t,
      "chat.hitl.summaries.screenshot",
      "Take a screenshot of the current page",
    );
  }
  if (action === "dom_tree" && level === "interactive") {
    return resolve(
      t,
      "chat.hitl.summaries.domTreeInteractive",
      "Read the interactive structure of the current page",
    );
  }
  if (action === "dom_tree") {
    return resolve(
      t,
      "chat.hitl.summaries.domTree",
      "Read the structure of the current page",
    );
  }
  if (action) {
    return resolve(t, `chat.hitl.browser.actions.${action}`, toolLabel);
  }
  return toolLabel;
}

function mcnSummary(
  mcnTool: string,
  args: Record<string, unknown>,
  t: HitlTranslate,
  resolvedToolLabel: string,
): string {
  const explicit = stringArg(args, "confirm_summary");
  if (explicit) return explicit;
  const name = stringArg(args, "name", "project_name");
  const quoteId = stringArg(args, "quote_id");
  const amount = args.expected_amount;
  if (mcnTool === "youli_create_package_quote" && name) {
    if (amount !== undefined && amount !== null && String(amount).trim()) {
      return resolve(
        t,
        "chat.hitl.mcn.summaries.createPackageQuoteWithAmount",
        `Create quote "${name}" (${String(amount)})`,
        { name, amount: String(amount) },
      );
    }
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createPackageQuote",
      `Create quote "${name}"`,
      { name },
    );
  }
  if (mcnTool === "youli_create_order_from_quote" && quoteId) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createOrder",
      `Create order from quote ${quoteId}`,
      { quoteId },
    );
  }
  if (mcnTool === "youli_create_combined_order" && quoteId) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createCombinedOrder",
      `Create combined orders from quote ${quoteId}`,
      { quoteId },
    );
  }
  const publishLink = stringArg(args, "publish_link");
  if (mcnTool === "youli_update_publish_link" && publishLink) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.updatePublishLink",
      `Update publish link: ${publishLink}`,
      { link: publishLink },
    );
  }
  if (mcnTool === "youli_export_delivery") {
    const project = stringArg(args, "project_name");
    if (project) {
      return resolve(
        t,
        "chat.hitl.mcn.summaries.exportDeliveryProject",
        `Export delivery data for ${project}`,
        { project },
      );
    }
    const mode = stringArg(args, "mode");
    if (mode) {
      const modeLabel = resolve(t, `chat.hitl.mcn.exportModes.${mode}`, mode);
      return resolve(
        t,
        "chat.hitl.mcn.summaries.exportDeliveryMode",
        `Export delivery data (${modeLabel})`,
        { mode: modeLabel },
      );
    }
  }
  if (mcnTool === "youli_create_project" && name) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createProject",
      `Create project "${name}"`,
      { name },
    );
  }
  if (mcnTool === "youli_create_sub_project" && name) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createSubProject",
      `Create sub-project "${name}"`,
      { name },
    );
  }
  if (mcnTool === "youli_create_coupon_version" && name) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createCouponVersion",
      `Create coupon policy "${name}"`,
      { name },
    );
  }
  if (mcnTool === "youli_create_contract" && name) {
    return resolve(
      t,
      "chat.hitl.mcn.summaries.createContract",
      `Create contract "${name}"`,
      { name },
    );
  }
  return resolvedToolLabel;
}

function buildSummary(
  name: string,
  args: Record<string, unknown>,
  t: HitlTranslate,
  toolLabel: string,
  description?: string,
): string {
  const mcnTool = extractMcnTool(name);
  const confirmSummary = stringArg(args, "confirm_summary");
  if (confirmSummary) return confirmSummary;
  const explained = description?.trim();
  if (explained && !isMachineHitlDescription(explained)) return explained;
  if (mcnTool) return mcnSummary(mcnTool, args, t, toolLabel);
  if (BROWSER_TOOLS.has(name)) return browserSummary(args, t, toolLabel);
  const command = stringArg(args, "command", "cmd");
  if (SHELL_TOOLS.has(name) && command) {
    return resolve(t, "chat.hitl.summaries.runCommand", `Run: ${command}`, {
      command,
    });
  }
  const path = stringArg(args, "path", "file", "file_path");
  if (name === "write_file" && path) {
    return resolve(t, "chat.hitl.summaries.writeFile", `Write ${path}`, {
      path,
    });
  }
  if (name === "read_file" && path) {
    return resolve(t, "chat.hitl.summaries.readFile", `Read ${path}`, { path });
  }
  if (name === "edit_file" && path) {
    return resolve(t, "chat.hitl.summaries.editFile", `Edit ${path}`, { path });
  }
  const url = stringArg(args, "url");
  if (name === "web_fetch" && url) {
    return resolve(t, "chat.hitl.summaries.fetchUrl", `Fetch ${url}`, { url });
  }
  return toolLabel;
}

export function summarizeHitlAction(
  name: string,
  args: Record<string, unknown> | undefined,
  t: HitlTranslate,
  toolLabel: string,
  description?: string,
): HitlActionView {
  const safeArgs = args && typeof args === "object" ? args : {};
  const mcnTool = extractMcnTool(name);
  const resolvedToolLabel =
    mcnTool && (toolLabel === name || !toolLabel.trim())
      ? resolve(t, `chat.hitl.mcn.tools.${mcnTool}`, toolLabel || name)
      : toolLabel;
  const rows: HitlArgRow[] = [];
  for (const [key, value] of Object.entries(safeArgs)) {
    if (shouldHide(key, value)) continue;
    const formatted = formatArgValue(name, key, value, t);
    if (!formatted) continue;
    rows.push({
      label: argLabel(key, t),
      value: formatted,
      ...(MONO_KEYS.has(key) ? { mono: true } : {}),
    });
  }
  return {
    toolLabel: resolvedToolLabel,
    summary: buildSummary(name, safeArgs, t, resolvedToolLabel, description),
    rows,
  };
}
