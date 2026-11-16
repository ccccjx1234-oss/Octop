import { useEffect, useState } from "react";
import {
  listAgentSubagents,
  type AgentSubagentSummary,
} from "../../../api/modules/subagents";

/** Installed workspace subagents for the running chat agent (empty if not ready). */
export function useChatSubagents(agentId: string | null): {
  subagents: AgentSubagentSummary[];
  loading: boolean;
} {
  const [subagents, setSubagents] = useState<AgentSubagentSummary[]>([]);
  const [loadedAgentId, setLoadedAgentId] = useState<string | null>(null);

  useEffect(() => {
    if (!agentId) {
      setSubagents([]);
      setLoadedAgentId(null);
      return;
    }
    let cancelled = false;
    void listAgentSubagents(agentId)
      .then((rows) => {
        if (!cancelled) setSubagents(rows);
      })
      .catch(() => {
        if (!cancelled) setSubagents([]);
      })
      .finally(() => {
        if (!cancelled) setLoadedAgentId(agentId);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return { subagents, loading: Boolean(agentId && loadedAgentId !== agentId) };
}
