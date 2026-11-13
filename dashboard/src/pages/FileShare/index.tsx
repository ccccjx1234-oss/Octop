import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Space, Spin, Typography } from "antd";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  fileSharesApi,
  type FileShareInfo,
} from "../../api/modules/fileShares";
import { apiErrorMessage } from "../../utils/apiError";
import { saveBlobAsFile } from "../../utils/saveBlobAsFile";

export default function FileSharePage() {
  const { shareId = "" } = useParams();
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [info, setInfo] = useState<FileShareInfo | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let current = true;
    setInfo(null);
    setFailed(false);
    fileSharesApi.info(shareId).then(
      (value) => {
        if (current) setInfo(value);
      },
      () => {
        if (current) setFailed(true);
      },
    );
    return () => {
      current = false;
    };
  }, [shareId]);

  const download = async () => {
    if (!info) return;
    setBusy(true);
    try {
      await saveBlobAsFile(
        await fileSharesApi.download(shareId),
        info.filename,
      );
    } catch (error) {
      message.error(apiErrorMessage(error, t("fileSharing.unavailable"), t));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    setBusy(true);
    try {
      await fileSharesApi.revoke(shareId);
      setFailed(true);
      setInfo(null);
      message.success(t("fileSharing.revoked"));
    } catch (error) {
      message.error(apiErrorMessage(error, t("fileSharing.unavailable"), t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={t("fileSharing.title")}
      style={{ maxWidth: 640, margin: "32px auto" }}
    >
      {failed ? (
        <Alert type="warning" message={t("fileSharing.unavailable")} />
      ) : !info ? (
        <Spin />
      ) : (
        <Space direction="vertical">
          <Typography.Title level={4}>{info.filename}</Typography.Title>
          <Typography.Text>{t("fileSharing.snapshotHint")}</Typography.Text>
          <Space>
            <Button
              type="primary"
              loading={busy}
              onClick={() => void download()}
            >
              {t("common.download")}
            </Button>
            {info.can_revoke && (
              <Button danger disabled={busy} onClick={() => void revoke()}>
                {t("fileSharing.revoke")}
              </Button>
            )}
          </Space>
        </Space>
      )}
    </Card>
  );
}
