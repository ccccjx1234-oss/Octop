import { useEffect, useRef, useState, type FormEvent } from "react";
import { Alert, Button, Input, Modal } from "antd";
import {
  ArrowLeftRight,
  CheckCircle2,
  HelpCircle,
  Lock,
  Phone,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  authApi,
  type ErpBindChallengeResponse,
  type LoginResponse,
} from "../../api/modules/auth";
import feishuIcon from "../../assets/channels/feishu.svg";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";
import styles from "./FeishuErpBindModal.module.less";

interface FeishuErpBindModalProps {
  challenge: ErpBindChallengeResponse | null;
  onSuccess: (res: LoginResponse) => Promise<void> | void;
  onCancel: () => void;
  onRetryFeishu: () => void;
}

export default function FeishuErpBindModal({
  challenge,
  onSuccess,
  onCancel,
  onRetryFeishu,
}: FeishuErpBindModalProps) {
  const { t } = useTranslation();
  const [mobile, setMobile] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [ticketExpired, setTicketExpired] = useState(false);
  const [boundSuccess, setBoundSuccess] = useState(false);
  const submittingRef = useRef(false);

  const ticketKey = challenge?.bind_ticket ?? "";
  useEffect(() => {
    submittingRef.current = false;
    setMobile("");
    setPassword("");
    setSubmitting(false);
    setErrorText(null);
    setTicketExpired(false);
    setBoundSuccess(false);
  }, [ticketKey]);

  const handleSubmit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (
      !challenge ||
      submittingRef.current ||
      submitting ||
      boundSuccess ||
      !mobile.trim() ||
      !password
    ) {
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setErrorText(null);
    setTicketExpired(false);
    try {
      const res = await authApi.staffLogin(
        mobile.trim(),
        password,
        challenge.bind_ticket,
      );
      setBoundSuccess(true);
      await onSuccess(res);
    } catch (err) {
      const msg = apiErrorMessage(
        err,
        t("login.feishuErpBind.bindFailedFallback"),
        t,
      );
      const parsed = parseApiError(err);
      const expired =
        parsed?.code === "OIDC_BAD_REQUEST" ||
        /过期|expired|已使用|重新点击飞书/i.test(msg);
      setErrorText(msg);
      setTicketExpired(expired);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const displayName =
    challenge?.display_name?.trim() ||
    t("login.feishuErpBind.defaultFeishuUser");
  const hasMaskedPhone = Boolean(challenge?.mobile_masked?.trim());
  const reasonKey = challenge?.reason || "erp_not_found";
  const alertType =
    reasonKey === "feishu_no_mobile" || reasonKey === "erp_not_found"
      ? "info"
      : "warning";

  return (
    <Modal
      open={Boolean(challenge)}
      maskClosable={false}
      keyboard={false}
      closable={false}
      footer={null}
      centered
      width={480}
      destroyOnHidden
    >
      {challenge ? (
        <div className={styles.modalBody} data-testid="feishu-erp-bind-modal">
          <div className={styles.header}>
            <div className={styles.brandBridge} aria-hidden="true">
              <span className={styles.brandBadge}>
                <img src={feishuIcon} alt="" width={22} height={22} />
              </span>
              <ArrowLeftRight size={16} className={styles.bridgeArrow} />
              <span className={styles.brandBadge}>
                <ShieldCheck size={22} strokeWidth={2} />
              </span>
            </div>
            <h3 className={styles.title}>{t("login.feishuErpBind.title")}</h3>
            <p className={styles.subtitle}>
              {t("login.feishuErpBind.subtitle")}
            </p>
          </div>

          <div className={styles.feishuPill}>
            <div className={styles.feishuUser}>
              <img src={feishuIcon} alt="" width={20} height={20} />
              <div className={styles.feishuUserMeta}>
                <span className={styles.feishuLabel}>
                  {t("login.feishuErpBind.feishuVerifiedBadge")}
                </span>
                <span className={styles.feishuName}>{displayName}</span>
              </div>
            </div>
            <span
              className={`${styles.mobileTag} ${
                hasMaskedPhone ? "" : styles.mobileTagMissing
              }`}
            >
              {hasMaskedPhone
                ? t("login.feishuErpBind.mobileTag", {
                    mobile: challenge.mobile_masked,
                  })
                : t("login.feishuErpBind.noMobileTag")}
            </span>
          </div>

          <div className={styles.steps}>
            <div className={`${styles.stepItem} ${styles.stepDone}`}>
              <CheckCircle2 size={14} aria-hidden="true" />
              <span>{t("login.feishuErpBind.step1")}</span>
            </div>
            <div
              className={`${styles.stepItem} ${
                boundSuccess ? styles.stepDone : styles.stepActive
              }`}
            >
              {boundSuccess ? (
                <CheckCircle2 size={14} aria-hidden="true" />
              ) : null}
              <span>{t("login.feishuErpBind.step2")}</span>
            </div>
            <div
              className={`${styles.stepItem} ${
                boundSuccess ? styles.stepDone : ""
              }`}
            >
              {boundSuccess ? (
                <CheckCircle2 size={14} aria-hidden="true" />
              ) : null}
              <span>{t("login.feishuErpBind.step3")}</span>
            </div>
          </div>

          {!boundSuccess ? (
            <Alert
              type={alertType}
              showIcon
              message={t(`login.feishuErpBind.reason.${reasonKey}`, {
                defaultValue: t("login.feishuErpBind.reason.erp_not_found"),
              })}
            />
          ) : null}

          <form
            className={styles.form}
            onSubmit={(event) => void handleSubmit(event)}
          >
            <div className={styles.fieldGroup}>
              <label
                htmlFor="feishu-erp-bind-mobile"
                className={styles.fieldLabel}
              >
                {t("login.feishuErpBind.mobileLabel")}
              </label>
              <Input
                id="feishu-erp-bind-mobile"
                prefix={
                  <Phone
                    size={16}
                    style={{ color: "var(--fn-text-quaternary)" }}
                  />
                }
                size="large"
                inputMode="tel"
                autoComplete="username"
                autoFocus
                className={styles.input}
                placeholder={t("login.feishuErpBind.mobilePlaceholder")}
                value={mobile}
                onChange={(e) => setMobile(e.target.value)}
                disabled={submitting || boundSuccess}
              />
            </div>

            <div className={styles.fieldGroup}>
              <label
                htmlFor="feishu-erp-bind-password"
                className={styles.fieldLabel}
              >
                {t("login.feishuErpBind.passwordLabel")}
              </label>
              <Input.Password
                id="feishu-erp-bind-password"
                prefix={
                  <Lock
                    size={16}
                    style={{ color: "var(--fn-text-quaternary)" }}
                  />
                }
                size="large"
                autoComplete="current-password"
                className={styles.input}
                placeholder={t("login.feishuErpBind.passwordPlaceholder")}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting || boundSuccess}
              />
            </div>

            {errorText ? (
              <Alert
                type="error"
                showIcon
                message={errorText}
                action={
                  ticketExpired ? (
                    <Button
                      size="small"
                      type="link"
                      icon={<RefreshCw size={13} />}
                      onClick={onRetryFeishu}
                    >
                      {t("login.feishuErpBind.retryFeishuButton")}
                    </Button>
                  ) : undefined
                }
              />
            ) : null}

            {boundSuccess ? (
              <Alert
                type="success"
                showIcon
                message={t("login.feishuErpBind.bindSuccessBanner")}
              />
            ) : null}

            <Button
              type="primary"
              htmlType="submit"
              size="large"
              block
              loading={submitting}
              disabled={
                !mobile.trim() || !password || submitting || boundSuccess
              }
              className={styles.submitBtn}
            >
              {submitting
                ? t("login.feishuErpBind.submittingButton")
                : t("login.feishuErpBind.submitButton")}
            </Button>
          </form>

          <div className={styles.helpBox}>
            <div className={styles.helpTitle}>
              <HelpCircle size={14} aria-hidden="true" />
              <span>{t("login.feishuErpBind.helpTitle")}</span>
            </div>
            <p className={styles.helpDesc}>{t("login.feishuErpBind.helpDesc")}</p>
          </div>

          <div className={styles.footerActions}>
            <Button
              type="text"
              disabled={submitting || boundSuccess}
              onClick={onCancel}
            >
              {t("login.feishuErpBind.cancelButton")}
            </Button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
