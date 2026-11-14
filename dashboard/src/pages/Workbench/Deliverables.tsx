import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Dropdown,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Spin,
} from "antd";
import { message } from "@/utils/antdMessage";
import {
  ExternalLink,
  FileSpreadsheet,
  FileText,
  Layers,
  Link2,
  MoreHorizontal,
  Plus,
  Search,
} from "lucide-react";
import {
  isAbortError,
  workbenchApi,
  workbenchErrorMessage,
  type DeliverableReviewStatus,
  type DeliverableType,
  type WorkbenchDeliverableItem,
  type WorkbenchDeliverablesResponse,
} from "../../api/modules/workbench";
import styles from "./workbench.module.less";

const TYPE_META: Record<
  DeliverableType,
  { label: string; badgeClass: string }
> = {
  document: { label: "文档", badgeClass: styles.typeDocument },
  link: { label: "链接", badgeClass: styles.typeLink },
  sheet: { label: "表格", badgeClass: styles.typeSheet },
  ppt: { label: "PPT", badgeClass: styles.typePpt },
};

const REVIEW_META: Record<
  DeliverableReviewStatus,
  { label: string; className: string }
> = {
  approved: { label: "审核通过", className: styles.reviewApproved },
  pending_review: { label: "待审核", className: styles.reviewPending },
  needs_revision: { label: "需修改", className: styles.reviewRevision },
};

function renderTypeIcon(type: DeliverableType) {
  if (type === "link") return <Link2 size={18} />;
  if (type === "sheet") return <FileSpreadsheet size={18} />;
  if (type === "ppt") return <Layers size={18} />;
  return <FileText size={18} />;
}

export default function DeliverablesPage() {
  const [filterType, setFilterType] = useState<string>("all");
  const [reviewFilter, setReviewFilter] = useState<string>("all");
  const [searchText, setSearchText] = useState("");
  const [data, setData] = useState<WorkbenchDeliverablesResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const [modalOpen, setModalOpen] = useState(false);
  const [editingItem, setEditingItem] =
    useState<WorkbenchDeliverableItem | null>(null);
  const [inspectingItem, setInspectingItem] =
    useState<WorkbenchDeliverableItem | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  const reqSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const loadData = useCallback(
    async (type: string, reviewStatus: string, q: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = ++reqSeqRef.current;
      const params = { type, review_status: reviewStatus, q };

      const cached = workbenchApi.getCachedDeliverables?.(params);
      if (cached) {
        setData(cached);
        setLoading(false);
      } else {
        setLoading(true);
      }

      try {
        const next = await workbenchApi.listDeliverables(params, {
          signal: controller.signal,
        });
        if (seq !== reqSeqRef.current) return;
        setData(next);
      } catch (err) {
        if (seq !== reqSeqRef.current || isAbortError(err)) return;
        message.error(workbenchErrorMessage(err, "加载交付成果失败"));
      } finally {
        if (seq === reqSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadData(filterType, reviewFilter, searchText);
    }, 150);
    return () => {
      window.clearTimeout(timer);
      abortRef.current?.abort();
    };
  }, [filterType, loadData, reviewFilter, searchText]);

  const openCreateModal = (prefill?: WorkbenchDeliverableItem) => {
    setEditingItem(null);
    form.setFieldsValue(
      prefill
        ? {
            deliverable_type: prefill.deliverable_type,
            target_name: prefill.target_name,
            title: prefill.title,
            review_status: prefill.review_status,
            content: prefill.content || prefill.summary,
          }
        : {
            deliverable_type: "document",
            target_name: "",
            title: "",
            review_status: "pending_review",
            content: "",
          },
    );
    setModalOpen(true);
  };

  const openEditModal = (item: WorkbenchDeliverableItem) => {
    if (item.readonly || item.id < 0) {
      setInspectingItem(item);
      return;
    }
    setEditingItem(item);
    form.setFieldsValue({
      deliverable_type: item.deliverable_type,
      target_name: item.target_name,
      title: item.title,
      review_status: item.review_status,
      content: item.content || item.summary,
    });
    setModalOpen(true);
  };

  const handleSave = async (values: {
    deliverable_type: DeliverableType;
    target_name: string;
    title: string;
    review_status: DeliverableReviewStatus;
    content: string;
  }) => {
    setSaving(true);
    try {
      if (editingItem) {
        await workbenchApi.updateDeliverable(editingItem.id, values);
        message.success("交付成果已更新");
      } else {
        await workbenchApi.createDeliverable(values);
        message.success("交付成果已创建并同步工作进度");
      }
      setModalOpen(false);
      await loadData(filterType, reviewFilter, searchText);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "保存交付成果失败"));
    } finally {
      setSaving(false);
    }
  };

  const handleQuickReview = async (
    item: WorkbenchDeliverableItem,
    status: DeliverableReviewStatus,
  ) => {
    try {
      await workbenchApi.updateDeliverable(item.id, { review_status: status });
      message.success(`状态已更新为「${REVIEW_META[status].label}」`);
      await loadData(filterType, reviewFilter, searchText);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "更新审核状态失败"));
    }
  };

  const handleDelete = async (item: WorkbenchDeliverableItem) => {
    try {
      await workbenchApi.deleteDeliverable(item.id);
      message.success("已删除交付成果");
      await loadData(filterType, reviewFilter, searchText);
    } catch (err) {
      message.error(workbenchErrorMessage(err, "删除失败"));
    }
  };

  return (
    <div className={styles.pageWrap}>
      <div className={styles.topBar}>
        <div className={styles.titleGroup}>
          <h1 className={styles.pageTitle}>交付成果</h1>
          <p className={styles.pageSub}>
            统一聚合对话归档交付、MCN 发布认领、合同档案与 AI 创作脚本
          </p>
        </div>
        <Button
          type="primary"
          icon={<Plus size={15} />}
          onClick={() => openCreateModal()}
          style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
        >
          新建交付
        </Button>
      </div>

      {data?.mcn_error && (
        <Alert type="warning" showIcon message={data.mcn_error} />
      )}

      <section className={styles.panelCard}>
        <div className={styles.filterBar}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Segmented
              value={filterType}
              onChange={(val) => setFilterType(String(val))}
              options={[
                { value: "all", label: "全部" },
                { value: "document", label: "文档" },
                { value: "link", label: "链接" },
                { value: "sheet", label: "表格" },
                { value: "ppt", label: "PPT" },
              ]}
            />
            <Select
              value={reviewFilter}
              onChange={setReviewFilter}
              style={{ width: 120 }}
              options={[
                { value: "all", label: "全部状态" },
                { value: "pending_review", label: "待审核" },
                { value: "approved", label: "审核通过" },
                { value: "needs_revision", label: "需修改" },
              ]}
            />
          </div>
          <Input
            allowClear
            prefix={<Search size={14} color="#9ca3af" />}
            placeholder="搜索交付对象或成果名称"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            style={{ width: 250 }}
          />
        </div>

        <div className={styles.delivKpiGrid}>
          <div className={styles.delivKpiCard}>
            <div className={styles.kpiLabel}>全部成果</div>
            <div className={styles.kpiValue} style={{ marginTop: 6 }}>
              {data?.kpis.total ?? 0}
            </div>
          </div>
          <div className={styles.delivKpiCard}>
            <div className={styles.kpiLabel}>待审核</div>
            <div className={styles.kpiValue} style={{ marginTop: 6 }}>
              {data?.kpis.pending_review ?? 0}
            </div>
          </div>
          <div className={styles.delivKpiCard}>
            <div className={styles.kpiLabel}>审核通过</div>
            <div className={styles.kpiValue} style={{ marginTop: 6 }}>
              {data?.kpis.approved ?? 0}
            </div>
          </div>
          <div className={styles.delivKpiCard}>
            <div className={styles.kpiLabel}>本周新增</div>
            <div className={styles.kpiValue} style={{ marginTop: 6 }}>
              {data?.kpis.added_this_week ?? 0}
            </div>
          </div>
        </div>

        <Spin spinning={loading}>
          {(data?.items ?? []).length === 0 ? (
            <div className={styles.emptyModuleState}>
              暂无符合条件的交付成果。可在「对话」中保存助手生成的方案，或点击右上角「新建交付」。
            </div>
          ) : (
            <div className={styles.delivCardsGrid}>
              {(data?.items ?? []).map((item) => {
                const tMeta =
                  TYPE_META[item.deliverable_type] ?? TYPE_META.document;
                const rMeta =
                  REVIEW_META[item.review_status] ?? REVIEW_META.pending_review;
                const isReadonly = Boolean(item.readonly || item.id < 0);
                const pillClass =
                  item.source === "chat"
                    ? `${styles.sourcePill} ${styles.sourcePillChat}`
                    : isReadonly
                      ? `${styles.sourcePill} ${styles.sourcePillLive}`
                      : styles.sourcePill;
                return (
                  <article key={`${item.id}-${item.source_message_id}`} className={styles.delivCard}>
                    <div className={styles.delivCardTop}>
                      <div className={styles.delivCardHeadLeft}>
                        <div className={`${styles.typeIconBox} ${tMeta.badgeClass}`}>
                          {renderTypeIcon(item.deliverable_type)}
                        </div>
                        <div style={{ minWidth: 0 }}>
                          <div className={styles.delivTypeLabel}>
                            <span>{tMeta.label}</span>
                            {item.source_label && (
                              <span
                                className={pillClass}
                                style={{ marginLeft: 8 }}
                              >
                                {item.source_label}
                              </span>
                            )}
                          </div>
                          <div
                            className={styles.delivTitle}
                            style={{ cursor: "pointer" }}
                            onClick={() => openEditModal(item)}
                          >
                            {item.title}
                          </div>
                          <div className={styles.delivSummary}>
                            {item.summary}
                          </div>
                        </div>
                      </div>

                      <div className={styles.statusActions}>
                        <span className={`${styles.reviewBadge} ${rMeta.className}`}>
                          {rMeta.label}
                        </span>
                        <Dropdown
                          trigger={["click"]}
                          menu={{
                            items: isReadonly
                              ? [
                                  {
                                    key: "view",
                                    label: "查看详情",
                                    onClick: () => setInspectingItem(item),
                                  },
                                  {
                                    key: "clone",
                                    label: "复制到我的交付",
                                    onClick: () => openCreateModal(item),
                                  },
                                ]
                              : [
                                  {
                                    key: "edit",
                                    label: "查看 / 编辑内容",
                                    onClick: () => openEditModal(item),
                                  },
                                  {
                                    key: "approve",
                                    label: "标记为审核通过",
                                    onClick: () =>
                                      void handleQuickReview(item, "approved"),
                                  },
                                  {
                                    key: "revision",
                                    label: "标记为需修改",
                                    onClick: () =>
                                      void handleQuickReview(
                                        item,
                                        "needs_revision",
                                      ),
                                  },
                                  {
                                    type: "divider",
                                  },
                                  {
                                    key: "delete",
                                    danger: true,
                                    label: "删除成果",
                                    onClick: () => void handleDelete(item),
                                  },
                                ],
                          }}
                        >
                          <Button
                            type="text"
                            size="small"
                            aria-label="更多操作"
                            icon={<MoreHorizontal size={16} />}
                          />
                        </Dropdown>
                      </div>
                    </div>

                    <div className={styles.delivFooter}>
                      <div>
                        <div className={styles.delivMetaLabel}>交付对象</div>
                        <div className={styles.delivMetaVal}>
                          {item.target_name || "未指定"}
                        </div>
                      </div>
                      <div>
                        <div className={styles.delivMetaLabel}>交付时间</div>
                        <div className={styles.delivMetaVal}>
                          {item.delivered_label}
                        </div>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </Spin>
      </section>

      <Modal
        open={Boolean(inspectingItem)}
        title={inspectingItem ? inspectingItem.title : "交付成果详情"}
        onCancel={() => setInspectingItem(null)}
        footer={
          inspectingItem
            ? [
                ...(inspectingItem.external_url
                  ? [
                      <Button
                        key="ext"
                        icon={<ExternalLink size={14} />}
                        href={inspectingItem.external_url}
                        target="_blank"
                      >
                        打开原链接
                      </Button>,
                    ]
                  : []),
                <Button
                  key="copy"
                  type="primary"
                  style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
                  onClick={() => {
                    const target = inspectingItem;
                    setInspectingItem(null);
                    openCreateModal(target);
                  }}
                >
                  复制到我的交付
                </Button>,
                <Button key="close" onClick={() => setInspectingItem(null)}>
                  关闭
                </Button>,
              ]
            : null
        }
      >
        {inspectingItem && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
            <div>
              <strong>来源：</strong>
              {inspectingItem.source_label || inspectingItem.source || "外部系统"}
            </div>
            <div>
              <strong>交付对象：</strong>
              {inspectingItem.target_name || "未指定"}
            </div>
            <div>
              <strong>时间：</strong>
              {inspectingItem.delivered_label}
            </div>
            <pre
              style={{
                whiteSpace: "pre-wrap",
                background: "#f8fafc",
                padding: "10px 12px",
                borderRadius: 8,
                margin: 0,
                fontFamily: "inherit",
              }}
            >
              {inspectingItem.content || inspectingItem.summary}
            </pre>
          </div>
        )}
      </Modal>

      <Modal
        open={modalOpen}
        title={editingItem ? "编辑交付成果" : "新建交付成果"}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText="保存交付成果"
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => void handleSave(values)}
        >
          <Form.Item
            name="deliverable_type"
            label="成果类型"
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "document", label: "文档" },
                { value: "link", label: "链接" },
                { value: "sheet", label: "表格" },
                { value: "ppt", label: "PPT" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="target_name"
            label="交付对象"
            rules={[{ required: true, message: "请输入交付对象" }]}
          >
            <Input placeholder="例如：茶颜 / 超级飞侠" />
          </Form.Item>
          <Form.Item
            name="title"
            label="成果名称"
            rules={[{ required: true, message: "请输入成果名称" }]}
          >
            <Input placeholder="例如：品牌合作内容策划方案" />
          </Form.Item>
          <Form.Item
            name="review_status"
            label="审核状态"
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "pending_review", label: "待审核" },
                { value: "approved", label: "审核通过" },
                { value: "needs_revision", label: "需修改" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="content"
            label="成果正文 / 摘要"
            rules={[{ required: true, message: "请输入交付成果内容" }]}
          >
            <Input.TextArea rows={5} placeholder="填写可直接交付的正文内容..." />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
