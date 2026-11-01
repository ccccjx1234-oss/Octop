import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Select, Space, Table, Tag } from "antd";
import { useTranslation } from "react-i18next";
import {
  qualityApi,
  type ToolPermissionRow,
} from "../../../api/modules/quality";
import { userGroupsApi, type UserGroup } from "../../../api/modules/userGroups";
import { message } from "@/utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";

export default function ToolsPanel() {
  const { t } = useTranslation();
  const [tools, setTools] = useState<ToolPermissionRow[]>([]);
  const [groups, setGroups] = useState<UserGroup[]>([]);
  const [custom, setCustom] = useState("");

  const load = useCallback(async () => {
    try {
      const [res, g] = await Promise.all([
        qualityApi.toolPermissions(),
        userGroupsApi.list(),
      ]);
      setTools(res.tools);
      setGroups(g);
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

  const save = async (toolName: string, groupIds: string[]) => {
    try {
      await qualityApi.saveToolPermission(toolName, groupIds);
      setTools((rows) => {
        const exists = rows.some((r) => r.tool_name === toolName);
        const next = {
          tool_name: toolName,
          builtin: false,
          group_ids: groupIds,
        };
        return exists
          ? rows.map((r) =>
              r.tool_name === toolName ? { ...r, group_ids: groupIds } : r,
            )
          : [...rows, next];
      });
      message.success(t("quality.common.saved"));
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

  const groupOptions = groups.map((g) => ({
    value: g.user_group_id,
    label: g.enabled ? g.name : t("quality.tools.groupOff", { name: g.name }),
  }));

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert type="info" showIcon message={t("quality.tools.hint")} />
      <Space.Compact style={{ maxWidth: 480, width: "100%" }}>
        <Input
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder={t("quality.tools.custom")}
        />
        <Button
          disabled={
            !custom.trim() || tools.some((r) => r.tool_name === custom.trim())
          }
          onClick={() => {
            const name = custom.trim();
            setTools((rows) => [
              ...rows,
              { tool_name: name, builtin: false, group_ids: [] },
            ]);
            setCustom("");
          }}
        >
          {t("quality.tools.addCustom")}
        </Button>
      </Space.Compact>
      <Table
        rowKey="tool_name"
        size="small"
        scroll={{ x: true }}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        dataSource={tools}
        columns={[
          {
            title: t("quality.tools.tool"),
            dataIndex: "tool_name",
            render: (name: string) => <code>{name}</code>,
          },
          {
            title: t("quality.tools.allowed"),
            dataIndex: "group_ids",
            render: (ids: string[], row) => (
              <Space>
                <Select
                  mode="multiple"
                  allowClear
                  style={{ minWidth: 260 }}
                  value={ids}
                  options={groupOptions}
                  placeholder={t("quality.tools.everyone")}
                  onChange={(next: string[]) => void save(row.tool_name, next)}
                />
                {ids.length === 0 && <Tag>{t("quality.tools.everyone")}</Tag>}
              </Space>
            ),
          },
        ]}
      />
    </Space>
  );
}
