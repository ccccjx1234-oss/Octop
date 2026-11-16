import { useMemo, useRef, useState } from "react";
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
import { isFileToolName } from "../constants";
import styles from "./ChatDeliveryCard.module.less";

interface ChatDeliveryCardProps {
  messageId: string;
  answerText: string;
  threadId?: string | null;
  toolNames?: ReadonlyArray<string>;
}

const NON_DELIVERABLE_OPENING_RE =
  /^(?:到点啦|该.+了|你好|您好|早安|晚安|嗨|哈喽|收到|好的|没问题|明白了|抱歉|不好意思|对不起|用户已拒绝|已(?:为你|帮你|成功)?(?:创建|设置|安排|取消|更新|修改|删除|保存|提交|导出|查询|找到|列出))/i;

const REMINDER_OR_CRON_RE =
  /(?:定时任务|提醒时间|下次执行|触发时间|任务\s*[a-zA-Z0-9_-]{4,}|(?:周一|周二|周三|周四|周五|周六|周日|每天|每周).*?提醒)/i;

const OPERATION_RECEIPT_RE =
  /(?:报价单|订单|合同|项目|子项目|任务|日程|提醒|记录).{0,16}已(?:创建|生成|保存|提交|更新|完成|取消|删除|归档|导出)|^(?:已|已经)?(?:为你|帮你|成功)?(?:查询|查到|列出|找到|获取到|创建|生成|保存|提交|导出|读取).{0,24}(?:如下|列表|清单|记录|文件|成功)/i;

const SYSTEM_OR_MODEL_META_RE =
  /(?:Octop\s*harness|通用\s*AI\s*Agent|模型标识|参数规模|训练数据截止|具体版本号|底层.{0,12}模型|服务端配置|启动日志|环境变量|USER\.md|MEMORY\.md|AGENTS\.md|SOUL\.md|HEARTBEAT\.md)/i;

const FILE_LISTING_RE =
  /(?:工作区|当前目录|目录下|文件夹).{0,16}(?:有以下|包含以下|共有|文件列表|如下)|^(?:我的)?文件列表[：:]/i;

const REFUSAL_OR_INPUT_REQUEST_RE =
  /(?:未找到|暂无(?:可用)?(?:数据|记录|结果|文件)|无法(?:生成|创建|获取|访问|确认)|请先(?:登录|授权|提供|确认|补充)|需要(?:你|您)(?:先)?(?:提供|确认|补充))/i;

const DELIVERABLE_DOC_KEYWORD_RE =
  /(?:合作方案|执行方案|策划方案|报价单|报价方案|结案报告|复盘报告|工作汇报|项目汇报|运营报告|排期表|费用明细|预算明细|预算表|短视频脚本|分镜脚本|脚本方案|框架合同|合同方案|活动策划|策划案|商业计划|交付清单|需求文档|项目方案)/i;

const ARTIFACT_FILE_RE = /\.(?:docx?|xlsx?|csv|pptx?|pdf)\b/i;

const GENERIC_TARGET_RE =
  /^(?:当前客户|该客户|本客户|这个客户|那个客户|某个客户|一个客户|我的客户|客户|当前品牌|该品牌|本品牌|这个品牌|品牌|公司|集团|工作室)$/;

const LEAD_IN_LINE_RE =
  /^(?:中间|因此|所以|如果|请告诉|只生成了|能确定的是|不知道的|需要注意的是|已知|目前|以下|具体如下|说明如下|总结如下|注意事项|记忆|摘要|说明|备注)/;

function normalizeToolBaseName(name: string): string {
  const trimmed = name.trim();
  const slash = trimmed.lastIndexOf("/");
  return (slash >= 0 ? trimmed.slice(slash + 1) : trimmed).toLowerCase();
}

function isMcnWriteToolName(base: string): boolean {
  return /(?:^|_)youli_(?:save_quote|create_package_quote|create_order_from_quote|create_combined_order|update_publish_link|create_project|create_sub_project|create_coupon_version|create_contract)$/.test(
    base,
  );
}

function isExportDownloadToolName(base: string): boolean {
  return /(?:^|_)youli_(?:export_delivery|get_download_task)$/.test(base);
}

function isReadOnlyOrChatToolName(base: string): boolean {
  return (
    /^(?:ls|list_files|find_files|glob|grep|read_file|view_file|web_search|web_fetch|ask_user_question|write_todos|memory_search|memory_get)$/.test(
      base,
    ) ||
    /(?:^|_)youli_(?:list_|get_|query_|parse_)/.test(base) ||
    /^(?:schedule|cron|reminder)_/.test(base)
  );
}

export function inferDeliveryDefaults(
  text: string,
  options?: { toolNames?: ReadonlyArray<string> },
): {
  shouldRender: boolean;
  shouldAutoExpand: boolean;
  deliverableType: DeliverableType;
  targetName: string;
  title: string;
  body: string;
} {
  const clean = text.trim();
  if (!clean) {
    return {
      shouldRender: false,
      shouldAutoExpand: false,
      deliverableType: "document",
      targetName: "当前客户",
      title: "AI 整理交付方案",
      body: "",
    };
  }

  const normalizedTools = (options?.toolNames ?? [])
    .map(normalizeToolBaseName)
    .filter(Boolean);
  const usedArtifactTool = normalizedTools.some((name) => isFileToolName(name));
  const usedMcnWriteTool = normalizedTools.some(isMcnWriteToolName);
  const usedExportTool = normalizedTools.some(isExportDownloadToolName);
  const onlyReadOnlyOrChatTools =
    normalizedTools.length > 0 &&
    !usedArtifactTool &&
    !usedMcnWriteTool &&
    !usedExportTool &&
    normalizedTools.every(isReadOnlyOrChatToolName);

  const hasMarkdownHeading = /^#{1,3}\s+\S/m.test(clean);
  const hasMarkdownTable = /\|\s*---/.test(clean);
  const hasMultiSectionOutline =
    (clean.match(/\n\s*(?:#{1,3}|\d+\.|[-*])\s+/g) ?? []).length >= 3;
  const hasDeliverableKeyword = DELIVERABLE_DOC_KEYWORD_RE.test(clean);
  const hasArtifactFile = ARTIFACT_FILE_RE.test(clean);

  const hasDocumentCue =
    /(方案|合同|协议|报告|复盘|脚本|策划|\.docx?\b|\bword\b)/i.test(clean);
  const hasUrl = /https?:\/\/\S+/i.test(clean);
  const hasPureLinkCue =
    hasUrl &&
    clean.length < 320 &&
    !hasDocumentCue &&
    (hasDeliverableKeyword || hasArtifactFile || usedExportTool);
  const hasSheetCue =
    /\.(?:xlsx?|csv)\b|\bcsv\b|报价单|排期表|费用明细/i.test(clean) ||
    (hasMarkdownTable && !hasDocumentCue);
  const hasPptCue =
    /\.(?:pptx?)\b|\bppt\b|幻灯片|演示文稿/i.test(clean) &&
    !/\.docx?\b|\bword\b/i.test(clean);

  const hasFormalDeliverableDoc =
    hasDeliverableKeyword &&
    clean.length >= 220 &&
    (hasMarkdownHeading || hasMultiSectionOutline || hasMarkdownTable);

  const endsWithQuestion = /[？?]\s*$/.test(clean);
  const isNonDeliverableMetaOrListing =
    SYSTEM_OR_MODEL_META_RE.test(clean) ||
    (FILE_LISTING_RE.test(clean) && !hasFormalDeliverableDoc) ||
    (OPERATION_RECEIPT_RE.test(clean) && !hasFormalDeliverableDoc) ||
    (REFUSAL_OR_INPUT_REQUEST_RE.test(clean) && !hasFormalDeliverableDoc) ||
    (endsWithQuestion && !hasFormalDeliverableDoc);

  const looksLikeCasualOrReminder =
    NON_DELIVERABLE_OPENING_RE.test(clean) || REMINDER_OR_CRON_RE.test(clean);

  // MCN write tools already persist records in ERP; their reply is a receipt, not a chat deliverable.
  const blockedByTools =
    (usedMcnWriteTool && !usedArtifactTool && !hasFormalDeliverableDoc) ||
    (onlyReadOnlyOrChatTools && !hasFormalDeliverableDoc);

  const blocked =
    blockedByTools ||
    isNonDeliverableMetaOrListing ||
    (looksLikeCasualOrReminder && !hasFormalDeliverableDoc);

  const shouldAutoExpand =
    !blocked &&
    hasDeliverableKeyword &&
    ((hasMarkdownHeading && hasMarkdownTable) ||
      (clean.length >= 220 &&
        (hasMarkdownHeading || hasMultiSectionOutline || hasMarkdownTable)) ||
      (hasArtifactFile && clean.length >= 160 && hasMarkdownHeading));

  const shouldRender =
    !blocked &&
    (shouldAutoExpand ||
      (clean.length >= 90 && hasDeliverableKeyword) ||
      (clean.length >= 140 && hasArtifactFile && usedArtifactTool) ||
      hasPureLinkCue);

  let deliverableType: DeliverableType = "document";
  if (hasPureLinkCue) deliverableType = "link";
  else if (hasSheetCue) deliverableType = "sheet";
  else if (hasPptCue) deliverableType = "ppt";

  const explicitCustomer =
    clean.match(/客户[：:]\s*([^\s|，。,；;\n]{2,16})/)?.[1] ??
    clean.match(/「([^\s「」\-—_]{2,10})[-—_]/)?.[1];
  const orgMatch = clean.match(
    /([\u4e00-\u9fa5A-Za-z0-9]{2,12}(?:科技|美妆|汽车|咖啡|文旅|集团|工作室|品牌|客户))/,
  )?.[1];
  const candidateTarget = explicitCustomer ?? orgMatch ?? "";
  const targetName =
    candidateTarget && !GENERIC_TARGET_RE.test(candidateTarget)
      ? candidateTarget
      : "当前客户";

  const lines = clean.split(/\r?\n/);
  const explicitMdHeading = lines
    .map((line) => line.match(/^#{1,6}\s+(.+)$/)?.[1] ?? "")
    .map((line) => line.replace(/\*\*/g, "").trim())
    .find(
      (line) =>
        line.length >= 4 && line.length <= 42 && !LEAD_IN_LINE_RE.test(line),
    );

  const fallbackHeading = lines
    .slice(0, 4)
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
        !/[：:]$/.test(line) &&
        !LEAD_IN_LINE_RE.test(line),
    );

  const firstHeading = explicitMdHeading ?? fallbackHeading;

  const title = firstHeading
    ? firstHeading.replace(/[：:。.!！]+$/, "").slice(0, 32)
    : "AI 整理交付方案";

  return {
    shouldRender,
    shouldAutoExpand,
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
  toolNames,
}: ChatDeliveryCardProps) {
  const navigate = useNavigate();
  const inferred = useMemo(
    () => inferDeliveryDefaults(answerText, { toolNames }),
    [answerText, toolNames],
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
  const saveInFlight = useRef(false);

  if (!inferred.shouldRender) return null;

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
    if (saveInFlight.current || saved) return;
    if (!title.trim()) {
      message.warning("请填写成果名称");
      return;
    }
    saveInFlight.current = true;
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
      saveInFlight.current = false;
      setSaving(false);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.hintRow}>已识别为可交付成果，你可以编辑后保存</div>
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
            disabled={saving}
            value={deliverableType}
            onChange={(val) => {
              setDeliverableType(val);
              setSaved(false);
            }}
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
                disabled={saving}
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
                disabled={saving}
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
                disabled={saving}
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
            disabled={saving}
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
                disabled={saved}
                onClick={() => void handleSave()}
                style={{ background: "#ff5a1f", borderColor: "#ff5a1f" }}
              >
                {saved ? "已保存" : "保存为交付成果"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
