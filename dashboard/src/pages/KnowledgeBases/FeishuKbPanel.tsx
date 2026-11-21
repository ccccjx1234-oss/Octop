import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
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
import { parseApiError } from "../../utils/apiError";
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
  onViewContent,
}: {
  source: FeishuSource;
  busy: boolean;
  scheduler: boolean;
  run: <T>(
    action: () => Promise<T>,
    onSuccess?: (result: T) => string | void,
  ) => void;
  onViewContent: (source: FeishuSource) => void;
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
        {source.has_staged_content !== undefined && (
          <Typography.Text type="secondary">
            {t(
              source.has_staged_content
                ? "feishuKb.staged"
                : "feishuKb.notStaged",
            )}
          </Typography.Text>
        )}
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
              run(
                () =>
                  feishuKbApi.configure(
                    source.source_id,
                    !!source.enabled,
                    interval * 60,
                  ),
                () => t("feishuKb.saved", "配置已保存。"),
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
              run(
                () =>
                  feishuKbApi.configure(
                    source.source_id,
                    enabled,
                    interval * 60,
                  ),
                () => t("feishuKb.saved", "配置已保存。"),
              )
            }
          />{" "}
          {t("feishuKb.enabled")}
          <Button
            disabled={!source.enabled || busy}
            onClick={() =>
              run(
                () => feishuKbApi.sync(source.source_id),
                (res) =>
                  res?.status === "updated"
                    ? t(
                        "feishuKb.syncUpdated",
                        "同步完成：已抓取最新文档内容（updated）。",
                      )
                    : t(
                        "feishuKb.syncUnchanged",
                        "同步完成：文档内容暂无变化（unchanged）。",
                      ),
              )
            }
          >
            {t("feishuKb.sync")}
          </Button>
          <Button
            disabled={!source.enabled || busy}
            onClick={() => onViewContent(source)}
          >
            {t("feishuKb.viewContent", "查看正文")}
          </Button>
          <Popconfirm
            title={t("feishuKb.deleteConfirm")}
            onConfirm={() =>
              run(
                () => feishuKbApi.remove(source.source_id),
                () => t("feishuKb.deleted", "已删除来源。"),
              )
            }
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
              run(
                () =>
                  feishuKbApi.configure(
                    source.source_id,
                    !!source.enabled,
                    interval * 60,
                    enabled,
                  ),
                () => t("feishuKb.saved", "配置已保存。"),
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

export default function FeishuKbPanel({ active }: { active: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [identities, setIdentities] = useState<FeishuIdentity[]>([]);
  const [sources, setSources] = useState<FeishuSource[]>([]);
  const [scheduler, setScheduler] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [sessionChanged, setSessionChanged] = useState(false);
  const [provider, setProvider] = useState<number>();
  const [document, setDocument] = useState("");
  const [preview, setPreview] = useState<{
    title: string;
    content: string;
  } | null>(null);
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
      setLoadFailed(false);
    } catch (error) {
      if (mounted.current && session === getAuthToken()) {
        setIdentities([]);
        setSources([]);
        setScheduler(false);
        setLoaded(false);
        setLoadFailed(true);
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
    setPreview(null);
    setLoaded(false);
    setLoadFailed(false);
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
    if (!active) return;
    if (!reconcileSession()) void refresh().catch(fail);
    const timer = window.setInterval(reconcileSession, 500);
    return () => window.clearInterval(timer);
  }, [active, refresh, fail, reconcileSession]);
  const start = async (id: number) => {
    if (pending.current || busy) return;
    const session = getAuthToken();
    const popup = window.open("", "_blank", "popup,width=600,height=720");
    if (!popup || !session) {
      popup?.close();
      message.error(t(!popup ? "feishuKb.popupBlocked" : "feishuKb.failed"));
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
        parsed.pathname !== "/oauth/v3/authorize" ||
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
    } catch (error) {
      const reason = parseApiError(error)?.message;
      message.error(
        reason === "identity_unavailable"
          ? t("feishuKb.needFeishuLogin")
          : t("feishuKb.failed"),
      );
      cancel(current, false);
    }
  };
  useEffect(() => {
    const availableIds = identities
      .filter((i) => i.consent === "authorized")
      .map((i) => i.provider_id);
    if (
      availableIds.length === 1 &&
      (provider === undefined || !availableIds.includes(provider))
    ) {
      setProvider(availableIds[0]);
    } else if (provider !== undefined && !availableIds.includes(provider)) {
      setProvider(undefined);
    }
  }, [identities, provider]);
  const run = async <T,>(
    action: () => Promise<T>,
    onSuccess?: (result: T) => string | void,
  ) => {
    if (busy) return;
    setBusy(true);
    try {
      const result = await action();
      const feedback = onSuccess?.(result);
      if (feedback && mounted.current) message.success(feedback);
    } catch {
      fail();
    } finally {
      await refresh().catch(fail);
      if (mounted.current) setBusy(false);
    }
  };
  const viewContent = (source: FeishuSource) => {
    const session = getAuthToken();
    void run(async () => {
      const res = await feishuKbApi.content(source.source_id);
      if (mounted.current && session === getAuthToken()) {
        setPreview({
          title: t("feishuKb.contentTitle", { id: source.document_id }),
          content: res?.content || "",
        });
      }
    });
  };
  const available = identities.filter((i) => i.consent === "authorized");
  const stagedCount = sources.every(
    (source) => source.has_staged_content !== undefined,
  )
    ? sources.filter((source) => source.has_staged_content).length
    : null;
  return (
    <>
      <List
        header={
          <Space wrap>
            {loaded && (
              <>
                <Typography.Text strong>
                  {t("feishuKb.sourceCount", { total: sources.length })}
                </Typography.Text>
                {stagedCount !== null && (
                  <Typography.Text>
                    {t("feishuKb.stagedCount", { total: stagedCount })}
                  </Typography.Text>
                )}
              </>
            )}
            <Button onClick={() => setOpen(true)}>
              {t("feishuKb.manageSources")}
            </Button>
          </Space>
        }
        dataSource={sources}
        locale={{
          emptyText: loadFailed ? (
            <Alert
              type="error"
              showIcon
              message={t("feishuKb.failed")}
              action={
                <Button size="small" onClick={() => void refresh().catch(fail)}>
                  {t("common.retry")}
                </Button>
              }
            />
          ) : loaded ? (
            t("feishuKb.noSources")
          ) : (
            <Spin />
          ),
        }}
        renderItem={(source) => (
          <SourceRow
            key={source.source_id}
            source={source}
            busy={busy}
            scheduler={scheduler}
            run={(action, onSuccess) => void run(action, onSuccess)}
            onViewContent={viewContent}
          />
        )}
      />
      <Drawer
        title={t("feishuKb.manageSources")}
        open={open}
        width={640}
        closable={!finalizing}
        maskClosable={!finalizing}
        keyboard={!finalizing}
        onClose={() => {
          if (pending.current?.completing) return;
          if (pending.current) cancel(pending.current, false);
          setPreview(null);
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
          {identities.some((item) => item.consent !== "authorized") && (
            <Alert
              type="info"
              showIcon
              message={t("feishuKb.guideTitle")}
              description={t("feishuKb.guideBody")}
            />
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
                      void run(
                        () => feishuKbApi.revoke(item.provider_id),
                        () => t("feishuKb.revoked", "已撤销授权。"),
                      )
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
                  void run(
                    async () => {
                      await feishuKbApi.create(provider, document);
                      setDocument("");
                    },
                    () =>
                      t(
                        "feishuKb.added",
                        "已添加来源（默认禁用，请开启「启用」后再同步）。",
                      ),
                  );
              }}
            >
              {t("feishuKb.add")}
            </Button>
          </Space>
        </Space>
      </Drawer>
      <Modal
        title={preview?.title}
        open={preview !== null}
        footer={null}
        onCancel={() => setPreview(null)}
        destroyOnHidden
      >
        <Typography.Paragraph
          style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}
        >
          {preview?.content || t("feishuKb.contentEmpty", "文档正文为空。")}
        </Typography.Paragraph>
      </Modal>
    </>
  );
}
