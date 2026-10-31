import { useState, useEffect, useRef, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Input, Button, Checkbox } from "antd";
import { message } from "@/utils/antdMessage";

import {
  BookOpen,
  Bot,
  Clock,
  KeyRound,
  Lock,
  Plug,
  ShieldCheck,
  User,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  clearAuthToken,
  setAuthToken,
  setRememberLoginPreference,
} from "../../api";
import {
  authApi,
  type LdapStatus,
  type OauthProviderStatus,
} from "../../api/modules/auth";
import { apiErrorMessage } from "../../utils/apiError";
import { publicUrl } from "../../utils/publicUrl";
import ThemeSwitcher from "../../components/ThemeSwitcher";
import styles from "./index.module.less";
import { refreshServerLabels } from "../../i18n";
import { applyUserLocale, applyGuestLocale } from "../../utils/locale";
import { useTheme } from "../../context/ThemeContext";
import {
  isSsoPopup,
  isSsoPopupMessage,
  notifySsoOpener,
  openSsoPopup,
} from "../../utils/ssoPopup";
import feishuIcon from "../../assets/channels/feishu.svg";
import dingtalkIcon from "../../assets/channels/dingtalk.svg";
import wecomIcon from "../../assets/channels/wecom.svg";
import googleIcon from "../../assets/providers/google.svg";
import CaptchaField, { type CaptchaFieldHandle } from "./CaptchaField";
import ForgotPasswordModal from "./ForgotPasswordModal";
import { type PublicCaptchaConfig } from "./captchaAdapters";

function providerLabel(
  provider: OauthProviderStatus,
  t: (key: string, opts?: Record<string, string>) => string,
): string {
  const name = provider.display_name.trim();
  if (name) return name;
  return t(`login.providerKind.${provider.kind}`, {
    defaultValue: provider.kind,
  });
}

function providerIcon(provider: OauthProviderStatus): ReactNode {
  if (provider.kind === "feishu") {
    return (
      <img src={feishuIcon} alt="" width={18} height={18} draggable={false} />
    );
  }
  if (provider.kind === "dingtalk") {
    return (
      <img src={dingtalkIcon} alt="" width={18} height={18} draggable={false} />
    );
  }
  if (provider.kind === "wecom") {
    return (
      <img src={wecomIcon} alt="" width={18} height={18} draggable={false} />
    );
  }
  const name = provider.display_name.trim().toLowerCase();
  if (
    provider.kind === "oidc" &&
    (name === "google" || name.includes("google"))
  ) {
    return (
      <img src={googleIcon} alt="" width={18} height={18} draggable={false} />
    );
  }
  return <KeyRound size={18} />;
}

export default function LoginPage() {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [loading, setLoading] = useState(false);
  const [providers, setProviders] = useState<OauthProviderStatus[]>([]);
  const [ldap, setLdap] = useState<LdapStatus | null>(null);
  const [ssoLoadingKind, setSsoLoadingKind] = useState<string | null>(null);
  const [captchaReady, setCaptchaReady] = useState(false);
  const [captchaResetKey, setCaptchaResetKey] = useState(0);
  const [showForgotHelp, setShowForgotHelp] = useState(false);
  const [captcha, setCaptcha] = useState<PublicCaptchaConfig>({
    provider: "slider",
  });
  const captchaRef = useRef<CaptchaFieldHandle>(null);

  useEffect(() => {
    void applyGuestLocale();
  }, []);

  useEffect(() => {
    let cancelled = false;
    authApi
      .getAuthStatus()
      .then((status) => {
        if (cancelled) return;
        if (status.setup_required) {
          clearAuthToken();
          navigate("/setup", { replace: true });
          return;
        }
        // Only probe OIDC / captcha after setup is done — otherwise lockdown 503s.
        authApi
          .getOauthStatus()
          .then((next) => {
            if (!cancelled) {
              setProviders(next.providers.filter((item) => item.enabled));
            }
          })
          .catch(() => {});
        authApi
          .getLdapStatus()
          .then((next) => {
            if (!cancelled) setLdap(next);
          })
          .catch(() => {});
        authApi
          .getCaptcha()
          .then((next) => {
            if (!cancelled) setCaptcha(next);
          })
          .catch(() => {
            if (!cancelled) setCaptcha({ provider: "slider" });
          });
      })
      .catch(() => {
        // Backend unreachable — let the user attempt login and show a real
        // error from the request itself; redirecting blindly to /setup
        // would mask the actual problem.
      });
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  useEffect(() => {
    const code = searchParams.get("oidc_error");
    if (!code) return;
    if (notifySsoOpener({ ok: false, error: code })) return;
    message.error(
      t(`login.oidcError.${code}`, {
        defaultValue: t("login.oidcError.generic"),
      }),
    );
    navigate("/login", { replace: true });
  }, [navigate, searchParams, t]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!isSsoPopupMessage(event, window.location.origin) || !event.data.ok) {
        if (
          isSsoPopupMessage(event, window.location.origin) &&
          !event.data.ok
        ) {
          setSsoLoadingKind(null);
          const code = event.data.error || "generic";
          message.error(
            t(`login.oidcError.${code}`, {
              defaultValue: t("login.oidcError.generic"),
            }),
          );
        }
        return;
      }
      if (event.data.access_token) {
        setAuthToken(event.data.access_token, event.data.remember ?? true);
      }
      window.location.replace(event.data.redirect || "/chat");
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [t]);

  const resetCaptcha = () => {
    setCaptchaReady(false);
    setCaptchaResetKey((k) => k + 1);
  };

  const onSso = async (kind: string) => {
    setSsoLoadingKind(kind);
    setRememberLoginPreference(remember);
    let popup: Window | null = null;
    if (kind !== "oidc") {
      popup = openSsoPopup();
    }
    try {
      const { authorization_url } = await authApi.startOauth(kind, "/chat");
      if (kind === "oidc") {
        window.location.href = authorization_url;
        return;
      }
      if (popup && !popup.closed) {
        popup.location.href = authorization_url;
        const timer = window.setInterval(() => {
          if (!popup || popup.closed) {
            window.clearInterval(timer);
            setSsoLoadingKind((current) => (current === kind ? null : current));
          }
        }, 400);
      } else {
        popup?.close();
        message.error(t("account.ssoPopupBlocked"));
        setSsoLoadingKind(null);
      }
    } catch (err) {
      popup?.close();
      message.error(apiErrorMessage(err, t("login.oidcStartFailed"), t));
      setSsoLoadingKind(null);
    }
  };

  const handleLogin = async () => {
    if (!username || !password || !captchaReady) return;
    setLoading(true);
    try {
      const token = await captchaRef.current?.getToken();
      const res = await authApi.login(username, password, token);
      setAuthToken(res.access_token, remember);
      await applyUserLocale(res.user.locale);
      void refreshServerLabels(res.user.locale);
      navigate("/chat", { replace: true });
    } catch (err) {
      message.error(apiErrorMessage(err, t("login.failed"), t));
      resetCaptcha();
    } finally {
      setLoading(false);
    }
  };

  const features = [
    {
      icon: <Bot size={17} strokeWidth={1.8} />,
      title: t("login.featureExperts"),
      desc: t("login.featureExpertsDesc"),
    },
    {
      icon: <BookOpen size={17} strokeWidth={1.8} />,
      title: t("login.featureKnowledge"),
      desc: t("login.featureKnowledgeDesc"),
    },
    {
      icon: <Plug size={17} strokeWidth={1.8} />,
      title: t("login.featureConnectors"),
      desc: t("login.featureConnectorsDesc"),
    },
    {
      icon: <Clock size={17} strokeWidth={1.8} />,
      title: t("login.featureAutomation"),
      desc: t("login.featureAutomationDesc"),
    },
  ];

  if (isSsoPopup() && searchParams.get("oidc_error")) {
    return null;
  }

  return (
    <div className={styles.shell}>
      <div className={styles.themeCorner}>
        <ThemeSwitcher compact />
      </div>
      <div className={styles.panel}>
        <aside className={styles.brandPane}>
          <img
            className={styles.watermark}
            src={publicUrl("logo_vertical_white.svg")}
            alt=""
            aria-hidden="true"
            draggable={false}
          />
          <div className={styles.brandContent}>
            <div className={styles.brandTop}>
              <img
                className={styles.brandWordmark}
                src={
                  isDark
                    ? publicUrl("logo_horizontal_white.png")
                    : publicUrl("logo_horizontal_dark.png")
                }
                alt="会火"
              />
            </div>
            <h1 className={styles.brandTitle}>{t("login.platformName")}</h1>
            <p className={styles.brandDesc}>{t("login.brandDesc")}</p>
            <div className={styles.featureList}>
              {features.map((feature) => (
                <div key={feature.title} className={styles.featureCard}>
                  <span className={styles.featureIcon}>{feature.icon}</span>
                  <div>
                    <div className={styles.featureTitle}>{feature.title}</div>
                    <div className={styles.featureDesc}>{feature.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className={styles.brandFoot}>
            <ShieldCheck size={14} strokeWidth={1.8} />
            <span>{t("login.brandFoot")}</span>
          </div>
        </aside>

        <section className={styles.formPane}>
          <div className={styles.form}>
            <div className={styles.mobileBrand}>
              <img
                className={styles.mobileWordmark}
                src={
                  isDark
                    ? publicUrl("logo_horizontal_white.png")
                    : publicUrl("logo_horizontal_dark.png")
                }
                alt="会火"
              />
              <div className={styles.mobilePlatform}>
                {t("login.platformName")}
              </div>
              <div className={styles.mobilePills}>
                {features.map((feature) => (
                  <span key={feature.title} className={styles.mobilePill}>
                    {feature.title}
                  </span>
                ))}
              </div>
            </div>

            <div className={styles.formHead}>
              <h2 className={styles.formTitle}>{t("login.formTitle")}</h2>
              <p className={styles.formSubtitle}>{t("login.formSubtitle")}</p>
            </div>

            <Input
              prefix={
                <User
                  size={16}
                  style={{ color: "var(--fn-text-quaternary)" }}
                />
              }
              placeholder={t("login.username")}
              size="large"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              style={{ borderRadius: 10 }}
            />

            <Input.Password
              prefix={
                <Lock
                  size={16}
                  style={{ color: "var(--fn-text-quaternary)" }}
                />
              }
              placeholder={t("login.password")}
              size="large"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onPressEnter={handleLogin}
              style={{ borderRadius: 10 }}
            />

            {ldap?.enabled && (
              <p
                style={{
                  margin: 0,
                  marginTop: -8,
                  width: "100%",
                  fontSize: 12,
                  lineHeight: 1.5,
                  color: "var(--fn-text-tertiary)",
                  textAlign: "center",
                }}
              >
                {t("login.ldapHint", {
                  name: ldap.display_name.trim() || t("adminSso.ldap.kind"),
                })}
              </p>
            )}

            <CaptchaField
              ref={captchaRef}
              config={captcha}
              resetKey={captchaResetKey}
              slideHint={t("login.slideHint")}
              slideVerifiedLabel={t("login.slideVerified")}
              unsupportedLabel={t("login.unsupportedCaptcha")}
              onReadyChange={setCaptchaReady}
            />

            <Button
              type="primary"
              size="large"
              block
              loading={loading}
              onClick={handleLogin}
              disabled={!username || !password || !captchaReady}
              style={{ borderRadius: 10, height: 44, fontWeight: 500 }}
            >
              {t("login.submit")}
            </Button>

            <div
              style={{
                width: "100%",
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  width: "100%",
                }}
              >
                <Checkbox
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                  style={{
                    margin: 0,
                    fontSize: 13,
                    color: "var(--fn-text-tertiary)",
                  }}
                >
                  {t("login.remember")}
                </Checkbox>
                <button
                  type="button"
                  data-testid="login-forgot-password-toggle"
                  onClick={() => setShowForgotHelp(true)}
                  style={{
                    margin: 0,
                    padding: 0,
                    border: "none",
                    background: "none",
                    cursor: "pointer",
                    fontSize: 13,
                    lineHeight: 1.5,
                    color: "var(--fn-text-tertiary)",
                    flexShrink: 0,
                  }}
                >
                  {t("login.forgotPassword", "Forgot password?")}
                </button>
              </div>
              <ForgotPasswordModal
                open={showForgotHelp}
                onClose={() => setShowForgotHelp(false)}
              />
            </div>

            {providers.length > 0 && (
              <>
                <div
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    color: "var(--fn-text-tertiary)",
                    fontSize: 13,
                  }}
                >
                  <span
                    style={{
                      flex: 1,
                      height: 1,
                      background: "var(--fn-border-primary)",
                    }}
                  />
                  {t("login.or")}
                  <span
                    style={{
                      flex: 1,
                      height: 1,
                      background: "var(--fn-border-primary)",
                    }}
                  />
                </div>
                {providers.map((provider) => (
                  <Button
                    key={provider.kind}
                    size="large"
                    block
                    icon={providerIcon(provider)}
                    loading={ssoLoadingKind === provider.kind}
                    onClick={() => void onSso(provider.kind)}
                    style={{ borderRadius: 10, height: 44, fontWeight: 500 }}
                  >
                    {t("login.oidcWith", { name: providerLabel(provider, t) })}
                  </Button>
                ))}
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
