import { useEffect, useRef } from "react";
import { Result } from "antd";
import { useTranslation } from "react-i18next";

export default function FeishuKbCallback() {
  const { t } = useTranslation();
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.search);
    const state = params.get("state") || "";
    const code = params.has("error") ? "" : params.get("code") || "";
    window.history.replaceState(null, "", window.location.pathname);
    if (window.opener) {
      window.opener.postMessage(
        { type: "octop-feishu-kb-callback", state, code },
        window.location.origin,
      );
      window.close();
    }
  }, []);
  return <Result title={t("feishuKb.verifying")} />;
}
