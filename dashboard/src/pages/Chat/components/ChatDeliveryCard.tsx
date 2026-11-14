import { useMemo, useState } from "react";
import { Button, Input, Select } from "antd";
import { message } from "@/utils/antdMessage";
import { CheckCircle2, FileText } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  workbenchApi,
  workbenchErrorMessage,
  type DeliverableReviewStatus,
  type DeliverableType,
} from "../../../api/modules/workbench";
import styles from "./ChatDeliveryCard.module.less";

interface ChatDeliveryCardProps {
  messageId: string;
  answerText: string;
  threadId?: string | null;
}

function inferDeliveryDefaults(text: string): {
  shouldAutoExpand: boolean;
  deliverableType: DeliverableType;
  targetName: string;
  title: string;
  body: string;
} {
  const clean = text.trim();
  const hasStructuredCue =
    clean.length >= 60 ||
    /\n\s*(?:\d+\.|[-*]|#{1,3})\s+/.test(clean) ||
    /(方案|报价|报告|交付|清单|脚本|合同|汇总|策划|ppt|http)/i.test(clean);

  const hasDocumentCue =
    /(方案|合同|协议|报告|复盘|脚本|策划|\.docx\b|\bword\b)/i.test(clean);
  const hasPureLinkCue =
    /^https?:\/\/\S+$/i.test(clean) ||
    (/https?:\/\//i.test(clean) && clean.length < 320 && !hasDocumentCue);
  const hasSheetCue =
    /\.(?:xlsx?|csv)\b|\bcsv\b|报价单|排期表|费用明细/i.test(clean) ||
    (/\|\s*---/.test(clean) && !hasDocumentCue);
  const hasPptCue =
    /\.(?:pptx?)\b|\bppt\b|幻灯片|演示文稿/i.test(clean) &&
    !/\.docx\b|\bword\b/i.test(clean);

  let deliverableType: DeliverableType = "document";
  if (hasPureLinkCue) deliverableType = "link";
  else if (hasSheetCue) deliverableType = "sheet";
  else if (hasPptCue) deliverableType = "ppt";

  const explicitCustomer =
    clean.match(/客户[：:]\s*([^\s|，。,；;\n]{2,16})/)?.[1] ??
    clean.match(/「([^\s「」\-—_]{2,10})[-—_]/)?.[1];
  const orgMatch = clean.match(
    /([\u4e00-\u9fa5A-Za-z0-9]{2,12}(?:科技|美妆|汽车|咖啡|文旅|集团|工作室|品牌|客户))/,
  );
  const targetName = explicitCustomer ?? orgMatch?.[1] ?? "当前客户";

  const firstHeading = clean
    .split(/\r?\n/)
    .map((line) =>
      line
        .replace(/^#{1,6}\s*/, "")
        .replace(/^[*-\d.]+\s*/, "")
        .replace(/\*\*/g, "")
        .trim(),
    )
    .find(
      (line) =>
        line.length >= 4 &&
        line.length <= 42 &&
        !/^\|/.test(line) &&
        !/^[-:| ]+$/.test(line) &&
        !/^(?:中间|因此|所以|如果|请告诉|只生成了)/.test(line),
    );

  const title = firstHeading
    ? firstHeading.replace(/[：:。.!！]+$/, "").slice(0, 32)
    : "AI 整理交付方案";

  return {
    shouldAutoExpand: hasStructuredCue,
    deliverableType,
    targetName,
    title,
    body: clean,
  };
}

export default function ChatDeliveryCard({
  messageId,
  answerText,
  threadId,
}: ChatDeliveryCardProps) {
  const navigate = useNavigate();
  const inferred = useMemo(
    () => inferDeliveryDefaults(answerText),
    [answerText],
  );

  const [expanded, setExpanded] = useState(inferred.shouldAutoExpand);
  const [deliverableType, setDeliverableType] = useState<DeliverableType>(
    inferred.deliverableType,
  );
  const [targetName, setTargetName] = useState(inferred.targetName);
  const [title, setTitle] = useState(inferred.title);
  const [reviewStatus, setReviewStatus] =
    useState<DeliverableReviewStatus>("pending_review");
  const [content, setContent] = useState(inferred.body);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  if (!answerText.trim()) return null;

  if (!expanded) {
    return (
      <div className={styles.wrap}>
        <button
          type="button"
          className={styles.compactTrigger}
          onClick={() => setExpanded(true)}
        >
          <FileText size={13} />
          <span>整理为对话交付成果</span>
        </button>
      </div>
    );
  }

  const handleSave = async () => {
    if (!title.trim()) {
      message.warning("请填写成果名称");
      return;
    }
    setSaving(true);
    try {
      const res = await workbenchApi.createDeliverable({
        deliverable_type: deliverableType,
        target_name: targetName.trim() || "当前客户",
        title: title.trim(),
        content: content.trim() || answerText.trim(),
        review_status: reviewStatus,
        source_thread_id: threadId ?? "",
        source_message_id: messageId,
      });
      setSaved(true);
      message.success(
        res.updated
          ? "已更新交付成果并同步工作进度"
          : "已保存为交付成果并同步更新工作进度",
      );
    } catch (err) {
      message.error(workbenchErrorMessage(err, "保存交付成果失败"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.hintRow}>
        已识别为可交付成果，你可以编辑后保存
      </div>
      <div className={styles.card}>
        <div className={styles.header}>
          <div className={styles.headLeft}>
            <div className={styles.iconBox}>
              <FileText size={16} />
            </div>
            <div>
              <div className={styles.headTitle}>对话交付</div>
              <div className={styles.headSub}>AI 已自动补全交付信息</div>
            </div>
          </div>
          <Select
            size="small"
            value={deliverableType}
            onChange={(val) => setDeliverableType(val)}
            style={{ width: 96 }}
            options={[
              { value: "document", label: "文档" },
              { value: "link", label: "链接" },
              { value: "sheet", label: "表格" },
              { value: "ppt", label: "PPT" },
            ]}
          />
        </div>

        <div className={styles.body}>
          <div className={styles.fieldsGrid}>
            <div>
              <div className={styles.fieldLabel}>交付对象</div>
              <Input
                value={targetName}
                onChange={(e) => {
                  setTargetName(e.target.value);
                  setSaved(false);
                }}
                placeholder="客户 / 对象名称"
              />
            </div>
            <div>
              <div className={styles.fieldLabel}>成果名称</div>
              <Input
                value={title}
                onChange={(e) => {
                  setTitle(e.target.value);
                  setSaved(false);
                }}
                placeholder="成果标题"
              />
            </div>
            <div>
              <div className={styles.fieldLabel}>审核状态</div>
              <Select
                value={reviewStatus}
                onChange={(val) => {
                  setReviewStatus(val);
                  setSaved(false);
                }}
                style={{ width: "100%" }}
                options={[
                  { value: "pending_review", label: "待审核" },
                  { value: "approved", label: "审核通过" },
                  { value: "needs_revision", label: "需修改" },
                ]}
              />
            </div>
          </div>

          <Input.TextArea
            className={styles.contentArea}
            rows={5}
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setSaved(false);
            }}
          />

          <div className={styles.footer}>
            <span className={styles.footerNote}>
              内容可直接编辑，保存后将同步更新工作进度
            </span>
            <div className={styles.savedActionRow}>
              {saved && (
                <Button
                  size="small"
                  type="link"
                  icon={<CheckCircle2 size={14} />}
                  onClick={() => navigate("/work/deliverables")}
                >
                  已保存 · 前往工作台查看
                </Button>
              )}
              <Button
                type="primary"
                icon={<FileText size={14} />}
                loading={saving}
                onClick={() => void handleSave()}
                style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
              >
                {saved ? "再次保存" : "保存为交付成果"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
