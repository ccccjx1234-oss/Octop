import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Slider,
  Spin,
} from "antd";
import { message } from "@/utils/antdMessage";
import {
  Activity,
  Check,
  CheckSquare,
  ChevronRight,
  Clock,
  FileText,
  Plus,
  RefreshCw,
  Trash2,
  Zap,
} from "lucide-react";
import {
  isAbortError,
  workbenchApi,
  workbenchErrorMessage,
  type WorkbenchProgressResponse,
  type WorkbenchTaskItem,
} from "../../api/modules/workbench";
import RoleSegmentedHeader from "./RoleSegmentedHeader";
import styles from "./workbench.module.less";

const STATUS_OPTIONS = [
  { value: "pending", label: "待开始" },
  { value: "in_progress", label: "进行中" },
  { value: "pending_delivery", label: "待交付" },
  { value: "done", label: "已完成" },
];

function statusLabel(status: string): string {
  return (
    STATUS_OPTIONS.find((item) => item.value === status)?.label ?? "进行中"
  );
}

export default function WorkProgressPage() {
  const [roleKey, setRoleKey] = useState("customer_service");
  const [range, setRange] = useState("week");
  const [data, setData] = useState<WorkbenchProgressResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncingMcn, setSyncingMcn] = useState(false);

  const [editingTask, setEditingTask] = useState<WorkbenchTaskItem | null>(
    null,
  );
  const [inspectingTask, setInspectingTask] =
    useState<WorkbenchTaskItem | null>(null);
  const [editProgress, setEditProgress] = useState(0);
  const [editStatus, setEditStatus] = useState("in_progress");
  const [savingTask, setSavingTask] = useState(false);
  const [deletingTask, setDeletingTask] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const reqSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const loadData = useCallback(async (role: string, periodRange: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++reqSeqRef.current;

    const cached = workbenchApi.getCachedProgress?.(role, periodRange);
    if (cached) {
      setData(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }

    try {
      const next = await workbenchApi.getProgress(role, periodRange, {
        signal: controller.signal,
      });
      if (seq !== reqSeqRef.current) return;
      setData(next);
    } catch (err) {
      if (seq !== reqSeqRef.current || isAbortError(err)) return;
      message.error(workbenchErrorMessage(err, "加载工作进度失败"));
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

  const handleTaskClick = (task: WorkbenchTaskItem) => {
    if (task.readonly || task.id < 0) {
      setInspectingTask(task);
      return;
    }
    setEditingTask(task);
    setEditProgress(task.progress);
    setEditStatus(task.status);
  };

  const handleSaveTask = async () => {
    if (!editingTask) return;
    setSavingTask(true);
    try {
      await workbenchApi.updateTask(editingTask.id, {
        progress: editProgress,
        status: editStatus,
      });
      message.success("工作进度已更新");
      setEditingTask(null);
      await loadData(roleKey, range);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "更新失败"));
    } finally {
      setSavingTask(false);
    }
  };

  const handleDeleteTask = async () => {
    if (!editingTask) return;
    setDeletingTask(true);
    try {
      await workbenchApi.deleteTask(editingTask.id);
      message.success("已删除本地任务");
      setEditingTask(null);
      await loadData(roleKey, range);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "删除任务失败"));
    } finally {
      setDeletingTask(false);
    }
  };

  const handleConvertErpTask = async (task: WorkbenchTaskItem) => {
    setSavingTask(true);
    try {
      await workbenchApi.createTask({
        role_key: task.role_key || roleKey,
        module_key: task.module_key,
        module_title: task.module_title,
        target_type: task.target_type,
        target_name: task.target_name,
        title: task.title,
        due_label: task.due_label,
        status: task.status,
        progress: task.progress,
        source: "manual",
        external_ref: task.external_ref,
      });
      message.success("已转为本地可跟踪任务");
      setInspectingTask(null);
      await loadData(roleKey, range);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "转为本地任务失败"));
    } finally {
      setSavingTask(false);
    }
  };

  const handleCreateTask = async (values: {
    module_key: string;
    target_name: string;
    title: string;
    due_label: string;
    progress: number;
  }) => {
    try {
      await workbenchApi.createTask({
        role_key: roleKey,
        module_key: values.module_key,
        target_name: values.target_name,
        title: values.title,
        due_label: values.due_label,
        progress: values.progress,
        status: values.progress >= 100 ? "done" : "in_progress",
      });
      message.success("已新增核心任务");
      setCreateOpen(false);
      createForm.resetFields();
      await loadData(roleKey, range);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "新增任务失败"));
    }
  };

  const handleSyncMcn = async () => {
    setSyncingMcn(true);
    try {
      const res = await workbenchApi.syncMcn(roleKey);
      if (res.mcn_connected) {
        message.success(res.detail || `已同步 ${res.synced} 项 ERP 数据`);
        await loadData(roleKey, range);
      } else {
        message.warning(res.detail || "当前未绑定 MCN 会话，展示本地工作台数据");
        await loadData(roleKey, range);
      }
    } catch (err) {
      message.error(workbenchErrorMessage(err, "同步 ERP 数据失败"));
    } finally {
      setSyncingMcn(false);
    }
  };

  return (
    <div className={styles.pageWrap}>
      <RoleSegmentedHeader
        title="工作进度"
        subtitle="按角色汇总 MCN ERP 实时业务进展与本地 AI 协作任务"
        roles={data?.roles ?? []}
        activeRole={roleKey}
        onRoleChange={setRoleKey}
      />

      <Spin spinning={loading}>
        {data && (
          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <section className={styles.bannerCard}>
              <div className={styles.bannerMain}>
                <span className={styles.roleBadge}>
                  {data.role.workspace_title}
                </span>
                <div className={styles.targetHeadline}>
                  <span className={styles.targetCaption}>交付对象</span>
                  <span className={styles.targetEmphasis}>
                    {data.role.target_type}
                  </span>
                </div>
                <div className={styles.bannerTagline}>
                  {data.role.goal_tagline}
                </div>
              </div>
              <div className={styles.bannerActions}>
                <span className={styles.aiGoalPill}>
                  {data.mcn_connected
                    ? `已直连：${data.data_sources?.join(" · ") || "MCN ERP"}`
                    : "未连接 MCN ERP"}
                </span>
                <Button
                  size="small"
                  icon={<RefreshCw size={13} />}
                  loading={syncingMcn}
                  onClick={() => void handleSyncMcn()}
                >
                  {data.mcn_connected ? "刷新 ERP 数据" : "检查 ERP 联动"}
                </Button>
              </div>
            </section>

            {data.mcn_error && (
              <Alert type="warning" showIcon message={data.mcn_error} />
            )}

            <section className={styles.kpiGrid}>
              <div className={styles.kpiCard}>
                <div className={`${styles.kpiIconBox} ${styles.kpiOrange}`}>
                  <FileText size={18} />
                </div>
                <div className={styles.kpiBody}>
                  <span className={styles.kpiLabel}>核心工作</span>
                  <span className={styles.kpiValue}>{data.kpis.core_work}</span>
                </div>
              </div>
              <div className={styles.kpiCard}>
                <div className={`${styles.kpiIconBox} ${styles.kpiPeach}`}>
                  <Clock size={18} />
                </div>
                <div className={styles.kpiBody}>
                  <span className={styles.kpiLabel}>推进中</span>
                  <span className={styles.kpiValue}>
                    {data.kpis.in_progress}
                  </span>
                </div>
              </div>
              <div className={styles.kpiCard}>
                <div className={`${styles.kpiIconBox} ${styles.kpiGreen}`}>
                  <CheckSquare size={18} />
                </div>
                <div className={styles.kpiBody}>
                  <span className={styles.kpiLabel}>
                    {range === "today"
                      ? "今日已交付 / 完成"
                      : range === "month"
                        ? "本月已交付 / 完成"
                        : "本周已交付 / 完成"}
                  </span>
                  <span className={styles.kpiValue}>
                    {data.kpis.delivered_this_week}
                  </span>
                </div>
              </div>
              <div className={styles.kpiCard}>
                <div className={`${styles.kpiIconBox} ${styles.kpiPurple}`}>
                  <Activity size={18} />
                </div>
                <div className={styles.kpiBody}>
                  <span className={styles.kpiLabel}>整体完成率</span>
                  <span className={styles.kpiValue}>
                    {data.kpis.overall_completion}%
                  </span>
                </div>
              </div>
            </section>

            <section className={styles.panelCard}>
              <div className={styles.panelHeader}>
                <div className={styles.panelTitleGroup}>
                  <Zap size={16} color="#ff5a1f" />
                  <h2 className={styles.panelTitle}>核心工作进度</h2>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div className={styles.rangeTabs}>
                    {[
                      { key: "week", label: "本周进度" },
                      { key: "today", label: "今日重点" },
                      { key: "month", label: "本月全量" },
                    ].map((tab) => (
                      <button
                        key={tab.key}
                        type="button"
                        className={`${styles.rangeTab} ${range === tab.key ? styles.rangeTabActive : ""}`}
                        onClick={() => setRange(tab.key)}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>
                  <Button
                    type="primary"
                    size="small"
                    icon={<Plus size={14} />}
                    onClick={() => {
                      createForm.setFieldsValue({
                        module_key: data.modules[0]?.module_key ?? "project_progress",
                        target_name: "",
                        title: "",
                        due_label: "本周内",
                        progress: 30,
                      });
                      setCreateOpen(true);
                    }}
                    style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
                  >
                    新建任务
                  </Button>
                </div>
              </div>

              <div className={styles.moduleColumns}>
                {data.modules.map((mod) => (
                  <div key={mod.module_key} className={styles.moduleCard}>
                    <div className={styles.moduleCardHead}>
                      <span className={styles.moduleTitle}>
                        {mod.module_title}
                      </span>
                      <span className={styles.moduleMeta}>
                        {mod.target_type} · {mod.task_count} 项
                      </span>
                    </div>

                    {mod.items.length === 0 ? (
                      <div className={styles.emptyModuleState}>
                        当前暂无任务，可点击右上角新建，或授权经贝账号后自动拉取 ERP 业务数据
                      </div>
                    ) : (
                      <div className={styles.taskList}>
                        {mod.items.map((item) => {
                          const isDone =
                            item.status === "done" || item.progress >= 100;
                          const isActive =
                            item.status === "pending_delivery" ||
                            item.progress >= 80;
                          const pillClass =
                            item.source === "chat"
                              ? `${styles.sourcePill} ${styles.sourcePillChat}`
                              : item.readonly
                                ? `${styles.sourcePill} ${styles.sourcePillLive}`
                                : styles.sourcePill;
                          return (
                            <button
                              key={`${item.id}-${item.external_ref}`}
                              type="button"
                              className={styles.taskRow}
                              onClick={() => handleTaskClick(item)}
                            >
                              <div className={styles.taskLeft}>
                                <span
                                  className={
                                    isDone
                                      ? `${styles.statusRing} ${styles.statusRingDone}`
                                      : isActive
                                        ? `${styles.statusRing} ${styles.statusRingActive}`
                                        : styles.statusRing
                                  }
                                >
                                  {isDone ? (
                                    <Check size={9} color="#fff" strokeWidth={3} />
                                  ) : isActive ? (
                                    <span className={styles.statusDot} />
                                  ) : null}
                                </span>
                                <div>
                                  <div className={styles.taskCustomer}>
                                    <span>{item.target_name}</span>
                                    {item.source_label && (
                                      <span
                                        className={pillClass}
                                        style={{ marginLeft: 8 }}
                                      >
                                        {item.source_label}
                                      </span>
                                    )}
                                  </div>
                                  <div className={styles.taskMeta}>
                                    {item.title} · {item.due_label} ·{" "}
                                    {statusLabel(item.status)}
                                  </div>
                                </div>
                              </div>
                              <div className={styles.taskRight}>
                                <span className={styles.taskPctRow}>
                                  <span>{item.progress}%</span>
                                  <ChevronRight size={13} color="#9ca3af" />
                                </span>
                                <div className={styles.progressTrack}>
                                  <div
                                    className={styles.progressFill}
                                    style={{ width: `${item.progress}%` }}
                                  />
                                </div>
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
      </Spin>

      <Modal
        open={Boolean(inspectingTask)}
        title={
          inspectingTask
            ? `${inspectingTask.target_name} · ERP / 外部系统明细`
            : "外部数据明细"
        }
        onCancel={() => setInspectingTask(null)}
        footer={
          inspectingTask
            ? [
                <Button key="close" onClick={() => setInspectingTask(null)}>
                  关闭
                </Button>,
                <Button
                  key="convert"
                  type="primary"
                  loading={savingTask}
                  style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
                  onClick={() => void handleConvertErpTask(inspectingTask)}
                >
                  转为本地可编辑任务
                </Button>,
              ]
            : null
        }
      >
        {inspectingTask && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
            <div>
              <strong>来源：</strong>
              {inspectingTask.source_label || inspectingTask.source}
            </div>
            <div>
              <strong>业务事项：</strong>
              {inspectingTask.title}
            </div>
            <div>
              <strong>时间节点：</strong>
              {inspectingTask.due_label}
            </div>
            <div>
              <strong>当前状态：</strong>
              {statusLabel(inspectingTask.status)}（{inspectingTask.progress}%）
            </div>
            {inspectingTask.meta && Object.keys(inspectingTask.meta).length > 0 && (
              <div
                style={{
                  background: "#f8fafc",
                  borderRadius: 8,
                  padding: "10px 12px",
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                {Object.entries(inspectingTask.meta)
                  .filter(([, val]) => val !== null && val !== undefined && val !== "")
                  .map(([k, val]) => (
                    <div key={k}>
                      <span style={{ color: "#6b7280" }}>{k}：</span>
                      <span>{String(val)}</span>
                    </div>
                  ))}
              </div>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(editingTask)}
        title={
          editingTask
            ? `${editingTask.target_name} · ${editingTask.title}`
            : "更新任务进度"
        }
        onCancel={() => setEditingTask(null)}
        footer={[
          <Button
            key="delete"
            danger
            icon={<Trash2 size={14} />}
            loading={deletingTask}
            onClick={() => void handleDeleteTask()}
          >
            删除任务
          </Button>,
          <Button key="cancel" onClick={() => setEditingTask(null)}>
            取消
          </Button>,
          <Button
            key="save"
            type="primary"
            loading={savingTask}
            style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
            onClick={() => void handleSaveTask()}
          >
            保存更新
          </Button>,
        ]}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <div style={{ marginBottom: 6, fontSize: 13 }}>当前状态</div>
            <Select
              value={editStatus}
              onChange={(val) => {
                setEditStatus(val);
                if (val === "done") setEditProgress(100);
              }}
              style={{ width: "100%" }}
              options={STATUS_OPTIONS}
            />
          </div>
          <div>
            <div style={{ marginBottom: 6, fontSize: 13 }}>
              完成进度：{editProgress}%
            </div>
            <Slider
              min={0}
              max={100}
              value={editProgress}
              onChange={(v) => setEditProgress(v)}
            />
          </div>
        </div>
      </Modal>

      <Modal
        open={createOpen}
        title="新增核心工作任务"
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        okText="创建任务"
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => void handleCreateTask(values)}
        >
          <Form.Item
            name="module_key"
            label="所属模块"
            rules={[{ required: true }]}
          >
            <Select
              options={(data?.modules ?? []).map((m) => ({
                value: m.module_key,
                label: m.module_title,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="target_name"
            label="交付对象"
            rules={[{ required: true, message: "请输入客户或对象名称" }]}
          >
            <Input placeholder="例如：茶颜 / 超级飞侠" />
          </Form.Item>
          <Form.Item
            name="title"
            label="任务内容"
            rules={[{ required: true, message: "请输入任务内容" }]}
          >
            <Input placeholder="例如：确认季度合作报价单" />
          </Form.Item>
          <Form.Item name="due_label" label="截止时间">
            <Input placeholder="例如：今天 18:00" />
          </Form.Item>
          <Form.Item name="progress" label="初始进度 (%)">
            <InputNumber min={0} max={100} style={{ width: "100%" }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
