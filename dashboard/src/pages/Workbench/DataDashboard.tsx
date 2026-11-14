import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, InputNumber, Modal, Select, Spin } from "antd";
import { message } from "@/utils/antdMessage";
import {
  isAbortError,
  workbenchApi,
  workbenchErrorMessage,
  type WorkbenchDashboardResponse,
} from "../../api/modules/workbench";
import RoleSegmentedHeader from "./RoleSegmentedHeader";
import styles from "./workbench.module.less";

export default function DataDashboardPage() {
  const [roleKey, setRoleKey] = useState("business");
  const [range, setRange] = useState("7d");
  const [data, setData] = useState<WorkbenchDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [editingGoal, setEditingGoal] = useState<
    WorkbenchDashboardResponse["goals"][number] | null
  >(null);
  const [goalCurrent, setGoalCurrent] = useState<number>(0);
  const [goalTarget, setGoalTarget] = useState<number>(100);
  const reqSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const loadData = useCallback(async (role: string, periodRange: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++reqSeqRef.current;

    const cached = workbenchApi.getCachedDashboard?.(role, periodRange);
    if (cached) {
      setData(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }

    try {
      const next = await workbenchApi.getDashboard(role, periodRange, {
        signal: controller.signal,
      });
      if (seq !== reqSeqRef.current) return;
      setData(next);
    } catch (err) {
      if (seq !== reqSeqRef.current || isAbortError(err)) return;
      message.error(workbenchErrorMessage(err, "加载数据看板失败"));
    } finally {
      if (seq === reqSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadData(roleKey, range);
    return () => {
      abortRef.current?.abort();
    };
  }, [loadData, roleKey, range]);

  const maxTrend = Math.max(
    1,
    ...(data?.trend.map((pt) => pt.value) ?? [1]),
  );

  const handleSaveGoal = async () => {
    if (!editingGoal) return;
    try {
      await workbenchApi.updateGoal(editingGoal.id, {
        current_value: goalCurrent,
        target_value: Math.max(1, goalTarget),
      });
      message.success("本月目标进度已更新");
      setEditingGoal(null);
      await loadData(roleKey, range);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "更新目标失败"));
    }
  };

  const colTemplate = `repeat(${Math.max(1, data?.trend.length ?? 7)}, minmax(0, 1fr))`;

  return (
    <div className={styles.pageWrap}>
      <RoleSegmentedHeader
        title="数据看板"
        subtitle="实时对接 MCN ERP 与 AI 创作工作台实绩数据"
        roles={data?.roles ?? []}
        activeRole={roleKey}
        onRoleChange={setRoleKey}
      />

      <Spin spinning={loading}>
        {data && (
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <section className={styles.overviewBar}>
              <div className={styles.overviewLeft}>
                <span className={styles.roleBadge}>{data.role.badge}</span>
                <span className={styles.panelTitle}>业务数据概览</span>
                <span
                  className={
                    data.mcn_connected
                      ? `${styles.sourcePill} ${styles.sourcePillLive}`
                      : styles.sourcePill
                  }
                >
                  {data.mcn_connected
                    ? `来源：${data.data_sources?.join(" · ") || "MCN ERP"}`
                    : "仅本地数据（未连 ERP）"}
                </span>
              </div>
              <Select
                value={range}
                onChange={setRange}
                style={{ width: 108 }}
                options={[
                  { value: "7d", label: "近 7 天" },
                  { value: "30d", label: "近 30 天" },
                  { value: "month", label: "本月累计" },
                ]}
              />
            </section>

            {data.mcn_error && (
              <Alert type="warning" showIcon message={data.mcn_error} />
            )}

            <section className={styles.kpiGrid}>
              {data.kpis.map((kpi) => {
                const delta = kpi.delta.trim();
                const deltaClass = delta.startsWith("-")
                  ? `${styles.kpiDelta} ${styles.kpiDeltaNeg}`
                  : delta.startsWith("+")
                    ? styles.kpiDelta
                    : `${styles.kpiDelta} ${styles.kpiDeltaNeutral}`;
                return (
                  <div
                    key={kpi.label}
                    className={styles.kpiCard}
                    style={{ flexDirection: "column", alignItems: "flex-start" }}
                  >
                    <span className={styles.kpiLabel}>{kpi.label}</span>
                    <span className={styles.kpiValue}>{kpi.value}</span>
                    <span className={deltaClass}>{kpi.delta}</span>
                  </div>
                );
              })}
            </section>

            <section className={styles.dashGrid}>
              <div className={styles.panelCard}>
                <div className={styles.panelHeader}>
                  <h2 className={styles.panelTitle}>业务趋势</h2>
                  <span className={styles.roleCaption}>
                    {data.trend_metric_label}
                  </span>
                </div>
                <div>
                  <div
                    className={styles.chartCanvas}
                    style={{ gridTemplateColumns: colTemplate }}
                  >
                    {data.trend.map((pt) => {
                      const heightPct =
                        pt.value <= 0
                          ? 6
                          : Math.max(14, Math.round((pt.value / maxTrend) * 84));
                      return (
                        <div key={pt.label} className={styles.chartCol}>
                          {pt.value > 0 && (
                            <span className={styles.chartValLabel}>
                              {pt.value}
                            </span>
                          )}
                          <div
                            className={styles.chartBar}
                            style={{
                              height: `${heightPct}%`,
                              opacity: pt.value <= 0 ? 0.25 : 1,
                            }}
                            title={`${pt.label}: ${pt.value}`}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div
                    className={styles.chartXLabels}
                    style={{ gridTemplateColumns: colTemplate }}
                  >
                    {data.trend.map((pt) => (
                      <span key={pt.label}>{pt.label}</span>
                    ))}
                  </div>
                </div>
              </div>

              <div className={styles.panelCard}>
                <div className={styles.panelHeader}>
                  <h2 className={styles.panelTitle}>本月目标</h2>
                </div>
                <div className={styles.goalList}>
                  {data.goals.map((g) => (
                    <div
                      key={g.id}
                      className={styles.goalRow}
                      role="button"
                      tabIndex={0}
                      onClick={() => {
                        setEditingGoal(g);
                        setGoalCurrent(g.current_value);
                        setGoalTarget(g.target_value);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          setEditingGoal(g);
                          setGoalCurrent(g.current_value);
                          setGoalTarget(g.target_value);
                        }
                      }}
                    >
                      <div className={styles.goalHeader}>
                        <span>
                          {g.title}（{g.current_value}/{g.target_value}
                          {g.unit}）
                        </span>
                        <strong>{g.percent}%</strong>
                      </div>
                      <div className={styles.goalTrack}>
                        <div
                          className={styles.goalFill}
                          style={{ width: `${g.percent}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          </div>
        )}
      </Spin>

      <Modal
        open={Boolean(editingGoal)}
        title={editingGoal ? `调整目标 · ${editingGoal.title}` : "调整目标"}
        onCancel={() => setEditingGoal(null)}
        onOk={() => void handleSaveGoal()}
        okText="保存"
      >
        {editingGoal && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span>当前完成值：</span>
              <InputNumber
                min={0}
                value={goalCurrent}
                onChange={(val) => setGoalCurrent(Number(val ?? 0))}
              />
              <span>{editingGoal.unit}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span>目标上限值：</span>
              <InputNumber
                min={1}
                value={goalTarget}
                onChange={(val) => setGoalTarget(Number(val ?? 1))}
              />
              <span>{editingGoal.unit}</span>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
