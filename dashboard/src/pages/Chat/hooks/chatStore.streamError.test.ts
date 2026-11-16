import { afterEach, describe, expect, it } from "vitest";
import { getSnapshot, ingestHarnessChunk, removeSession } from "./chatStore";

const SESSION = "test-stream-error-finalize";

describe("finalizeStreamingMessages stream-error promotion", () => {
  afterEach(() => {
    removeSession(SESSION);
  });

  it("does not promote a normal answer that mentions rate limits", () => {
    ingestHarnessChunk(SESSION, {
      type: "token",
      node: "agent",
      content: "If you hit Error code: 429 rate_limit, wait a minute.",
    });
    ingestHarnessChunk(SESSION, { type: "done" });
    const msgs = getSnapshot(SESSION).messages.filter(
      (message) => message.role === "assistant",
    );
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.status).toBe("done");
  });

  it("promotes model-retry envelopes to error", () => {
    ingestHarnessChunk(SESSION, {
      type: "token",
      node: "agent",
      content: "Model call failed after 3 attempts with RuntimeError: boom",
    });
    ingestHarnessChunk(SESSION, { type: "done" });
    const msgs = getSnapshot(SESSION).messages.filter(
      (message) => message.role === "assistant",
    );
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.status).toBe("error");
    expect(msgs[0]?.errorInfo).toEqual({
      code: "stream_error",
      source: "model_retry",
    });
  });

  it("renders an explicit authentication error and stops loading", () => {
    ingestHarnessChunk(SESSION, {
      type: "error",
      message: "模型服务鉴权失败，请检查 API Key。",
    });
    ingestHarnessChunk(SESSION, { type: "done" });
    const state = getSnapshot(SESSION);
    const errors = state.messages.filter(
      (message) => message.status === "error",
    );
    expect(errors).toHaveLength(1);
    expect(errors[0]?.content).toContain("鉴权失败");
    expect(state.isStreaming).toBe(false);
  });

  it("keeps a later answer separate from an earlier resume error", () => {
    ingestHarnessChunk(SESSION, {
      type: "error",
      message: "HITL resume failed (502)",
    });
    ingestHarnessChunk(SESSION, {
      type: "token",
      node: "agent",
      content:
        "报价单已经创建完成。\n\n| 账号 | 金额 |\n| --- | --- |\n| A | 7500 |",
    });
    ingestHarnessChunk(SESSION, {
      type: "token",
      node: "agent",
      content: "\n\n合计 7500 元。",
    });
    ingestHarnessChunk(SESSION, { type: "done" });

    const messages = getSnapshot(SESSION).messages;
    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({
      status: "error",
      content: "HITL resume failed (502)",
    });
    expect(messages[1]).toMatchObject({
      status: "done",
      content:
        "报价单已经创建完成。\n\n| 账号 | 金额 |\n| --- | --- |\n| A | 7500 |\n\n合计 7500 元。",
    });
  });
});
