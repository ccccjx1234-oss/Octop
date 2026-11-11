import { useEffect, useRef, useState } from "react";
import { Button, Result, Spin } from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  getRememberLoginPreference,
  isSessionOnlyAuth,
  setAuthToken,
} from "../../api";
import { authApi } from "../../api/modules/auth";
import { refreshServerLabels } from "../../i18n";
import { apiErrorMessage } from "../../utils/apiError";
import { applyUserLocale } from "../../utils/locale";

const DEFAULT_REDIRECT = "/chat";

/** Read the handoff code from the fragment first; the query is a legacy fallback. */
export function readYouliCompleteParams(hash: string, search: string) {
  const fromHash = new URLSearchParams(
    hash.startsWith("#") ? hash.slice(1) : hash,
  );
  const fromQuery = new URLSearchParams(
    search.startsWith("?") ? search.slice(1) : search,
  );
  const pick = (key: string) => fromHash.get(key) || fromQuery.get(key) || "";
  return {
    code: pick("code"),
    state: pick("state"),
    requestId: pick("request_id"),
  };
}

export default function YouliComplete() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const did = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (did.current) return;
    did.current = true;

    const { code, state, requestId } = readYouliCompleteParams(
      window.location.hash,
      window.location.search,
    );
    if (!code || !state || !requestId) {
      setError(
        t("login.youli.missingCode", {
          defaultValue: "登录回调参数不完整，请重新发起登录。",
        }),
      );
      return;
    }
    if (window.location.hash || window.location.search) {
      window.history.replaceState(null, "", window.location.pathname);
    }

    void authApi
      .exchangeYouliCode({ code, state, request_id: requestId })
      .then(async (res) => {
        const remember = getRememberLoginPreference();
        setAuthToken(res.access_token, remember || !isSessionOnlyAuth());
        await applyUserLocale(res.user.locale);
        void refreshServerLabels(res.user.locale);
        navigate(DEFAULT_REDIRECT, { replace: true });
      })
      .catch((err) => {
        setError(
          apiErrorMessage(
            err,
            t("login.youli.failed", {
              defaultValue: "登录交接失败，请重新进入。",
            }),
            t,
          ),
        );
      });
  }, [navigate, t]);

  if (error) {
    return (
      <Result
        status="error"
        title={t("login.youli.title", {
          defaultValue: "正在完成登录",
        })}
        subTitle={error}
        extra={
          <Button
            type="primary"
            onClick={() => navigate("/login", { replace: true })}
          >
            {t("login.oidcComplete.backToLogin", {
              defaultValue: "返回登录页",
            })}
          </Button>
        }
      />
    );
  }

  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Spin
        tip={t("login.youli.title", {
          defaultValue: "正在完成登录",
        })}
      />
    </div>
  );
}

