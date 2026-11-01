import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import { Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  qualityApi,
  type EvalCase,
  type EvalCaseInput,
  type EvalResult,
  type EvalRun,
} from "../../../api/modules/quality";
import { useAgent } from "../../../context/AgentContext";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { message } from "@/utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";

interface CaseForm {
  suite: string;
  prompt: string;
  expect_contains: string;
  expect_not_contains: string;
}

const lines = (text: string | undefined): string[] =>
  (text ?? "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);

const STATUS_COLOR: Record<EvalRun["status"], string> = {
  running: "processing",
  done: "green",
  failed: "red",
};

export default function EvalsPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const { agents } = useAgent();
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [editing, setEditing] = useState<EvalCase | "new" | null>(null);
  const [runAgent, setRunAgent] = useState<string>();
  const [runSuite, setRunSuite] = useState<string>("default");
  const [detail, setDetail] = useState<
    (EvalRun & { results: EvalResult[] }) | null
  >(null);
  const [form] = Form.useForm<CaseForm>();

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([
        qualityApi.evalCases(),
        qualityApi.evalRuns(),
      ]);
      setCases(c);
      setRuns(r);
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("quality.common.requestFailed", "Request failed"),
          t,
        ),
      );
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while any run is still going.
  const hasRunning = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!hasRunning) return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [hasRunning, load]);

  const suites = useMemo(
    () => Array.from(new Set(cases.map((c) => c.suite))).sort(),
    [cases],
  );
  const agentName = (id: string) =>
    agents.find((a) => a.agent_id === id)?.name ?? id;

  const openEditor = (row: EvalCase | "new") => {
    setEditing(row);
    form.setFieldsValue(
      row === "new"
        ? {
            suite: runSuite || "default",
            prompt: "",
            expect_contains: "",
            expect_not_contains: "",
          }
        : {
            suite: row.suite,
            prompt: row.prompt,
            expect_contains: row.expect_contains.join("\n"),
            expect_not_contains: row.expect_not_contains.join("\n"),
          },
    );
  };

  const saveCase = async () => {
    const values = await form.validateFields();
    const input: EvalCaseInput = {
      suite: values.suite,
      prompt: values.prompt,
      expect_contains: lines(values.expect_contains),
      expect_not_contains: lines(values.expect_not_contains),
    };
    try {
      if (editing === "new") await qualityApi.createEvalCase(input);
      else if (editing)
        await qualityApi.updateEvalCase(editing.eval_case_id, input);
      setEditing(null);
      await load();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("quality.common.requestFailed", "Request failed"),
          t,
        ),
      );
    }
  };

  const startRun = async () => {
    if (!runAgent) return;
    try {
      await qualityApi.startEvalRun(runAgent, runSuite);
      await load();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("quality.common.requestFailed", "Request failed"),
          t,
        ),
      );
    }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card title={t("quality.evals.runTitle")}>
        <Space wrap>
          <Select
            style={{ minWidth: 200 }}
            placeholder={t("quality.evals.expert")}
            value={runAgent}
            onChange={setRunAgent}
            options={agents.map((a) => ({ value: a.agent_id, label: a.name }))}
          />
          <Select
            style={{ minWidth: 160 }}
            value={runSuite}
            onChange={setRunSuite}
            options={(suites.length ? suites : ["default"]).map((s) => ({
              value: s,
              label: s,
            }))}
          />
          <Button
            type="primary"
            disabled={!runAgent || cases.length === 0}
            onClick={() => void startRun()}
          >
            {t("quality.evals.run")}
          </Button>
        </Space>
      </Card>

      <Card
        title={t("quality.evals.runsTitle")}
        extra={
          <Button icon={<RefreshCw size={14} />} onClick={() => void load()}>
            {t("quality.common.refresh")}
          </Button>
        }
      >
        <Table
          rowKey="eval_run_id"
          size="small"
          scroll={{ x: true }}
          dataSource={runs}
          columns={[
            {
              title: t("quality.evals.started"),
              dataIndex: "started_at",
              render: (v: number) => formatServerDateTime(v, timeZone),
            },
            {
              title: t("quality.evals.expert"),
              dataIndex: "agent_id",
              render: (id: string) => agentName(id),
            },
            { title: t("quality.evals.suite"), dataIndex: "suite" },
            {
              title: t("quality.evals.status"),
              dataIndex: "status",
              render: (s: EvalRun["status"], row) => (
                <Tag color={STATUS_COLOR[s]} title={row.error || undefined}>
                  {t(`quality.evals.statuses.${s}`)}
                </Tag>
              ),
            },
            {
              title: t("quality.evals.passed"),
              key: "passed",
              render: (_: unknown, row: EvalRun) =>
                `${row.passed} / ${row.total}`,
            },
            {
              title: "",
              key: "detail",
              render: (_: unknown, row: EvalRun) => (
                <Button
                  size="small"
                  onClick={async () =>
                    setDetail(await qualityApi.evalRun(row.eval_run_id))
                  }
                >
                  {t("quality.evals.detail")}
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title={t("quality.evals.casesTitle")}
        extra={
          <Button icon={<Plus size={14} />} onClick={() => openEditor("new")}>
            {t("quality.evals.addCase")}
          </Button>
        }
      >
        <Table
          rowKey="eval_case_id"
          size="small"
          scroll={{ x: true }}
          dataSource={cases}
          locale={{ emptyText: t("quality.evals.noCases") }}
          columns={[
            { title: t("quality.evals.suite"), dataIndex: "suite" },
            {
              title: t("quality.evals.prompt"),
              dataIndex: "prompt",
              ellipsis: true,
            },
            {
              title: t("quality.evals.expectContains"),
              dataIndex: "expect_contains",
              render: (v: string[]) => v.map((p) => <Tag key={p}>{p}</Tag>),
            },
            {
              title: t("quality.evals.expectNotContains"),
              dataIndex: "expect_not_contains",
              render: (v: string[]) =>
                v.map((p) => (
                  <Tag key={p} color="red">
                    {p}
                  </Tag>
                )),
            },
            {
              title: "",
              key: "actions",
              render: (_: unknown, row: EvalCase) => (
                <Space>
                  <Button size="small" onClick={() => openEditor(row)}>
                    {t("quality.common.edit")}
                  </Button>
                  <Popconfirm
                    title={t("quality.common.delete")}
                    onConfirm={async () => {
                      await qualityApi.deleteEvalCase(row.eval_case_id);
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

      <Modal
        open={editing !== null}
        title={
          editing === "new"
            ? t("quality.evals.addCase")
            : t("quality.common.edit")
        }
        onCancel={() => setEditing(null)}
        onOk={() => void saveCase()}
        okText={t("quality.common.save")}
        cancelText={t("quality.common.cancel")}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item name="suite" label={t("quality.evals.suite")}>
            <Input />
          </Form.Item>
          <Form.Item
            name="prompt"
            label={t("quality.evals.prompt")}
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item
            name="expect_contains"
            label={t("quality.evals.expectContains")}
            extra={t("quality.evals.phrasesHint")}
          >
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item
            name="expect_not_contains"
            label={t("quality.evals.expectNotContains")}
            extra={t("quality.evals.phrasesHint")}
          >
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={720}
        title={
          detail
            ? `${agentName(detail.agent_id)} · ${detail.suite} · ${
                detail.passed
              }/${detail.total}`
            : ""
        }
      >
        <Table
          rowKey="eval_case_id"
          size="small"
          pagination={false}
          dataSource={detail?.results ?? []}
          expandable={{
            expandedRowRender: (row) => (
              <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                {row.output}
              </pre>
            ),
          }}
          columns={[
            {
              title: t("quality.evals.result"),
              dataIndex: "passed",
              render: (ok: boolean) =>
                ok ? (
                  <Tag color="green">{t("quality.evals.pass")}</Tag>
                ) : (
                  <Tag color="red">{t("quality.evals.fail")}</Tag>
                ),
            },
            {
              title: t("quality.evals.prompt"),
              dataIndex: "prompt",
              ellipsis: true,
            },
            { title: t("quality.evals.detail"), dataIndex: "detail" },
            {
              title: t("quality.evals.latency"),
              dataIndex: "latency_ms",
              render: (ms: number) => `${(ms / 1000).toFixed(1)}s`,
            },
          ]}
        />
      </Drawer>
    </Space>
  );
}
