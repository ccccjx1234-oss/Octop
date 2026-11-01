import { useEffect, useRef, useState } from "react";
import { Button, Input, Popover } from "antd";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useTranslation } from "react-i18next";
import { feedbackApi } from "../../../api/modules/quality";
import styles from "../index.module.less";

const EXCERPT_CHARS = 300;
const CHAT_THREAD_PATH = /\/chat\/[^/]+\/([^/?#]+)/;

/** Thread id from the ``/chat/:agentId/:threadId`` URL (no router hook needed). */
function currentThreadId(): string | undefined {
  const match = CHAT_THREAD_PATH.exec(window.location.pathname);
  return match ? decodeURIComponent(match[1]) : undefined;
}

// One ratings request per thread, shared by every bubble in it.
const threadRatings = new Map<string, Promise<Record<string, number>>>();

function loadRatings(
  agentId: string,
  threadId: string,
): Promise<Record<string, number>> {
  const key = `${agentId}:${threadId}`;
  let pending = threadRatings.get(key);
  if (!pending) {
    pending = feedbackApi
      .threadRatings(agentId, threadId)
      .then((res) => res.ratings)
      .catch(() => ({}));
    threadRatings.set(key, pending);
  }
  return pending;
}

function rememberRating(
  agentId: string,
  threadId: string,
  messageId: string,
  rating: number,
): void {
  const key = `${agentId}:${threadId}`;
  const pending = threadRatings.get(key) ?? Promise.resolve({});
  threadRatings.set(
    key,
    pending.then((ratings) => ({ ...ratings, [messageId]: rating })),
  );
}

interface FeedbackButtonsProps {
  agentId: string;
  messageId: string;
  text: string;
}

export default function FeedbackButtons({
  agentId,
  messageId,
  text,
}: FeedbackButtonsProps) {
  const { t } = useTranslation();
  const threadId = currentThreadId();
  const [rating, setRating] = useState(0);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const touched = useRef(false);

  useEffect(() => {
    if (!threadId) return;
    let alive = true;
    void loadRatings(agentId, threadId).then((ratings) => {
      if (alive && !touched.current) setRating(ratings[messageId] ?? 0);
    });
    return () => {
      alive = false;
    };
  }, [agentId, threadId, messageId]);

  if (!threadId) return null;

  const send = (next: number, comment = "") => {
    touched.current = true;
    setRating(next);
    void feedbackApi
      .rate(
        agentId,
        threadId,
        messageId,
        next,
        text.slice(0, EXCERPT_CHARS),
        comment,
      )
      .then(() => rememberRating(agentId, threadId, messageId, next))
      .catch(() => setRating(0));
  };

  const onDown = () => {
    if (rating === -1) {
      send(0);
      return;
    }
    send(-1);
    setReason("");
    setReasonOpen(true);
  };

  return (
    <>
      <button
        className={`${styles.msgActionBtn} ${
          rating === 1 ? styles.msgActionBtnActive : ""
        }`}
        onClick={() => send(rating === 1 ? 0 : 1)}
        title={t("chat.feedbackUp")}
        aria-label={t("chat.feedbackUp")}
        aria-pressed={rating === 1}
        type="button"
      >
        <ThumbsUp size={13} />
      </button>
      <Popover
        open={reasonOpen}
        onOpenChange={setReasonOpen}
        trigger="click"
        content={
          <div style={{ width: 260 }}>
            <Input.TextArea
              autoFocus
              rows={3}
              maxLength={500}
              value={reason}
              placeholder={t("chat.feedbackReason")}
              onChange={(e) => setReason(e.target.value)}
            />
            <div style={{ marginTop: 8, textAlign: "right" }}>
              <Button
                size="small"
                type="primary"
                onClick={() => {
                  if (reason.trim()) send(-1, reason.trim());
                  setReasonOpen(false);
                }}
              >
                {t("quality.common.save")}
              </Button>
            </div>
          </div>
        }
      >
        <button
          className={`${styles.msgActionBtn} ${
            rating === -1 ? styles.msgActionBtnActive : ""
          }`}
          onClick={onDown}
          title={t("chat.feedbackDown")}
          aria-label={t("chat.feedbackDown")}
          aria-pressed={rating === -1}
          type="button"
        >
          <ThumbsDown size={13} />
        </button>
      </Popover>
    </>
  );
}
