import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Select,
  Space,
  Steps,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { AlertCircle, Clock, Plus, SlidersHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  sharedFeishuApi,
  type DirectoryEntry,
  type ScopeKind,
  type SharedFeishuConnection,
  type SharedFeishuReview,
  type SharedFeishuScope,
  type SharedFeishuSource,
  type SyncSchedule,
} from "../../api/modules/sharedFeishu";
import { getAuthToken } from "../../api/request";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";

type Folder = { id: string; title: string };
export type FeishuSourceView = "content" | "sources" | "connect";
type Step = "sources" | "accounts" | "directory" | "review";
type Pending = {
  popup: Window;
  session: string;
  state: string;
  completing: boolean;
};
const scopes: ScopeKind[] = ["wiki_space", "wiki_node", "drive_folder"];
const INTERVAL_PRESETS = [5, 15, 30, 60] as const;
const statusTagColor: Record<SharedFeishuSource["status"], string> = {
  queued: "processing",
  syncing: "processing",
  synced: "success",
  paused: "default",
  error: "error",
  disconnected: "default",
};
const inProgress = (source: SharedFeishuSource) =>
  source.status === "queued" || source.status === "syncing";

export default function SharedFeishuSources({
  kbId,
  usable,
  view,
  onNavigate,
  onSourceCount,
  onPendingCount,
  onDocumentsChanged,
}: {
  kbId: string;
  usable: boolean;
  view: FeishuSourceView;
  onNavigate: (view: FeishuSourceView) => void;
  onSourceCount: (count: number) => void;
  onPendingCount?: (count: number) => void;
  onDocumentsChanged: () => void;
}) {
  const { t } = useTranslation();
  const zone = useServerTimezone();
  const [wizardStep, setWizardStep] =
    useState<Exclude<Step, "sources">>("accounts");
  const step = view === "sources" ? "sources" : wizardStep;
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [sources, setSources] = useState<SharedFeishuSource[]>([]);
  const [directory, setDirectory] = useState<DirectoryEntry[]>([]);
  const [page, setPage] = useState<string | null>(null);
  const [connections, setConnections] = useState<SharedFeishuConnection[]>([]);
  const [providers, setProviders] = useState<{ id: number; name: string }[]>(
    [],
  );
  const [connection, setConnection] = useState<string>();
  const [provider, setProvider] = useState<number>();
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState<ScopeKind>("wiki_space");
  const [space, setSpace] = useState<Folder>();
  const [path, setPath] = useState<Folder[]>([]);
  const [target, setTarget] = useState<Folder>();
  const [includeChildren, setIncludeChildren] = useState(true);
  const [review, setReview] = useState<SharedFeishuReview>();
  const [scope, setScope] = useState<SharedFeishuScope>();
  const [schedule, setSchedule] = useState<SyncSchedule>("daily");
  const [intervalMinutes, setIntervalMinutes] = useState<number>(30);
  const [autoAdd, setAutoAdd] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const pending = useRef<Pending | null>(null);
  const sourceSignature = useRef("");
  const onChanged = useRef(onDocumentsChanged);
  onChanged.current = onDocumentsChanged;
  const onCount = useRef(onSourceCount);
  onCount.current = onSourceCount;
  const onPending = useRef(onPendingCount);
  onPending.current = onPendingCount;

  const refreshSources = useCallback(async () => {
    const session = getAuthToken();
    const rows = await sharedFeishuApi.list(kbId);
    if (session !== getAuthToken()) return;
    const signature = JSON.stringify(
      rows.map(({ id, documents, last_sync, status }) => [
        id,
        documents,
        last_sync,
        status,
      ]),
    );
    if (sourceSignature.current && signature !== sourceSignature.current)
      onChanged.current();
    sourceSignature.current = signature;
    setSources(rows);
    onCount.current(rows.length);
    onPending.current?.(
      rows.reduce((sum, row) => sum + (row.pending_count || 0), 0),
    );
  }, [kbId]);

  const refreshConnections = useCallback(async () => {
    const session = getAuthToken();
    const result = await sharedFeishuApi.connections();
    if (session !== getAuthToken()) return;
    setConnections(result.connections);
    setProviders(result.providers);
    setProvider((current) => current ?? result.providers[0]?.id);
    setConnection((current) =>
      result.connections.some((row) => row.id === current && row.connected)
        ? current
        : result.connections.find((row) => row.connected)?.id,
    );
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([refreshSources(), refreshConnections()]);
  }, [refreshConnections, refreshSources]);

  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(false);
    try {
      await action();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refreshSources().catch(() => setError(true));
  }, [refreshSources]);

  useEffect(() => {
    if (view !== "connect") {
      setWizardStep("accounts");
    }
    if (view === "content") return;
    setError(false);
    setAccountsLoading(true);
    let active = true;
    void refreshConnections()
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => {
        if (active) setAccountsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [view, refreshConnections]);

  const hasWork = sources.some(inProgress);
  useEffect(() => {
    if (view !== "sources" && !hasWork) return;
    let requesting = false;
    const timer = window.setInterval(
      () => {
        if (requesting) return;
        requesting = true;
        void refreshSources()
          .catch(() => setError(true))
          .finally(() => {
            requesting = false;
          });
      },
      hasWork ? 3000 : 12000,
    );
    return () => window.clearInterval(timer);
  }, [view, hasWork, refreshSources]);

  useEffect(() => {
    const closePending = () => {
      pending.current?.popup.close();
      pending.current = null;
    };
    const listener = (event: MessageEvent) => {
      const attempt = pending.current;
      if (
        !attempt ||
        attempt.completing ||
        event.origin !== window.location.origin ||
        event.source !== attempt.popup ||
        event.data?.type !== "octop-feishu-kb-callback" ||
        event.data?.state !== attempt.state
      )
        return;
      if (
        getAuthToken() !== attempt.session ||
        typeof event.data?.code !== "string" ||
        !event.data.code
      ) {
        closePending();
        setAuthorizing(false);
        setError(true);
        return;
      }
      attempt.completing = true;
      void sharedFeishuApi
        .callback(attempt.session, attempt.state, event.data.code)
        .then(async () => {
          if (pending.current === attempt && getAuthToken() === attempt.session)
            await refreshConnections();
        })
        .catch(() => setError(true))
        .finally(() => {
          if (pending.current === attempt) {
            closePending();
            setAuthorizing(false);
          }
        });
    };
    window.addEventListener("message", listener);
    const timer = window.setInterval(() => {
      const attempt = pending.current;
      if (
        attempt &&
        !attempt.completing &&
        (attempt.popup.closed || getAuthToken() !== attempt.session)
      ) {
        closePending();
        setAuthorizing(false);
      }
    }, 1000);
    return () => {
      closePending();
      window.clearInterval(timer);
      window.removeEventListener("message", listener);
    };
  }, [refreshConnections]);

  const authorize = async () => {
    const selectedProvider = providers.find((item) => item.id === provider);
    if (!selectedProvider || pending.current) return;
    const session = getAuthToken();
    const popup = window.open("", "_blank", "popup,width=600,height=720");
    if (!popup || !session) {
      popup?.close();
      setError(true);
      return;
    }
    const attempt = { popup, session, state: "", completing: false };
    pending.current = attempt;
    setAuthorizing(true);
    setError(false);
    try {
      const { url } = await sharedFeishuApi.authorize(
        selectedProvider.id,
        label.trim() || selectedProvider.name,
      );
      const targetUrl = new URL(url);
      const redirect = new URL(
        targetUrl.searchParams.get("redirect_uri") ?? "",
      );
      const state = targetUrl.searchParams.get("state");
      if (
        pending.current !== attempt ||
        session !== getAuthToken() ||
        targetUrl.origin !== "https://accounts.feishu.cn" ||
        targetUrl.pathname !== "/oauth/v3/authorize" ||
        redirect.origin !== window.location.origin ||
        redirect.pathname !== "/feishu-kb/callback" ||
        !state
      )
        throw new Error("Invalid Feishu authorization");
      attempt.state = state;
      popup.location.href = targetUrl.href;
    } catch {
      popup.close();
      if (pending.current === attempt) {
        pending.current = null;
        setAuthorizing(false);
        setError(true);
      }
    }
  };

  const browse = async (
    nextKind: ScopeKind,
    nextSpace?: Folder,
    nextPath: Folder[] = [],
    append = false,
  ) => {
    if (!connection) return;
    const params: Record<string, string> = {
      kind:
        nextKind === "drive_folder"
          ? "drive_folders"
          : nextSpace
          ? "wiki_nodes"
          : "wiki_spaces",
    };
    if (nextSpace) params.space = nextSpace.id;
    if (nextPath.length) params.parent = nextPath[nextPath.length - 1].id;
    if (append && page) params.page = page;
    setKind(nextKind);
    setSpace(nextSpace);
    setPath(nextPath);
    setWizardStep("directory");
    if (!append) {
      setTarget(undefined);
      setDirectory([]);
      setPage(null);
    }
    const result = await sharedFeishuApi.browse(connection, params);
    setDirectory((previous) =>
      append ? [...previous, ...result.items] : result.items,
    );
    setPage(result.next_page);
  };

  const reviewScope = async (
    candidate: SharedFeishuScope,
    nextSchedule: SyncSchedule,
    includeNew: boolean,
    nextIntervalMinutes = 30,
  ) => {
    const result = await sharedFeishuApi.preview(kbId, candidate);
    setScope(candidate);
    setReview(result);
    setSchedule(nextSchedule);
    setIntervalMinutes(nextIntervalMinutes >= 1 ? nextIntervalMinutes : 30);
    setAutoAdd(includeNew);
    setConfirmed(false);
    setWizardStep("review");
  };

  const reviewTarget = async () => {
    if (!connection || !target) return;
    await reviewScope(
      {
        connection_id: connection,
        kind,
        root: target.id,
        title: target.title,
        space: space?.id ?? null,
        include_children: includeChildren,
      },
      "daily",
      false,
      30,
    );
  };

  const afterAction = async () => {
    await refreshSources();
    onChanged.current();
  };

  const validInterval =
    schedule !== "interval" ||
    (Number.isInteger(intervalMinutes) &&
      intervalMinutes >= 1 &&
      intervalMinutes <= 43200);

  const scheduleText = (source: SharedFeishuSource) =>
    source.schedule === "interval" && source.interval_minutes
      ? t("sharedFeishu.everyMinutes", { minutes: source.interval_minutes })
      : t(`sharedFeishu.${source.schedule}`);

  if (view === "content") return null;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && (
        <Alert type="error" showIcon message={t("sharedFeishu.failed")} />
      )}
      {!usable && (
        <Alert
          type="warning"
          showIcon
          message={t("sharedFeishu.modelRequired")}
        />
      )}
      {step !== "sources" && (
        <Steps
          size="small"
          current={step === "accounts" ? 0 : step === "directory" ? 1 : 2}
          items={[
            { title: t("sharedFeishu.accounts") },
            { title: t("sharedFeishu.directory") },
            { title: t("sharedFeishu.review") },
          ]}
        />
      )}
      {step === "sources" && (
        <>
          <Button
            type="primary"
            icon={<Plus size={14} />}
            disabled={busy}
            onClick={() => onNavigate("connect")}
          >
            {t("sharedFeishu.addScope")}
          </Button>
          <List
            dataSource={sources}
            loading={busy}
            locale={{ emptyText: t("sharedFeishu.noSources") }}
            renderItem={(source) => {
              const hasPending = source.pending_count > 0;
              const busySync = inProgress(source);
              const disconnected = !connections.some(
                (row) => row.id === source.connection_id && row.connected,
              );
              const reviewButtonDisabled =
                !usable || busy || busySync || disconnected;
              const reviewButton = (
                <Button
                  type={hasPending ? "primary" : "default"}
                  className={
                    hasPending ? styles.feishuPendingButton : undefined
                  }
                  icon={
                    hasPending ? (
                      <AlertCircle size={14} />
                    ) : (
                      <SlidersHorizontal size={14} />
                    )
                  }
                  disabled={reviewButtonDisabled}
                  onClick={() =>
                    void run(() => {
                      setTarget(undefined);
                      return reviewScope(
                        {
                          connection_id: source.connection_id,
                          kind: source.scope_kind,
                          root: source.root_token,
                          space: source.space_id || null,
                          title: source.title,
                          include_children: Boolean(source.include_children),
                        },
                        source.schedule,
                        Boolean(source.auto_add),
                        source.interval_minutes ?? 30,
                      ).then(() => onNavigate("connect"));
                    })
                  }
                >
                  {hasPending
                    ? t("sharedFeishu.pendingAction", {
                        count: source.pending_count,
                      })
                    : t("sharedFeishu.reviewChanges")}
                </Button>
              );

              return (
                <List.Item
                  key={source.id}
                  className={
                    hasPending
                      ? `${styles.feishuSourceCard} ${styles.feishuSourceCardPending}`
                      : styles.feishuSourceCard
                  }
                >
                  <Space
                    direction="vertical"
                    size="small"
                    style={{ width: "100%" }}
                  >
                    <Typography.Text strong>{source.title}</Typography.Text>
                    <Space wrap>
                      <Tag>{source.connection_label}</Tag>
                      <Tag color={statusTagColor[source.status]}>
                        {t(`sharedFeishu.status.${source.status}`)}
                      </Tag>
                      <Tag
                        icon={<Clock size={12} />}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        {scheduleText(source)}
                      </Tag>
                      <Tag color={source.auto_add ? "green" : "default"}>
                        {t(
                          source.auto_add
                            ? "sharedFeishu.autoAddOn"
                            : "sharedFeishu.autoAddOff",
                        )}
                      </Tag>
                    </Space>
                    <Typography.Text type="secondary">
                      {t("sharedFeishu.sourceCount", {
                        documents: source.documents,
                        pending: source.pending_count,
                        skipped: source.skipped_count,
                      })}
                    </Typography.Text>
                    {hasPending && (
                      <div className={styles.feishuPendingCallout}>
                        <AlertCircle
                          size={15}
                          style={{ flexShrink: 0, marginTop: 2 }}
                        />
                        <div className={styles.feishuPendingCalloutText}>
                          <div>
                            {t("sharedFeishu.pendingBanner", {
                              count: source.pending_count,
                            })}
                          </div>
                          {busySync && (
                            <div className={styles.feishuPendingBusyHint}>
                              {t("sharedFeishu.pendingBusyHint")}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                    {source.last_sync && (
                      <Typography.Text type="secondary">
                        {t("sharedFeishu.lastSync", {
                          date: formatServerDateTime(source.last_sync, zone),
                        })}
                      </Typography.Text>
                    )}
                    {source.last_error && (
                      <Typography.Text type="danger">
                        {t("sharedFeishu.syncFailed", {
                          reason: source.last_error,
                        })}
                      </Typography.Text>
                    )}
                    <Space wrap>
                      {hasPending &&
                        (busySync ? (
                          <Tooltip title={t("sharedFeishu.pendingBusyHint")}>
                            <span style={{ display: "inline-flex" }}>
                              {reviewButton}
                            </span>
                          </Tooltip>
                        ) : (
                          reviewButton
                        ))}
                      <Button
                        disabled={
                          !usable ||
                          busy ||
                          busySync ||
                          source.status === "disconnected"
                        }
                        onClick={() =>
                          void run(async () => {
                            await sharedFeishuApi.setActive(
                              kbId,
                              source.id,
                              true,
                            );
                            await afterAction();
                          })
                        }
                      >
                        {t(
                          source.active
                            ? "sharedFeishu.sync"
                            : "sharedFeishu.resume",
                        )}
                      </Button>
                      <Button
                        disabled={!source.active || busy}
                        onClick={() =>
                          void run(async () => {
                            await sharedFeishuApi.setActive(
                              kbId,
                              source.id,
                              false,
                            );
                            await afterAction();
                          })
                        }
                      >
                        {t("sharedFeishu.pause")}
                      </Button>
                      {!hasPending && reviewButton}
                    </Space>
                  </Space>
                </List.Item>
              );
            }}
          />
          {connections.length > 0 && (
            <Button type="link" onClick={() => onNavigate("connect")}>
              {t("sharedFeishu.accounts")}
            </Button>
          )}
        </>
      )}
      {step === "accounts" && (
        <>
          {!accountsLoading && connections.length === 0 && (
            <Alert
              type="info"
              showIcon
              message={t("sharedFeishu.guideTitle")}
              description={
                <span style={{ whiteSpace: "pre-line" }}>
                  {t("sharedFeishu.guideBody")}
                </span>
              }
            />
          )}
          <List
            dataSource={connections}
            loading={accountsLoading}
            locale={{ emptyText: t("sharedFeishu.noAccounts") }}
            renderItem={(row) => (
              <List.Item key={row.id}>
                <Space wrap>
                  <Button
                    type={connection === row.id ? "primary" : "default"}
                    disabled={!row.connected || authorizing}
                    onClick={() => setConnection(row.id)}
                  >
                    {row.label}
                  </Button>
                  <Tag>{row.tenant}</Tag>
                  {row.connected ? (
                    <Popconfirm
                      title={t("sharedFeishu.disconnectConfirm")}
                      onConfirm={() =>
                        void run(async () => {
                          await sharedFeishuApi.disconnect(row.id);
                          await refresh();
                          onChanged.current();
                        })
                      }
                    >
                      <Button danger type="link" disabled={busy || authorizing}>
                        {t("sharedFeishu.disconnect")}
                      </Button>
                    </Popconfirm>
                  ) : (
                    <Tag>{t("sharedFeishu.status.disconnected")}</Tag>
                  )}
                </Space>
              </List.Item>
            )}
          />
          <Typography.Text strong>
            {t("sharedFeishu.addAccount")}
          </Typography.Text>
          <Typography.Text type="secondary">
            {t("sharedFeishu.newGrantHint")}
          </Typography.Text>
          {!accountsLoading && providers.length > 0 && !connection && (
            <Typography.Text type="secondary">
              {t("sharedFeishu.directoryScopesHint")}
            </Typography.Text>
          )}
          {accountsLoading ? null : providers.length === 0 ? (
            <Alert type="info" message={t("sharedFeishu.noProviders")} />
          ) : (
            <Space wrap>
              <Select
                aria-label={t("sharedFeishu.provider")}
                style={{ minWidth: 150 }}
                value={provider}
                options={providers.map(({ id, name }) => ({
                  value: id,
                  label: name,
                }))}
                onChange={setProvider}
              />
              <Input
                aria-label={t("sharedFeishu.newGrantName")}
                placeholder={t("sharedFeishu.newGrantName")}
                maxLength={80}
                value={label}
                onChange={(event) => setLabel(event.target.value)}
              />
              <Button
                loading={authorizing}
                disabled={provider === undefined || busy}
                onClick={() => void authorize()}
              >
                {t("sharedFeishu.authorize")}
              </Button>
            </Space>
          )}
          <Space>
            <Button
              disabled={authorizing}
              onClick={() => onNavigate("content")}
            >
              {t("common.back")}
            </Button>
            <Button
              type="primary"
              disabled={!connection || authorizing}
              loading={busy}
              onClick={() => void run(() => browse("wiki_space"))}
            >
              {t("sharedFeishu.directory")}
            </Button>
          </Space>
        </>
      )}
      {step === "directory" && (
        <>
          <Select
            aria-label={t("sharedFeishu.scopeType")}
            value={kind}
            disabled={busy}
            options={scopes.map((value) => ({
              value,
              label: t(`sharedFeishu.scopes.${value}`),
            }))}
            onChange={(value) => void run(() => browse(value))}
          />
          {(space || path.length > 0) && (
            <Space wrap>
              <Button
                size="small"
                disabled={busy}
                onClick={() => void run(() => browse(kind))}
              >
                {t("sharedFeishu.root")}
              </Button>
              {space && (
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => void run(() => browse(kind, space))}
                >
                  {space.title}
                </Button>
              )}
              {path.map((folder, index) => (
                <Button
                  key={folder.id}
                  size="small"
                  disabled={busy}
                  onClick={() =>
                    void run(() =>
                      browse(kind, space, path.slice(0, index + 1)),
                    )
                  }
                >
                  {folder.title}
                </Button>
              ))}
            </Space>
          )}
          <List
            dataSource={directory}
            loading={busy}
            locale={{ emptyText: t("sharedFeishu.noDirectories") }}
            renderItem={(entry) => (
              <List.Item key={entry.id}>
                <Space wrap>
                  <Typography.Text>{entry.title}</Typography.Text>
                  {(kind !== "wiki_node" || space) && (
                    <Button
                      disabled={busy}
                      type={target?.id === entry.id ? "primary" : "default"}
                      size="small"
                      onClick={() => setTarget(entry)}
                    >
                      {t("sharedFeishu.select")}
                    </Button>
                  )}
                  {kind !== "wiki_space" && (
                    <Button
                      type="link"
                      size="small"
                      disabled={busy}
                      onClick={() =>
                        void run(() =>
                          space || kind === "drive_folder"
                            ? browse(kind, space, [...path, entry])
                            : browse(kind, entry),
                        )
                      }
                    >
                      {t("sharedFeishu.openFolder")}
                    </Button>
                  )}
                </Space>
              </List.Item>
            )}
          />
          {page && (
            <Button
              disabled={busy}
              onClick={() => void run(() => browse(kind, space, path, true))}
            >
              {t("sharedFeishu.loadMore")}
            </Button>
          )}
          <Checkbox
            checked={includeChildren}
            onChange={(event) => setIncludeChildren(event.target.checked)}
          >
            {t("sharedFeishu.includeChildren")}
          </Checkbox>
          <Space>
            <Button disabled={busy} onClick={() => setWizardStep("accounts")}>
              {t("common.back")}
            </Button>
            <Button
              type="primary"
              loading={busy}
              disabled={!target || !usable}
              onClick={() => void run(reviewTarget)}
            >
              {t("sharedFeishu.review")}
            </Button>
          </Space>
        </>
      )}
      {step === "review" && review && scope && (
        <>
          <Typography.Title level={5}>{scope.title}</Typography.Title>
          <Alert
            type="warning"
            showIcon
            message={t(
              `sharedFeishu.audience.${review.visibility.visibility}`,
              { groups: review.visibility.group_names.join(", ") },
            )}
            description={t("sharedFeishu.sharedCopy")}
          />
          <Typography.Text>
            {(review.new_count ?? 0) > 0
              ? t("sharedFeishu.reviewCountWithNew", {
                  count: review.documents.length,
                  newCount: review.new_count ?? 0,
                  skipped: review.skipped,
                })
              : t("sharedFeishu.reviewCount", {
                  count: review.documents.length,
                  skipped: review.skipped,
                })}
          </Typography.Text>
          {review.documents.length ? (
            <List
              size="small"
              dataSource={review.documents}
              style={{ maxHeight: 240, overflowY: "auto" }}
              renderItem={(doc) => (
                <List.Item
                  key={doc.id}
                  className={doc.is_new ? styles.feishuReviewRowNew : undefined}
                >
                  <Space size="small" wrap>
                    {doc.is_new ? (
                      <Tag color="warning">
                        {t("sharedFeishu.reviewNewBadge")}
                      </Tag>
                    ) : review.existing_source ? (
                      <Tag>{t("sharedFeishu.reviewImportedBadge")}</Tag>
                    ) : null}
                    <span>{[...doc.path, doc.title].join(" / ")}</span>
                  </Space>
                </List.Item>
              )}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
          <div className={styles.feishuScheduleBox}>
            <Select
              aria-label={t("sharedFeishu.schedule")}
              value={schedule}
              style={{ width: "100%" }}
              options={[
                { value: "interval", label: t("sharedFeishu.interval") },
                { value: "daily", label: t("sharedFeishu.daily") },
                { value: "manual", label: t("sharedFeishu.manual") },
              ]}
              onChange={setSchedule}
            />
            {schedule === "interval" && (
              <>
                <div className={styles.feishuIntervalRow}>
                  <span>{t("sharedFeishu.intervalPrefix")}</span>
                  <InputNumber
                    aria-label={t("sharedFeishu.intervalUnit")}
                    min={1}
                    max={43200}
                    precision={0}
                    value={intervalMinutes}
                    onChange={(value) => {
                      if (typeof value === "number" && Number.isFinite(value)) {
                        setIntervalMinutes(Math.trunc(value));
                      }
                    }}
                  />
                  <span>{t("sharedFeishu.intervalUnit")}</span>
                </div>
                <div className={styles.feishuIntervalPresets}>
                  {INTERVAL_PRESETS.map((minutes) => (
                    <Button
                      key={minutes}
                      size="small"
                      type={intervalMinutes === minutes ? "primary" : "default"}
                      onClick={() => setIntervalMinutes(minutes)}
                    >
                      {t("sharedFeishu.intervalPreset", { minutes })}
                    </Button>
                  ))}
                </div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {t("sharedFeishu.intervalRangeHint")}
                </Typography.Text>
              </>
            )}
          </div>
          <div>
            <Checkbox
              checked={autoAdd}
              onChange={(event) => setAutoAdd(event.target.checked)}
            >
              {t("sharedFeishu.autoAdd")}
            </Checkbox>
            <Typography.Text
              type="secondary"
              style={{
                display: "block",
                fontSize: 12,
                marginLeft: 24,
                marginTop: 2,
              }}
            >
              {t("sharedFeishu.autoAddHint")}
            </Typography.Text>
          </div>
          <Checkbox
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          >
            {t("sharedFeishu.confirm")}
          </Checkbox>
          <Space>
            <Button
              disabled={busy}
              onClick={() =>
                target ? setWizardStep("directory") : onNavigate("sources")
              }
            >
              {t("common.back")}
            </Button>
            <Button
              type="primary"
              loading={busy}
              disabled={!confirmed || !usable || !validInterval}
              onClick={() =>
                void run(async () => {
                  await (schedule === "interval"
                    ? sharedFeishuApi.approve(
                        kbId,
                        scope,
                        review.fingerprint,
                        schedule,
                        autoAdd,
                        intervalMinutes,
                      )
                    : sharedFeishuApi.approve(
                        kbId,
                        scope,
                        review.fingerprint,
                        schedule,
                        autoAdd,
                      ));
                  onNavigate("content");
                  await afterAction();
                })
              }
            >
              {t("sharedFeishu.import")}
            </Button>
          </Space>
        </>
      )}
    </Space>
  );
}
