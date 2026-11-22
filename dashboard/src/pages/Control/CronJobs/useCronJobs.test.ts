import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { octopCronApi } from "../../../api/modules/cronjob";
import { useCronJobs, type CronJobFormValues } from "./useCronJobs";

vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({
    activeAgentId: "agent-1",
    activeAgent: { is_owner: true },
  }),
}));

vi.mock("react-i18next", () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});

afterEach(() => vi.restoreAllMocks());

it.each(["create", "patch"] as const)(
  "passes the invalid trigger error to the form on %s",
  async (operation) => {
    vi.spyOn(octopCronApi, "settings").mockResolvedValue({ timezone: "UTC" });
    const list = vi.spyOn(octopCronApi, "list").mockResolvedValue([]);
    const save = vi
      .spyOn(octopCronApi, operation)
      .mockRejectedValue(
        new Error(
          'Request failed: 400 - {"error":{"code":"CRON_TRIGGER_INVALID"}}',
        ),
      );
    const { result } = renderHook(() => useCronJobs());
    await waitFor(() => expect(list).toHaveBeenCalledWith("agent-1"));
    const values: CronJobFormValues = {
      name: "Daily report",
      enabled: false,
      schedule: { type: "cron", cron: "invalid", timezone: "UTC" },
      _scheduleMode: "custom",
      prompt: "Write a report",
      task_type: "agent",
      fresh_thread: false,
    };
    const onInvalidTrigger = vi.fn();
    await act(async () => {
      const saved =
        operation === "create"
          ? await result.current.createJob(values, onInvalidTrigger)
          : await result.current.updateJob("job-1", values, onInvalidTrigger);
      expect(saved).toBe(false);
    });

    expect(save).toHaveBeenCalledOnce();
    expect(onInvalidTrigger).toHaveBeenCalledWith(
      "apiErrors.CRON_TRIGGER_INVALID",
    );
    expect(result.current.jobs).toEqual([]);
  },
);
