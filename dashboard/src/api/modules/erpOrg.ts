import { request } from "../request";
import type { ResourceVisibilityMode } from "./userGroups";

export type ScopedResourceVisibilityMode =
  | ResourceVisibilityMode
  | "company"
  | "scoped";

export interface ErpDepartmentItem {
  company_id: number;
  org_id: number;
  parent_id: number;
  name: string;
  org_code: string;
  sort: number;
  status: number;
  synced_at?: number;
}

export interface ErpDepartmentTreeNode extends ErpDepartmentItem {
  children: ErpDepartmentTreeNode[];
}

export interface ErpEmployeeItem {
  company_id: number;
  employee_id: number;
  account_user_id: number;
  user_id: number | null;
  has_octop_account: boolean;
  nick_name: string;
  phone: string;
  employee_no: string;
  status: number;
  is_admin: boolean;
  org_ids: number[];
  admin_org_ids: number[];
  org_names: string[];
  synced_at?: number;
}

export interface ErpLocalUserItem {
  user_id: number;
  username: string;
  display_name: string;
  role: string;
  role_name?: string | null;
  avatar_icon?: string | null;
  avatar_url?: string | null;
}

export interface ErpOrgStatus {
  integration_enabled: boolean;
  company_ids: number[];
  department_count: number;
  employee_count: number;
  last_synced_at: number | null;
}

export interface DepartmentGrant {
  company_id: number;
  org_id: number;
  include_children: boolean;
}

export interface ErpEmployeeGrant {
  company_id: number;
  employee_id: number;
}

export interface ResourceScopeExtensions {
  company_id?: number | null;
  department_grants?: DepartmentGrant[] | null;
  user_ids?: number[] | null;
  erp_employees?: ErpEmployeeGrant[] | null;
  deny_department_grants?: DepartmentGrant[] | null;
  deny_user_ids?: number[] | null;
  deny_erp_employees?: ErpEmployeeGrant[] | null;
}

export interface ResourceScopePayload extends ResourceScopeExtensions {
  visibility?: ScopedResourceVisibilityMode | null;
  group_ids?: string[] | null;
}

export interface ResourceScopeSelection {
  companyId: number;
  departmentGrants: DepartmentGrant[];
  userIds: number[];
  erpEmployees: ErpEmployeeGrant[];
  denyDepartmentGrants: DepartmentGrant[];
  denyUserIds: number[];
  denyErpEmployees: ErpEmployeeGrant[];
}

export interface ScopeKbGap {
  kb_id: string;
  name: string;
  visibility: string;
  reason: string;
}

export function emptyScopeSelection(): ResourceScopeSelection {
  return {
    companyId: 0,
    departmentGrants: [],
    userIds: [],
    erpEmployees: [],
    denyDepartmentGrants: [],
    denyUserIds: [],
    denyErpEmployees: [],
  };
}

export function scopeSelectionFromPayload(
  payload?: ResourceScopePayload | null,
): ResourceScopeSelection {
  if (!payload) return emptyScopeSelection();
  return {
    companyId: Number(payload.company_id || 0),
    departmentGrants: Array.isArray(payload.department_grants)
      ? payload.department_grants.map((item) => ({
          company_id: Number(item.company_id || 0),
          org_id: Number(item.org_id),
          include_children: item.include_children !== false,
        }))
      : [],
    userIds: Array.isArray(payload.user_ids)
      ? payload.user_ids.map((id) => Number(id))
      : [],
    erpEmployees: Array.isArray(payload.erp_employees)
      ? payload.erp_employees.map((item) => ({
          company_id: Number(item.company_id || 0),
          employee_id: Number(item.employee_id),
        }))
      : [],
    denyDepartmentGrants: Array.isArray(payload.deny_department_grants)
      ? payload.deny_department_grants.map((item) => ({
          company_id: Number(item.company_id || 0),
          org_id: Number(item.org_id),
          include_children: item.include_children !== false,
        }))
      : [],
    denyUserIds: Array.isArray(payload.deny_user_ids)
      ? payload.deny_user_ids.map((id) => Number(id))
      : [],
    denyErpEmployees: Array.isArray(payload.deny_erp_employees)
      ? payload.deny_erp_employees.map((item) => ({
          company_id: Number(item.company_id || 0),
          employee_id: Number(item.employee_id),
        }))
      : [],
  };
}

export function hasScopeAllowTarget(
  visibility: ScopedResourceVisibilityMode,
  groupIds: string[],
  scope: ResourceScopeSelection,
): boolean {
  if (visibility === "groups") return groupIds.length > 0;
  if (visibility !== "scoped") return true;
  return (
    groupIds.length > 0 ||
    scope.departmentGrants.length > 0 ||
    scope.userIds.length > 0 ||
    scope.erpEmployees.length > 0
  );
}

export function buildScopeRequestPayload(args: {
  visibility: ScopedResourceVisibilityMode;
  groupIds: string[];
  scope: ResourceScopeSelection;
  isAdmin?: boolean;
}): Record<string, unknown> {
  const { visibility, groupIds, scope } = args;
  const legacyGroupIds = visibility === "groups" ? groupIds : [];
  const hasDenies =
    scope.denyDepartmentGrants.length > 0 ||
    scope.denyUserIds.length > 0 ||
    scope.denyErpEmployees.length > 0;
  if (
    (visibility === "owner" ||
      visibility === "all" ||
      visibility === "groups") &&
    !hasDenies
  ) {
    return {
      visibility,
      group_ids: legacyGroupIds,
    };
  }
  return {
    visibility,
    group_ids:
      visibility === "groups" || visibility === "scoped" ? groupIds : [],
    company_id: visibility === "owner" ? 0 : scope.companyId || 0,
    department_grants:
      visibility === "scoped" ? scope.departmentGrants : [],
    user_ids: visibility === "scoped" ? scope.userIds : [],
    erp_employees: visibility === "scoped" ? scope.erpEmployees : [],
    deny_department_grants:
      visibility === "owner" ? [] : scope.denyDepartmentGrants,
    deny_user_ids: visibility === "owner" ? [] : scope.denyUserIds,
    deny_erp_employees:
      visibility === "owner" ? [] : scope.denyErpEmployees,
  };
}

function buildQuery(params: Record<string, string | number | boolean | undefined>): string {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== "") {
      sp.set(k, String(v));
    }
  });
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

export const erpOrgApi = {
  status: () => request<ErpOrgStatus>("/erp-org/status"),

  departments: (params?: { company_id?: number; include_inactive?: boolean }) =>
    request<{
      departments: ErpDepartmentItem[];
      tree: ErpDepartmentTreeNode[];
    }>(`/erp-org/departments${buildQuery(params ?? {})}`),

  employees: (params?: {
    company_id?: number;
    org_id?: number;
    include_children?: boolean;
    q?: string;
    active_only?: boolean;
  }) =>
    request<{ employees: ErpEmployeeItem[] }>(
      `/erp-org/employees${buildQuery(params ?? {})}`,
    ),

  localUsers: (q?: string) =>
    request<{ users: ErpLocalUserItem[] }>(
      `/erp-org/local-users${buildQuery({ q })}`,
    ),

  sync: (companyId?: number) =>
    request<{
      results: Record<string, unknown>[];
      summary: ErpOrgStatus;
    }>("/erp-org/sync", {
      method: "POST",
      body: JSON.stringify(
        companyId && companyId > 0 ? { company_id: companyId } : {},
      ),
    }),

  checkAgentScopeKbGaps: (agentId: string, body: Record<string, unknown>) =>
    request<{ gaps: ScopeKbGap[] }>(
      `/agents/${encodeURIComponent(agentId)}/scope-kb-check`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
};
