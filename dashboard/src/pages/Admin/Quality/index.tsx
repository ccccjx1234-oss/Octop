import { Tabs } from "antd";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import PageShell from "../../../layouts/PageShell";
import EvalsPanel from "./EvalsPanel";
import FeedbackPanel from "./FeedbackPanel";
import ModelsPanel from "./ModelsPanel";
import ToolsPanel from "./ToolsPanel";

const TABS = ["models", "feedback", "evals", "tools"] as const;
type TabKey = (typeof TABS)[number];

export default function AdminQualityPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const active: TabKey = (TABS as readonly string[]).includes(raw ?? "")
    ? (raw as TabKey)
    : "models";

  return (
    <PageShell
      title={t("pageShell.adminQuality.title")}
      subtitle={t("pageShell.adminQuality.subtitle")}
    >
      <Tabs
        activeKey={active}
        onChange={(key) => setParams({ tab: key }, { replace: true })}
        destroyOnHidden
        items={[
          {
            key: "models",
            label: t("quality.tabs.models"),
            children: <ModelsPanel />,
          },
          {
            key: "feedback",
            label: t("quality.tabs.feedback"),
            children: <FeedbackPanel />,
          },
          {
            key: "evals",
            label: t("quality.tabs.evals"),
            children: <EvalsPanel />,
          },
          {
            key: "tools",
            label: t("quality.tabs.tools"),
            children: <ToolsPanel />,
          },
        ]}
      />
    </PageShell>
  );
}
