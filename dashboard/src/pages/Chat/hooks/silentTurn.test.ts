import { describe, expect, it } from "vitest";
import { turnEndedWithoutReply } from "./silentTurn";

describe("turnEndedWithoutReply", () => {
  it("is true when the latest visible turn is only the user", () => {
    expect(
      turnEndedWithoutReply([
        { role: "assistant", content: "前 1 个客户" },
        { role: "user", content: "还有哪些" },
      ]),
    ).toBe(true);
  });

  it("ignores an empty assistant bubble", () => {
    expect(
      turnEndedWithoutReply([
        { role: "user", content: "还有哪些" },
        { role: "assistant", content: "   " },
      ]),
    ).toBe(true);
  });

  it("is false when a tool call or error was shown", () => {
    expect(
      turnEndedWithoutReply([
        { role: "user", content: "查客户" },
        { role: "assistant", content: "", toolData: { name: "youli_list_customers" } },
      ]),
    ).toBe(false);
    expect(
      turnEndedWithoutReply([
        { role: "user", content: "查客户" },
        { role: "assistant", content: "失败", errorInfo: { code: "stream_error" } },
      ]),
    ).toBe(false);
  });
});
