import { useEffect, useState } from "react";
import { Card, Col, Row, Segmented, Space, Statistic, Table } from "antd";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { qualityApi, type FeedbackSummary } from "../../../api/modules/quality";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { message } from "@/utils/antdMessage";
import { formatServerDateTime } from "../../../utils/formatMessageTime";

const DAY_OPTIONS = [7, 30, 90];

function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function FeedbackPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [days, setDays] = useState(30);
  const [data, setData] = useState<FeedbackSummary | null>(null);

  useEffect(() => {
    qualityApi
      .feedback(days)
      .then(setData)
      .catch((err: unknown) =>
        message.error(err instanceof Error ? err.message : String(err)),
      );
  }, [days]);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Segmented
        value={days}
        onChange={(v) => setDays(Number(v))}
        options={DAY_OPTIONS.map((n) => ({
          value: n,
          label: t("quality.feedback.daysOption", { n }),
        }))}
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t("quality.feedback.up")} value={data?.up ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t("quality.feedback.down")}
              value={data?.down ?? 0}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t("quality.feedback.satisfaction")}
              value={percent(data?.satisfaction ?? null)}
            />
          </Card>
        </Col>
      </Row>
      <Card title={t("quality.feedback.perAgent")}>
        <Table
          rowKey="agent_id"
          size="small"
          pagination={false}
          scroll={{ x: true }}
          dataSource={data?.per_agent ?? []}
          locale={{ emptyText: t("quality.feedback.empty") }}
          columns={[
            { title: t("quality.feedback.expert"), dataIndex: "agent_name" },
            { title: t("quality.feedback.up"), dataIndex: "up" },
            { title: t("quality.feedback.down"), dataIndex: "down" },
            {
              title: t("quality.feedback.satisfaction"),
              dataIndex: "satisfaction",
              render: (v: number | null) => percent(v),
            },
          ]}
        />
      </Card>
      <Card title={t("quality.feedback.recentDown")}>
        <Table
          rowKey={(row) => `${row.thread_id}:${row.message_id}:${row.username}`}
          size="small"
          scroll={{ x: true }}
          dataSource={data?.recent_down ?? []}
          locale={{ emptyText: t("quality.feedback.empty") }}
          columns={[
            {
              title: t("quality.feedback.time"),
              dataIndex: "updated_at",
              render: (v: number) => formatServerDateTime(v, timeZone),
            },
            {
              title: t("quality.feedback.expert"),
              dataIndex: "agent_name",
              render: (name: string, row) => (
                <Link to={`/chat/${row.agent_id}/${row.thread_id}`}>
                  {name}
                </Link>
              ),
            },
            { title: t("quality.feedback.user"), dataIndex: "username" },
            { title: t("quality.feedback.comment"), dataIndex: "comment" },
            {
              title: t("quality.feedback.excerpt"),
              dataIndex: "excerpt",
              ellipsis: true,
            },
          ]}
        />
      </Card>
    </Space>
  );
}
