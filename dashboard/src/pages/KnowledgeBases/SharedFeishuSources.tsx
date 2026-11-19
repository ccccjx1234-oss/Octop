import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  List,
  Popconfirm,
  Select,
  Space,
  Steps,
  Tag,
  Typography,
} from "antd";
import { Link2, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  sharedFeishuApi,
  type DirectoryEntry,
  type ScopeKind,
  type SharedFeishuConnection,
  type SharedFeishuReview,
  type SharedFeishuScope,
  type SharedFeishuSource,
} from "../../api/modules/sharedFeishu";
import { getAuthToken } from "../../api/request";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";

type Folder = { id: string; title: string };
type Step = "sources" | "accounts" | "directory" | "review";
type Pending = {
  popup: Window;
  session: string;
  state: string;
  completing: boolean;
};
const scopes: ScopeKind[] = ["wiki_space", "wiki_node", "drive_folder"];
const inProgress = (source: SharedFeishuSource) =>
  source.status === "queued" || source.status === "syncing";

export default function SharedFeishuSources({
  kbId,
  usable,
  onDocumentsChanged,
}: {
  kbId: string;
  usable: boolean;
  onDocumentsChanged: () => void;
}) {
  const { t } = useTranslation();
  const zone = useServerTimezone();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step>("sources");
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
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
  const [schedule, setSchedule] = useState<"daily" | "manual">("daily");
  const [autoAdd, setAutoAdd] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const pending = useRef<Pending | null>(null);
  const sourceSignature = useRef("");
  const onChanged = useRef(onDocumentsChanged);
  onChanged.current = onDocumentsChanged;

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

  const hasWork = sources.some(inProgress);
  useEffect(() => {
    if (!open && !hasWork) return;
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
  }, [open, hasWork, refreshSources]);

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
    if (provider === undefined || !label.trim() || pending.current) return;
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
      const { url } = await sharedFeishuApi.authorize(provider, label.trim());
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
    setStep("directory");
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
    nextSchedule: "daily" | "manual",
    includeNew: boolean,
  ) => {
    const result = await sharedFeishuApi.preview(kbId, candidate);
    setScope(candidate);
    setReview(result);
    setSchedule(nextSchedule);
    setAutoAdd(includeNew);
    setConfirmed(false);
    setStep("review");
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
    );
  };

  const afterAction = async () => {
    await refreshSources();
    onChanged.current();
  };

  return (
    <>
      <Button
        icon={<Link2 size={14} />}
        onClick={() => {
          setOpen(true);
          setStep("sources");
          void run(refresh);
        }}
      >
        {t("sharedFeishu.connect")}
      </Button>
      <Drawer
        title={t("sharedFeishu.manage")}
        width="min(650px, 100vw)"
        open={open}
        maskClosable={!authorizing}
        keyboard={!authorizing}
        closable={!authorizing}
        onClose={() => setOpen(false)}
        destroyOnHidden
      >
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
                disabled={busy || !usable}
                onClick={() => setStep("accounts")}
              >
                {t("sharedFeishu.addScope")}
              </Button>
              <List
                dataSource={sources}
                loading={busy}
                locale={{ emptyText: t("sharedFeishu.noSources") }}
                renderItem={(source) => (
                  <List.Item key={source.id}>
                    <Space
                      direction="vertical"
                      size="small"
                      style={{ width: "100%" }}
                    >
                      <Typography.Text strong>{source.title}</Typography.Text>
                      <Space wrap>
                        <Tag>{source.connection_label}</Tag>
                        <Tag>{t(`sharedFeishu.status.${source.status}`)}</Tag>
                        <span>{t(`sharedFeishu.${source.schedule}`)}</span>
                      </Space>
                      <Typography.Text type="secondary">
                        {t("sharedFeishu.sourceCount", {
                          documents: source.documents,
                          pending: source.pending_count,
                          skipped: source.skipped_count,
                        })}
                      </Typography.Text>
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
                        <Button
                          disabled={
                            !usable ||
                            busy ||
                            inProgress(source) ||
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
                        <Button
                          disabled={
                            !usable ||
                            busy ||
                            inProgress(source) ||
                            !connections.some(
                              (row) =>
                                row.id === source.connection_id &&
                                row.connected,
                            )
                          }
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
                                  include_children: Boolean(
                                    source.include_children,
                                  ),
                                },
                                source.schedule,
                                Boolean(source.auto_add),
                              );
                            })
                          }
                        >
                          {t("sharedFeishu.reviewChanges")}
                        </Button>
                      </Space>
                    </Space>
                  </List.Item>
                )}
              />
              {connections.length > 0 && (
                <Button type="link" onClick={() => setStep("accounts")}>
                  {t("sharedFeishu.accounts")}
                </Button>
              )}
            </>
          )}
          {step === "accounts" && (
            <>
              <List
                dataSource={connections}
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
                          <Button
                            danger
                            type="link"
                            disabled={busy || authorizing}
                          >
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
              {providers.length === 0 ? (
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
                    aria-label={t("sharedFeishu.accountLabel")}
                    placeholder={t("sharedFeishu.accountLabel")}
                    maxLength={80}
                    value={label}
                    onChange={(event) => setLabel(event.target.value)}
                  />
                  <Button
                    loading={authorizing}
                    disabled={!label.trim() || provider === undefined || busy}
                    onClick={() => void authorize()}
                  >
                    {t("sharedFeishu.authorize")}
                  </Button>
                </Space>
              )}
              <Space>
                <Button
                  disabled={authorizing}
                  onClick={() => setStep("sources")}
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
                  onClick={() =>
                    void run(() => browse(kind, space, path, true))
                  }
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
                <Button disabled={busy} onClick={() => setStep("accounts")}>
                  {t("common.back")}
                </Button>
                <Button
                  type="primary"
                  loading={busy}
                  disabled={!target}
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
                {t("sharedFeishu.reviewCount", {
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
                    <List.Item key={doc.id}>
                      {[...doc.path, doc.title].join(" / ")}
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
              )}
              <Select
                aria-label={t("sharedFeishu.schedule")}
                value={schedule}
                options={[
                  { value: "daily", label: t("sharedFeishu.daily") },
                  { value: "manual", label: t("sharedFeishu.manual") },
                ]}
                onChange={setSchedule}
              />
              <Checkbox
                checked={autoAdd}
                onChange={(event) => setAutoAdd(event.target.checked)}
              >
                {t("sharedFeishu.autoAdd")}
              </Checkbox>
              <Checkbox
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              >
                {t("sharedFeishu.confirm")}
              </Checkbox>
              <Space>
                <Button
                  disabled={busy}
                  onClick={() => setStep(target ? "directory" : "sources")}
                >
                  {t("common.back")}
                </Button>
                <Button
                  type="primary"
                  loading={busy}
                  disabled={!confirmed}
                  onClick={() =>
                    void run(async () => {
                      await sharedFeishuApi.approve(
                        kbId,
                        scope,
                        review.fingerprint,
                        schedule,
                        autoAdd,
                      );
                      setStep("sources");
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
      </Drawer>
    </>
  );
}
