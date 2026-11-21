import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Button, Drawer, Form, Input, Spin, Tag, Tooltip } from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import { message } from "@/utils/antdMessage";

import { agentChatApi } from "../../../api/modules/agentChat";
import { connectorsApi } from "../../../api/modules/connectors";
import { knowledgeBasesApi } from "../../../api/modules/knowledgeBases";
import {
  publishedExpertsApi,
  type PublishedExpert,
  type PublishExpertBody,
} from "../../../api/modules/publishedExperts";
import { request } from "../../../api/request";
import type { OctopAgent } from "../../../context/AgentContext";
import { apiErrorMessage } from "../../../utils/apiError";
import { pickLocale } from "../../../utils/localizedText";
import { resolveSkillDisplayName } from "../../Agent/Skills/skillDisplayNames";
import styles from "../index.module.less";

export interface PublishExpertDrawerProps {
  open: boolean;
  mode: "publish" | "refresh";
  agent: OctopAgent;
  published: PublishedExpert | null;
  onClose: () => void;
  onSuccess: () => void;
}

interface PublishFormValues {
  name: string;
  description?: string;
  welcome_zh?: string;
  welcome_en?: string;
}

interface SkillSummary {
  name: string;
  slug?: string;
  kind?: string;
  label?: { zh?: string; en?: string };
  display_name?: string;
}

interface SharePreview {
  skills: string[];
  connectors: string[];
  knowledgeBases: string[];
}

const EMPTY_SHARE: SharePreview = {
  skills: [],
  connectors: [],
  knowledgeBases: [],
};

function uniqueNames(names: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const name of names) {
    const text = name.trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function ShareRow({
  label,
  note,
  items,
  empty,
}: {
  label: string;
  note: string;
  items: string[];
  empty: string;
}) {
  return (
    <div className={styles.publishShareRow}>
      <div className={styles.publishShareHead}>
        <span className={styles.publishShareLabel}>{label}</span>
        <Tooltip title={note}>
          <InfoCircleOutlined className={styles.publishShareTip} />
        </Tooltip>
      </div>
      <div className={styles.publishShareTags}>
        {items.length > 0 ? (
          items.map((item) => (
            <Tag key={item} bordered={false}>
              {item}
            </Tag>
          ))
        ) : (
          <span className={styles.publishShareEmpty}>{empty}</span>
        )}
      </div>
    </div>
  );
}

export default function PublishExpertDrawer({
  open,
  mode,
  agent,
  published,
  onClose,
  onSuccess,
}: PublishExpertDrawerProps) {
  const { t, i18n } = useTranslation();
  const [form] = Form.useForm<PublishFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [prefillLoading, setPrefillLoading] = useState(false);
  const [share, setShare] = useState<SharePreview>(EMPTY_SHARE);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;

    const loadDefaults = async () => {
      setPrefillLoading(true);
      setShare(EMPTY_SHARE);
      const baseName =
        mode === "refresh" && published ? published.name : agent.name;
      const baseDescription =
        mode === "refresh" && published
          ? published.description
          : agent.description || "";

      try {
        const [welcome, skills, bases, instances, catalog] = await Promise.all([
          agentChatApi.welcome(agent.agent_id).catch(() => null),
          request<SkillSummary[]>(
            "/agents/" + encodeURIComponent(agent.agent_id) + "/skills",
          ).catch(() => [] as SkillSummary[]),
          knowledgeBasesApi.list().catch(() => []),
          connectorsApi.listInstances().catch(() => []),
          connectorsApi.catalog().catch(() => []),
        ]);
        if (cancelled) return;
        const knowledgeById = new Map(bases.map((base) => [base.id, base.name]));
        const connectorByName = new Map(
          (instances ?? []).map((item) => [
            item.mcp_server_name,
            item.display_name || item.mcp_server_name,
          ]),
        );
        const connectorByKind = new Map(
          (catalog ?? []).map((item) => [item.kind, item.name]),
        );
        const connectorNames = (agent.mcp_servers ?? []).map((ref) => {
          const exact = connectorByName.get(ref);
          if (exact) return exact;
          const kind = ref.includes("__") ? ref.split("__")[0] : ref;
          return connectorByKind.get(kind) || ref;
        });
        setShare({
          skills: uniqueNames(
            (skills ?? [])
              .filter((skill) => skill.kind !== "builtin")
              .map((skill) =>
                resolveSkillDisplayName(
                  {
                    slug: skill.slug || skill.name,
                    name: skill.name,
                    label: skill.label,
                    display_name: skill.display_name,
                  },
                  i18n.language,
                ),
              ),
          ),
          connectors: uniqueNames(connectorNames),
          knowledgeBases: uniqueNames(
            (agent.knowledge_base_ids ?? []).map(
              (id) => knowledgeById.get(id) || id,
            ),
          ),
        });
        form.setFieldsValue({
          name: baseName,
          description: baseDescription,
          welcome_zh: welcome
            ? pickLocale(welcome.welcome_message, "zh")
            : "",
          welcome_en: welcome
            ? pickLocale(welcome.welcome_message, "en")
            : "",
        });
      } finally {
        if (!cancelled) setPrefillLoading(false);
      }
    };

    void loadDefaults();
    return () => {
      cancelled = true;
    };
  }, [open, mode, agent, published, form, i18n.language]);

  const buildBody = (values: PublishFormValues): PublishExpertBody => ({
    name: values.name.trim(),
    description: values.description?.trim() || "",
    welcome_message: {
      zh: values.welcome_zh?.trim() || "",
      en: values.welcome_en?.trim() || "",
    },
  });

  const handleSubmit = async () => {
    let values: PublishFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    setSubmitting(true);
    try {
      const body = buildBody(values);
      if (mode === "refresh" && published) {
        await publishedExpertsApi.refresh(published.id, body);
        message.success(t("experts.published.updateSuccess"));
      } else {
        await publishedExpertsApi.publish(agent.agent_id, body);
        message.success(t("experts.published.publishSuccessHint"));
      }
      onSuccess();
      onClose();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          mode === "refresh"
            ? t("experts.published.updateFailed")
            : t("experts.published.publishFailed"),
          t,
        ),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const primaryLabel =
    mode === "refresh"
      ? t("experts.published.update")
      : t("experts.published.publish");

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={440}
      destroyOnHidden
      maskClosable={!submitting}
      closable={!submitting}
      title={
        mode === "refresh"
          ? t("experts.published.update")
          : t("experts.published.drawerTitle")
      }
      footer={
        <div className={styles.publishDrawerFooter}>
          <Button onClick={onClose} disabled={submitting}>
            {t("common.cancel")}
          </Button>
          <Button
            type="primary"
            loading={submitting}
            disabled={prefillLoading}
            onClick={() => void handleSubmit()}
          >
            {primaryLabel}
          </Button>
        </div>
      }
    >
      <div className={styles.publishDrawerBody}>
        {prefillLoading ? (
          <div className={styles.publishDrawerLoading}>
            <Spin size="small" />
          </div>
        ) : null}

        <Alert
          type="info"
          showIcon
          className={styles.publishDrawerHint}
          message={t("experts.published.drawerHint")}
        />
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          disabled={prefillLoading || submitting}
        >
          <Form.Item
            name="name"
            label={t("experts.published.fieldName")}
            rules={[
              {
                required: true,
                message: t("experts.pleaseEnterName"),
              },
            ]}
          >
            <Input placeholder={t("experts.agentNameLabel")} />
          </Form.Item>
          <Form.Item
            name="description"
            label={t("experts.published.fieldDescription")}
          >
            <Input.TextArea
              rows={3}
              placeholder={t("experts.table.description")}
            />
          </Form.Item>
          <Form.Item
            name="welcome_zh"
            label={t("experts.published.fieldWelcomeZh")}
          >
            <Input.TextArea
              rows={2}
              placeholder={t("experts.published.fieldWelcomePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="welcome_en"
            label={t("experts.published.fieldWelcomeEn")}
          >
            <Input.TextArea
              rows={2}
              placeholder={t("experts.published.fieldWelcomePlaceholder")}
            />
          </Form.Item>
        </Form>
        {!prefillLoading ? (
        <div className={styles.publishShare}>
          <ShareRow
            label={t("experts.published.shareSkills")}
            note={t("experts.published.shareSkillsNote")}
            items={share.skills}
            empty={t("experts.published.shareNone")}
          />
          <ShareRow
            label={t("experts.published.shareConnectors")}
            note={t("experts.published.shareConnectorsNote")}
            items={share.connectors}
            empty={t("experts.published.shareNone")}
          />
          <ShareRow
            label={t("experts.published.shareKnowledge")}
            note={t("experts.published.shareKnowledgeNote")}
            items={share.knowledgeBases}
            empty={t("experts.published.shareNone")}
          />
        </div>
        ) : null}
      </div>
    </Drawer>
  );
}
