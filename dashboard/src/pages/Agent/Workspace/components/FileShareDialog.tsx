import { useState } from "react";
import { Alert, App, Button, Input, Modal, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { fileSharesApi } from "../../../../api/modules/fileShares";
import { apiErrorMessage } from "../../../../utils/apiError";

interface Props {
  agentId: string;
  path: string;
  onClose: () => void;
}

export default function FileShareDialog({ agentId, path, onClose }: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [accounts, setAccounts] = useState("");
  const [busy, setBusy] = useState(false);
  const [shareId, setShareId] = useState<string | null>(null);

  const create = async () => {
    const names = [...new Set(accounts.split(/[,，\s]+/).filter(Boolean))];
    if (!names.length) return;
    setBusy(true);
    try {
      const share = await fileSharesApi.create(
        agentId,
        path.replace(/^\/+/, ""),
        names,
      );
      setShareId(share.share_id);
    } catch (error) {
      message.error(apiErrorMessage(error, t("fileSharing.unavailable"), t));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (!shareId) return;
    setBusy(true);
    try {
      await fileSharesApi.revoke(shareId);
      message.success(t("fileSharing.revoked"));
      onClose();
    } catch (error) {
      message.error(apiErrorMessage(error, t("fileSharing.unavailable"), t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t("fileSharing.title")}
      open
      onCancel={() => !busy && onClose()}
      footer={
        shareId ? (
          <Button danger loading={busy} onClick={() => void revoke()}>
            {t("fileSharing.revoke")}
          </Button>
        ) : (
          <Button
            type="primary"
            disabled={!accounts.trim()}
            loading={busy}
            onClick={() => void create()}
          >
            {t("fileSharing.create")}
          </Button>
        )
      }
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Text>{path}</Typography.Text>
        <Alert type="info" message={t("fileSharing.snapshotHint")} />
        {shareId ? (
          <Typography.Paragraph
            copyable={{ text: fileSharesApi.link(shareId) }}
          >
            <a href={fileSharesApi.link(shareId)}>
              {fileSharesApi.link(shareId)}
            </a>
          </Typography.Paragraph>
        ) : (
          <Input.TextArea
            aria-label={t("fileSharing.accounts")}
            placeholder={t("fileSharing.accounts")}
            value={accounts}
            onChange={(event) => setAccounts(event.target.value)}
          />
        )}
      </Space>
    </Modal>
  );
}
