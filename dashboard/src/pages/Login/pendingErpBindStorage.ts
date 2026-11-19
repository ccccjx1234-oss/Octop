import type { ErpBindChallengeResponse } from "../../api/modules/auth";

export const PENDING_ERP_BIND_STORAGE_KEY = "octop:pending_erp_bind";

interface StoredPendingErpBind {
  payload: ErpBindChallengeResponse;
  expiresAt: number;
}

export function savePendingErpBind(payload: ErpBindChallengeResponse): void {
  try {
    const ttlMs = Math.max(1, Number(payload.expires_in) || 600) * 1000;
    const entry: StoredPendingErpBind = {
      payload,
      expiresAt: Date.now() + ttlMs,
    };
    sessionStorage.setItem(
      PENDING_ERP_BIND_STORAGE_KEY,
      JSON.stringify(entry),
    );
  } catch {
    // Ignore storage quota or private-mode access errors.
  }
}

export function loadPendingErpBind(): ErpBindChallengeResponse | null {
  try {
    const raw = sessionStorage.getItem(PENDING_ERP_BIND_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredPendingErpBind> | null;
    if (
      !parsed ||
      typeof parsed !== "object" ||
      typeof parsed.expiresAt !== "number" ||
      Date.now() >= parsed.expiresAt ||
      !parsed.payload ||
      parsed.payload.erp_bind_required !== true ||
      !parsed.payload.bind_ticket
    ) {
      sessionStorage.removeItem(PENDING_ERP_BIND_STORAGE_KEY);
      return null;
    }
    return parsed.payload;
  } catch {
    return null;
  }
}

export function clearPendingErpBind(): void {
  try {
    sessionStorage.removeItem(PENDING_ERP_BIND_STORAGE_KEY);
  } catch {
    // Ignore storage access errors.
  }
}
