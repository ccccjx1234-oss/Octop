import { message } from "../../../utils/antdMessage";
import { octopThreadsApi } from "../../../api/modules/octopThreads";
import i18n from "../../../i18n";
import { parseApiError } from "../../../utils/apiError";
import { patchStoredSession } from "../hooks/useSessions";

/** Persist the share flag and tell the user when it does not stick. */
export async function saveSessionShared(
  agentId: string,
  sessionId: string,
  contributed: boolean,
  onSaved: () => void,
  failedText: string,
): Promise<void> {
  try {
    await octopThreadsApi.patch(agentId, sessionId, { contributed });
  } catch (error) {
    message.error(
      parseApiError(error)?.code === "THREAD_PRIVATE_KNOWLEDGE"
        ? i18n.t("apiErrors.THREAD_PRIVATE_KNOWLEDGE")
        : failedText,
    );
    return;
  }
  patchStoredSession(sessionId, { contributed });
  onSaved();
}
