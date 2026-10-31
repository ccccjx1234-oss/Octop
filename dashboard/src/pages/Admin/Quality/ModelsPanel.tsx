import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  qualityApi,
  type CostReport,
  type ModelHealthResponse,
  type ModelPrice,
} from "../../../api/modules/quality";
import { message } from "@/utils/antdMessage";

const WINDOWS = ["today", "last_7d", "last_30d"] as const;

function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export default function ModelsPanel() {
  const { t } = useTranslation();
  const [health, setHealth] = useState<ModelHealthResponse | null>(null);
  const [chain, setChain] = useState<string[]>([]);
  const [prices, setPrices] = useState<ModelPrice[]>([]);
  const [cost, setCost] = useState<CostReport | null>(null);
  const [window, setWindow] = useState<string>("last_30d");
  const [form] = Form.useForm<ModelPrice>();

  const load = useCallback(async () => {
    try {
      const [h, p, c] = await Promise.all([
        qualityApi.modelHealth(),
        qualityApi.pricing(),
        qualityApi.cost(window),
      ]);
      setHealth(h);
      setChain(h.fallback_chain);
      setPrices(p);
      setCost(c);
    } catch (err) {
      message.error(errorText(err));
    }
  }, [window]);

  useEffect(() => {
    void load();
  }, [load]);

  const saveChain = async () => {
    try {
      const res = await qualityApi.saveFallbackChain(chain);
      setChain(res.fallback_chain);
      message.success(t("quality.common.saved"));
    } catch (err) {
      message.error(errorText(err));
    }
  };

  const savePrice = async (values: ModelPrice) => {
    try {
      await qualityApi.savePrice({
        ...values,
        input_per_mtok: values.input_per_mtok ?? 0,
        output_per_mtok: values.output_per_mtok ?? 0,
        cache_read_per_mtok: values.cache_read_per_mtok ?? 0,
        currency: values.currency || "CNY",
      });
      form.resetFields();
      message.success(t("quality.common.saved"));
      await load();
    } catch (err) {
      message.error(errorText(err));
    }
  };

  const windowMinutes = Math.round((health?.models[0]?.window_s ?? 600) / 60);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        title={t("quality.models.healthTitle", { minutes: windowMinutes })}
        extra={
          <Button icon={<RefreshCw size={14} />} onClick={() => void load()}>
            {t("quality.common.refresh")}
          </Button>
        }
      >
        <Table
          rowKey="ref"
          size="small"
          pagination={false}
          scroll={{ x: true }}
          dataSource={health?.models ?? []}
          locale={{ emptyText: t("quality.models.healthEmpty") }}
          columns={[
            { title: t("quality.models.ref"), dataIndex: "ref" },
            {
              title: t("quality.models.state"),
              dataIndex: "state",
              render: (state: string) =>
                state === "healthy" ? (
                  <Tag color="green">{t("quality.models.healthy")}</Tag>
                ) : (
                  <Tag color="orange">{t("quality.models.open")}</Tag>
                ),
            },
            { title: t("quality.models.calls"), dataIndex: "calls" },
            {
              title: t("quality.models.successRate"),
              dataIndex: "success_rate",
              render: (v: number | null) => percent(v),
            },
          ]}
        />
      </Card>

      <Card title={t("quality.models.chainTitle")}>
        <Typography.Paragraph type="secondary">
          {t("quality.models.chainHint")}
        </Typography.Paragraph>
        <Space.Compact style={{ width: "100%" }}>
          <Select
            mode="tags"
            style={{ width: "100%" }}
            value={chain}
            onChange={setChain}
            placeholder={t("quality.models.chainPlaceholder")}
            tokenSeparators={[",", " "]}
          />
          <Button type="primary" onClick={() => void saveChain()}>
            {t("quality.common.save")}
          </Button>
        </Space.Compact>
      </Card>

      <Card title={t("quality.models.pricingTitle")}>
        <Typography.Paragraph type="secondary">
          {t("quality.models.pricingHint")}
        </Typography.Paragraph>
        <Form
          form={form}
          layout="inline"
          onFinish={(values) => void savePrice(values)}
          initialValues={{ currency: "CNY" }}
          style={{ rowGap: 8, marginBottom: 12 }}
        >
          <Form.Item
            name="model"
            rules={[{ required: true }]}
            style={{ minWidth: 220 }}
          >
            <Input placeholder={t("quality.models.model")} />
          </Form.Item>
          <Form.Item name="input_per_mtok">
            <InputNumber min={0} placeholder={t("quality.models.input")} />
          </Form.Item>
          <Form.Item name="output_per_mtok">
            <InputNumber min={0} placeholder={t("quality.models.output")} />
          </Form.Item>
          <Form.Item name="cache_read_per_mtok">
            <InputNumber min={0} placeholder={t("quality.models.cacheRead")} />
          </Form.Item>
          <Form.Item name="currency" style={{ width: 90 }}>
            <Input placeholder={t("quality.models.currency")} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit">
              {t("quality.common.save")}
            </Button>
          </Form.Item>
        </Form>
        <Table
          rowKey="model"
          size="small"
          pagination={false}
          scroll={{ x: true }}
          dataSource={prices}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          onRow={(row) => ({ onDoubleClick: () => form.setFieldsValue(row) })}
          columns={[
            { title: t("quality.models.model"), dataIndex: "model" },
            { title: t("quality.models.input"), dataIndex: "input_per_mtok" },
            { title: t("quality.models.output"), dataIndex: "output_per_mtok" },
            {
              title: t("quality.models.cacheRead"),
              dataIndex: "cache_read_per_mtok",
            },
            { title: t("quality.models.currency"), dataIndex: "currency" },
            {
              title: "",
              key: "actions",
              render: (_: unknown, row: ModelPrice) => (
                <Space>
                  <Button size="small" onClick={() => form.setFieldsValue(row)}>
                    {t("quality.common.edit")}
                  </Button>
                  <Popconfirm
                    title={t("quality.common.delete")}
                    onConfirm={async () => {
                      await qualityApi.deletePrice(row.model);
                      await load();
                    }}
                  >
                    <Button size="small" danger>
                      {t("quality.common.delete")}
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title={
          cost
            ? `${t("quality.models.costTitle")} · ${t(
                "quality.models.total",
              )} ${cost.total_cost} ${cost.currency}`
            : t("quality.models.costTitle")
        }
        extra={
          <Segmented
            value={window}
            onChange={(v) => setWindow(String(v))}
            options={WINDOWS.map((w) => ({
              value: w,
              label: t(`quality.models.windows.${w}`),
            }))}
          />
        }
      >
        <Typography.Title level={5}>
          {t("quality.models.byModel")}
        </Typography.Title>
        <Table
          rowKey="model"
          size="small"
          pagination={false}
          scroll={{ x: true }}
          dataSource={cost?.by_model ?? []}
          columns={[
            { title: t("quality.models.model"), dataIndex: "model" },
            { title: t("quality.models.tokens"), dataIndex: "total_tokens" },
            {
              title: t("quality.models.cost"),
              dataIndex: "cost",
              render: (v: number, row) =>
                row.priced ? v : <Tag>{t("quality.models.unpriced")}</Tag>,
            },
          ]}
        />
        <Typography.Title level={5} style={{ marginTop: 16 }}>
          {t("quality.models.byUser")}
        </Typography.Title>
        <Table
          rowKey="user_id"
          size="small"
          pagination={false}
          scroll={{ x: true }}
          dataSource={cost?.by_user ?? []}
          columns={[
            { title: t("quality.models.user"), dataIndex: "username" },
            { title: t("quality.models.tokens"), dataIndex: "total_tokens" },
            { title: t("quality.models.cost"), dataIndex: "cost" },
          ]}
        />
      </Card>
    </Space>
  );
}
