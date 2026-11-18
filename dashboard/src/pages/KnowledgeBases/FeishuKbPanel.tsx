import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Select,
  Space,
  Switch,
  Typography,
  message,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  feishuKbApi,
  type FeishuIdentity,
  type FeishuSource,
} from "../../api/modules/feishuKb";
import { getAuthToken } from "../../api/request";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";

type Pending = {
  popup: Window;
  session: string;
  timer: number;
  completing: boolean;
  state: string;
};

function SourceRow({
  source,
  busy,
  scheduler,
  run,
}: {
  source: FeishuSource;
  busy: boolean;
  scheduler: boolean;
  run: (action: () => Promise<unknown>) => void;
}) {
  const { t } = useTranslation();
  const timezone = useServerTimezone();
  const [interval, setInterval] = useState(source.interval_seconds / 60);
  useEffect(
    () => setInterval(source.interval_seconds / 60),
    [source.interval_seconds],
  );
  const valid =
    Number.isInteger(interval) && interval >= 15 && interval <= 43200;
  return (
    <List.Item>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Text strong>{source.document_id}</Typography.Text>
        <Typography.Text>
          {t("feishuKb.providerId", { id: source.provider_id })}
        </Typography.Text>
        <Typography.Text>
          {t("feishuKb.lastStatus", { status: source.status })}
        </Typography.Text>
        <Typography.Text>
          {t("feishuKb.nextRun", {
            time: !scheduler
              ? t("feishuKb.schedulerDisabled")
              : !source.enabled
              ? "—"
              : !source.next_sync || source.next_sync * 1000 <= Date.now()
              ? t("feishuKb.dueNow")
              : formatServerDateTime(source.next_sync, timezone),
          })}
        </Typography.Text>
        <Space wrap>
          <label>
            {t("feishuKb.interval")}{" "}
            <InputNumber
              aria-label={t("feishuKb.interval")}
              min={15}
              max={43200}
              value={interval}
              onChange={(v) => setInterval(v ?? 0)}
            />
          </label>
          <Button
            disabled={!valid || busy}
            onClick={() =>
              run(() =>
                feishuKbApi.configure(
                  source.source_id,
                  !!source.enabled,
                  interval * 60,
                ),
              )
            }
          >
            {t("common.save")}
          </Button>
          <Switch
            aria-label={t("feishuKb.enabled")}
            checked={!!source.enabled}
            disabled={busy || !valid}
            onChange={(enabled) =>
              run(() =>
                feishuKbApi.configure(source.source_id, enabled, interval * 60),
              )
            }
          />{" "}
          {t("feishuKb.enabled")}
          <Button
            disabled={!source.enabled || busy}
            onClick={() => run(() => feishuKbApi.sync(source.source_id))}
          >
            {t("feishuKb.sync")}
          </Button>
          <Popconfirm
            title={t("feishuKb.deleteConfirm")}
            onConfirm={() => run(() => feishuKbApi.remove(source.source_id))}
          >
            <Button danger disabled={busy}>
              {t("feishuKb.delete")}
            </Button>
          </Popconfirm>
        </Space>
        <Space wrap>
          <Switch
            aria-label={t("feishuKb.retrieval")}
            checked={!!source.retrieval_enabled}
            disabled={busy || !source.enabled || !valid}
            onChange={(enabled) =>
              run(() =>
                feishuKbApi.configure(
                  source.source_id,
                  !!source.enabled,
                  interval * 60,
                  enabled,
                ),
              )
            }
          />
          {t("feishuKb.retrieval")}
        </Space>
        <Typography.Text type="secondary">
          {t("feishuKb.retrievalHint")}
        </Typography.Text>
      </Space>
    </List.Item>
  );
}

export default function FeishuKbPanel() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [identities, setIdentities] = useState<FeishuIdentity[]>([]);
  const [sources, setSources] = useState<FeishuSource[]>([]);
  const [scheduler, setScheduler] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [sessionChanged, setSessionChanged] = useState(false);
  const [provider, setProvider] = useState<number>();
  const [document, setDocument] = useState("");
  const pending = useRef<Pending | null>(null);
  const mounted = useRef(true);
  const displayedSession = useRef(getAuthToken());
  const refresh = useCallback(async () => {
    const session = getAuthToken();
    try {
      const [status, list] = await Promise.all([
        feishuKbApi.status(),
        feishuKbApi.sources(),
      ]);
      if (!mounted.current || session !== getAuthToken()) return;
      setIdentities(status.identities);
      setScheduler(status.scheduler_configured);
      setSources(list);
      setLoaded(true);
    } catch (error) {
      if (mounted.current && session === getAuthToken()) {
        setIdentities([]);
        setSources([]);
        setScheduler(false);
        setLoaded(false);
      }
      throw error;
    }
  }, []);
  const fail = useCallback(() => message.error(t("feishuKb.failed")), [t]);
  const cancel = useCallback(
    (current: Pending, report: boolean) => {
      window.clearInterval(current.timer);
      current.popup.close();
      if (pending.current !== current) return;
      pending.current = null;
      if (mounted.current) {
        setBusy(false);
        setFinalizing(false);
        if (report) fail();
      }
    },
    [fail],
  );
  const reconcileSession = useCallback(() => {
    const session = getAuthToken();
    if (session === displayedSession.current) return false;
    displayedSession.current = session;
    const current = pending.current;
    if (current) cancel(current, false);
    setIdentities([]);
    setSources([]);
    setProvider(undefined);
    setDocument("");
    setLoaded(false);
    if (current?.completing) setSessionChanged(true);
    void refresh().catch(fail);
    return true;
  }, [cancel, fail, refresh]);
  useEffect(() => {
    mounted.current = true;
    const onMessage = (event: MessageEvent) => {
      const current = pending.current;
      if (
        !current ||
        current.completing ||
        event.origin !== window.location.origin ||
        event.source !== current.popup ||
        event.data?.type !== "octop-feishu-kb-callback"
      )
        return;
      const { state, code } = event.data;
      if (
        typeof state !== "string" ||
        !state ||
        state !== current.state ||
        typeof code !== "string" ||
        !code ||
        current.session !== getAuthToken()
      ) {
        if (!reconcileSession()) cancel(current, true);
        return;
      }
      current.completing = true;
      setFinalizing(true);
      void feishuKbApi
        .callback(current.session, state, code)
        .then(async () => {
          if (pending.current !== current) return;
          if (current.session !== getAuthToken()) {
            reconcileSession();
            return;
          }
          cancel(current, false);
          await refresh();
        })
        .catch(() => {
          if (!reconcileSession()) cancel(current, true);
        });
    };
    window.addEventListener("message", onMessage);
    return () => {
      mounted.current = false;
      window.removeEventListener("message", onMessage);
      if (pending.current) cancel(pending.current, false);
    };
  }, [cancel, refresh, reconcileSession]);
  useEffect(() => {
    if (!open) return;
    if (!reconcileSession()) void refresh().catch(fail);
    const timer = window.setInterval(reconcileSession, 500);
    return () => window.clearInterval(timer);
  }, [open, refresh, fail, reconcileSession]);
  const start = async (id: number) => {
    if (pending.current || busy) return;
    const session = getAuthToken();
    const popup = window.open("", "_blank", "popup,width=600,height=720");
    if (!popup || !session) {
      popup?.close();
      fail();
      return;
    }
    const current: Pending = {
      popup,
      session,
      timer: 0,
      completing: false,
      state: "",
    };
    pending.current = current;
    setBusy(true);
    current.timer = window.setInterval(() => {
      if (session !== getAuthToken()) reconcileSession();
      else if (!current.completing && popup.closed) cancel(current, true);
    }, 500);
    try {
      const { url } = await feishuKbApi.authorize(id);
      if (pending.current !== current) return;
      const parsed = new URL(url);
      const redirect = new URL(parsed.searchParams.get("redirect_uri") || "");
      const state = parsed.searchParams.get("state") || "";
      if (
        parsed.origin !== "https://accounts.feishu.cn" ||
        parsed.pathname !== "/open-apis/authen/v1/authorize" ||
        redirect.origin !== window.location.origin ||
        redirect.pathname !== "/feishu-kb/callback" ||
        redirect.search ||
        redirect.hash ||
        !state ||
        session !== getAuthToken()
      )
        throw new Error("invalid authorization");
      current.state = state;
      popup.location.href = parsed.href;
    } catch {
      cancel(current, true);
    }
  };
  const run = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    try {
      await action();
    } catch {
      fail();
    } finally {
      await refresh().catch(fail);
      if (mounted.current) setBusy(false);
    }
  };
  const available = identities.filter((i) => i.consent === "authorized");
  return (
    <>
      <Button onClick={() => setOpen(true)}>{t("feishuKb.title")}</Button>
      <Drawer
        title={t("feishuKb.title")}
        open={open}
        width={640}
        closable={!finalizing}
        maskClosable={!finalizing}
        keyboard={!finalizing}
        onClose={() => {
          if (pending.current?.completing) return;
          if (pending.current) cancel(pending.current, false);
          setOpen(false);
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {finalizing && (
            <Alert type="info" message={t("feishuKb.finalizing")} />
          )}
          {sessionChanged && (
            <Alert type="warning" message={t("feishuKb.sessionChanged")} />
          )}
          <Alert
            type="warning"
            showIcon
            message={t("feishuKb.staging")}
            description={t("feishuKb.scopeHint")}
          />
          {loaded && (
            <Alert
              type={scheduler ? "warning" : "info"}
              message={t(
                scheduler ? "feishuKb.schedulerOn" : "feishuKb.schedulerOff",
              )}
              description={t("feishuKb.schedulerHint")}
            />
          )}
          {loaded && identities.length === 0 && (
            <Typography.Text>{t("feishuKb.noIdentity")}</Typography.Text>
          )}
          {identities.map((item) => (
            <div key={item.provider_id}>
              <Typography.Title level={5}>
                {item.provider_name} ({item.provider_id})
              </Typography.Title>
              <div>
                {t("feishuKb.identity", {
                  identity: item.identity,
                  tenant: item.tenant,
                })}
              </div>
              <div>
                {t("feishuKb.scopes", {
                  scopes: item.requested_scopes.join(", "),
                })}
              </div>
              <div>
                {t(
                  item.consent === "authorized"
                    ? "feishuKb.authorized"
                    : "feishuKb.consentRequired",
                )}
              </div>
              <Space wrap>
                <Button
                  disabled={busy}
                  onClick={() => void start(item.provider_id)}
                >
                  {t("feishuKb.authorize")}
                </Button>
                {item.consent === "authorized" && (
                  <Popconfirm
                    title={t("feishuKb.revokeConfirm")}
                    onConfirm={() =>
                      void run(() => feishuKbApi.revoke(item.provider_id))
                    }
                  >
                    <Button danger disabled={busy}>
                      {t("feishuKb.revoke")}
                    </Button>
                  </Popconfirm>
                )}
              </Space>
            </div>
          ))}
          <Typography.Title level={5}>
            {t("feishuKb.addSource")}
          </Typography.Title>
          <Space wrap>
            <Select
              aria-label={t("feishuKb.provider")}
              style={{ minWidth: 160 }}
              value={provider}
              placeholder={t("feishuKb.provider")}
              options={available.map((i) => ({
                value: i.provider_id,
                label: `${i.provider_name} (${i.provider_id})`,
              }))}
              onChange={setProvider}
            />
            <Input
              aria-label={t("feishuKb.documentId")}
              placeholder={t("feishuKb.documentId")}
              value={document}
              onChange={(e) => setDocument(e.target.value)}
            />
            <Button
              disabled={
                busy ||
                !available.some((i) => i.provider_id === provider) ||
                !/^[A-Za-z0-9_-]{1,200}$/.test(document)
              }
              onClick={() => {
                if (provider !== undefined)
                  void run(async () => {
                    await feishuKbApi.create(provider, document);
                    setDocument("");
                  });
              }}
            >
              {t("feishuKb.add")}
            </Button>
          </Space>
          <List
            dataSource={sources}
            renderItem={(source) => (
              <SourceRow
                key={source.source_id}
                source={source}
                busy={busy}
                scheduler={scheduler}
                run={(action) => void run(action)}
              />
            )}
          />
        </Space>
      </Drawer>
    </>
  );
}
