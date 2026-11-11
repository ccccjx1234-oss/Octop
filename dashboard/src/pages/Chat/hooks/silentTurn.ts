/** A finished turn the user can see only as their own message. */

export interface SilentTurnMessage {
  role: string;
  content?: string;
  toolData?: unknown;
  hitlData?: unknown;
  errorInfo?: unknown;
}

export function turnEndedWithoutReply(messages: SilentTurnMessage[]): boolean {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user") return true;
    if (message.role !== "assistant") continue;
    if (message.toolData || message.hitlData || message.errorInfo) return false;
    if ((message.content || "").trim()) return false;
  }
  return false;
}

export const SILENT_TURN_MESSAGE =
  "没有收到回复，连接可能中断了。请再发一次。";
