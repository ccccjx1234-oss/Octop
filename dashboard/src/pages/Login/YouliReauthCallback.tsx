import { useEffect, useRef } from "react";
import { Result } from "antd";
import { useTranslation } from "react-i18next";
import { authApi } from "../../api/modules/auth";

export default function YouliReauthCallback() {
  const { t } = useTranslation();
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.search);
    const state = params.get("state") || "";
    const code = params.get("code") || "";
    window.history.replaceState(null, "", window.location.pathname);
    const opener = window.opener;
    if (!opener) return;
    const send = (proof?: string) => {
      opener.postMessage(
        { type: "octop-youli-reauth", proof },
        window.location.origin,
      );
      window.close();
    };
    if (!state || !code) {
      send();
      return;
    }
    void authApi
      .completeYouliReauth(state, code)
      .then(({ proof }) => send(proof))
      .catch(() => send());
  }, []);
  return <Result title={t("account.youliVerifying")} />;
}
