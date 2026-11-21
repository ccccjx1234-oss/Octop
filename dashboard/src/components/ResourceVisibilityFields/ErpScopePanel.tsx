import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  App,
  Button,
  Dropdown,
  Input,
  Select,
  Switch,
  Tag,
  type MenuProps,
} from "antd";
import cx from "classnames";
import {
  Check,
  ChevronDown,
  ChevronUp,
  MoreHorizontal,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  erpOrgApi,
  type DepartmentGrant,
  type ErpDepartmentItem,
  type ErpDepartmentTreeNode,
  type ErpEmployeeGrant,
  type ErpEmployeeItem,
  type ErpLocalUserItem,
  type ErpOrgStatus,
  type ResourceScopeSelection,
  type ScopedResourceVisibilityMode,
} from "../../api/modules/erpOrg";
import { apiErrorMessage } from "../../utils/apiError";
import styles from "./ResourceVisibilityFields.module.less";

export type ErpCatalogState = {
  status: "idle" | "loading" | "ready" | "error";
  orgStatus: ErpOrgStatus | null;
  departments: ErpDepartmentItem[];
  tree: ErpDepartmentTreeNode[];
  employees: ErpEmployeeItem[];
  localUsers: ErpLocalUserItem[];
  loadedAt: number;
};

const CATALOG_TTL_MS = 10_000;

let catalogState: ErpCatalogState = {
  status: "idle",
  orgStatus: null,
  departments: [],
  tree: [],
  employees: [],
  localUsers: [],
  loadedAt: 0,
};
let catalogInflight: Promise<void> | null = null;
const catalogListeners = new Set<() => void>();

function publishCatalog(next: ErpCatalogState) {
  catalogState = next;
  catalogListeners.forEach((fn) => fn());
}

export function resetErpCatalogCacheForTest() {
  catalogState = {
    status: "idle",
    orgStatus: null,
    departments: [],
    tree: [],
    employees: [],
    localUsers: [],
    loadedAt: 0,
  };
  catalogInflight = null;
}

function loadErpCatalog(force = false): Promise<void> {
  if (
    !force &&
    catalogState.status === "ready" &&
    Date.now() - catalogState.loadedAt < CATALOG_TTL_MS
  ) {
    return Promise.resolve();
  }
  if (!force && catalogInflight) return catalogInflight;

  publishCatalog({ ...catalogState, status: "loading" });
  catalogInflight = (async () => {
    try {
      const [orgStatus, deptRes, empRes, userRes] = await Promise.all([
        erpOrgApi.status(),
        erpOrgApi.departments(),
        erpOrgApi.employees(),
        erpOrgApi.localUsers(),
      ]);
      publishCatalog({
        status: "ready",
        orgStatus,
        departments: deptRes.departments ?? [],
        tree: deptRes.tree ?? [],
        employees: empRes.employees ?? [],
        localUsers: userRes.users ?? [],
        loadedAt: Date.now(),
      });
    } catch {
      publishCatalog({
        ...catalogState,
        status: "error",
        loadedAt: Date.now(),
      });
    } finally {
      catalogInflight = null;
    }
  })();
  return catalogInflight;
}

export function useErpCatalog(enabled: boolean): {
  catalog: ErpCatalogState;
  refresh: () => Promise<void>;
} {
  const [snap, setSnap] = useState(catalogState);

  useEffect(() => {
    if (!enabled) return;
    const sync = () => setSnap(catalogState);
    catalogListeners.add(sync);
    void loadErpCatalog(false).then(sync);
    return () => {
      catalogListeners.delete(sync);
    };
  }, [enabled]);

  const refresh = useCallback(async () => {
    await loadErpCatalog(true);
    setSnap(catalogState);
  }, []);

  return { catalog: snap, refresh };
}

function deptKey(companyId: number, orgId: number): string {
  return `${companyId}:${orgId}`;
}

function parseDeptKey(key: string): { company_id: number; org_id: number } | null {
  const parts = String(key).split(":");
  if (parts.length !== 2) return null;
  const companyId = Number(parts[0]);
  const orgId = Number(parts[1]);
  if (!Number.isFinite(companyId) || !Number.isFinite(orgId)) return null;
  return { company_id: companyId, org_id: orgId };
}

function empKey(companyId: number, employeeId: number): string {
  return `emp:${companyId}:${employeeId}`;
}

function userKey(userId: number): string {
  return `user:${userId}`;
}

type FilteredTreeNode = {
  node: ErpDepartmentTreeNode;
  children: FilteredTreeNode[];
  forceExpand: boolean;
};

function computeDeptCounts(
  tree: ErpDepartmentTreeNode[],
  employees: ErpEmployeeItem[],
): Map<string, number> {
  const directMap = new Map<string, Set<string>>();
  for (const emp of employees) {
    if (emp.status !== 1) continue;
    const memberId = `${emp.company_id}:${emp.employee_id}`;
    for (const oid of emp.org_ids ?? []) {
      const k = deptKey(emp.company_id, oid);
      let set = directMap.get(k);
      if (!set) {
        set = new Set<string>();
        directMap.set(k, set);
      }
      set.add(memberId);
    }
  }

  const counts = new Map<string, number>();
  const walk = (node: ErpDepartmentTreeNode): Set<string> => {
    const k = deptKey(node.company_id, node.org_id);
    const aggregated = new Set<string>(directMap.get(k) ?? []);
    for (const child of node.children ?? []) {
      for (const id of walk(child)) {
        aggregated.add(id);
      }
    }
    counts.set(k, aggregated.size);
    return aggregated;
  };
  for (const root of tree) {
    walk(root);
  }
  return counts;
}

function filterDeptTree(
  nodes: ErpDepartmentTreeNode[],
  query: string,
): FilteredTreeNode[] {
  const q = query.trim().toLowerCase();
  if (!q) {
    return nodes.map((node) => ({
      node,
      children: filterDeptTree(node.children ?? [], ""),
      forceExpand: false,
    }));
  }
  const out: FilteredTreeNode[] = [];
  for (const node of nodes) {
    const matchedChildren = filterDeptTree(node.children ?? [], q);
    const selfMatches =
      node.name.toLowerCase().includes(q) ||
      (node.org_code ?? "").toLowerCase().includes(q);
    if (selfMatches || matchedChildren.length > 0) {
      out.push({
        node,
        children:
          matchedChildren.length > 0
            ? matchedChildren
            : selfMatches
              ? filterDeptTree(node.children ?? [], "")
              : [],
        forceExpand: matchedChildren.length > 0,
      });
    }
  }
  return out;
}

type DepartmentScopePickerProps = {
  label: string;
  placeholder: string;
  value: DepartmentGrant[];
  tree: ErpDepartmentTreeNode[];
  employees: ErpEmployeeItem[];
  deptByKey: Map<string, ErpDepartmentItem>;
  disabled?: boolean;
  onChange: (next: DepartmentGrant[]) => void;
};

function DepartmentScopePicker({
  label,
  placeholder,
  value,
  tree,
  employees,
  deptByKey,
  disabled,
  onChange,
}: DepartmentScopePickerProps) {
  const { t } = useTranslation();
  const [searchText, setSearchText] = useState("");
  const [expandOverrides, setExpandOverrides] = useState<
    Record<string, boolean>
  >({});

  const deptCounts = useMemo(
    () => computeDeptCounts(tree, employees),
    [tree, employees],
  );
  const filteredNodes = useMemo(
    () => filterDeptTree(tree, searchText),
    [tree, searchText],
  );
  const defaultExpandedSet = useMemo(() => {
    const set = new Set<string>();
    for (const root of tree) {
      if (root.children?.length) {
        set.add(deptKey(root.company_id, root.org_id));
      }
      for (const child of root.children ?? []) {
        if (child.children?.length) {
          set.add(deptKey(child.company_id, child.org_id));
        }
      }
    }
    return set;
  }, [tree]);

  const selectedMap = useMemo(() => {
    const map = new Map<string, DepartmentGrant>();
    for (const item of value) {
      map.set(deptKey(item.company_id, item.org_id), item);
    }
    return map;
  }, [value]);

  const upsertGrant = (
    companyId: number,
    orgId: number,
    includeChildren: boolean,
  ) => {
    const exists = value.some(
      (item) => item.company_id === companyId && item.org_id === orgId,
    );
    if (exists) {
      onChange(
        value.map((item) =>
          item.company_id === companyId && item.org_id === orgId
            ? { ...item, include_children: includeChildren }
            : item,
        ),
      );
      return;
    }
    onChange([
      ...value,
      {
        company_id: companyId,
        org_id: orgId,
        include_children: includeChildren,
      },
    ]);
  };

  const removeGrant = (companyId: number, orgId: number) => {
    onChange(
      value.filter(
        (item) => !(item.company_id === companyId && item.org_id === orgId),
      ),
    );
  };

  const toggleNode = (node: ErpDepartmentTreeNode) => {
    if (disabled) return;
    const k = deptKey(node.company_id, node.org_id);
    if (selectedMap.has(k)) {
      removeGrant(node.company_id, node.org_id);
    } else {
      upsertGrant(node.company_id, node.org_id, true);
    }
  };

  const toggleExpand = (key: string, currentExpanded: boolean) => {
    setExpandOverrides((prev) => ({
      ...prev,
      [key]: !currentExpanded,
    }));
  };

  const renderTreeNodes = (
    items: FilteredTreeNode[],
    depth: number,
  ): ReactNode =>
    items.map((item) => {
      const { node } = item;
      const k = deptKey(node.company_id, node.org_id);
      const hasChildren = item.children.length > 0;
      const expanded =
        item.forceExpand ||
        (expandOverrides[k] ?? defaultExpandedSet.has(k));
      const grant = selectedMap.get(k);
      const selected = Boolean(grant);
      const count = deptCounts.get(k) ?? 0;

      const menuItems: MenuProps["items"] = selected
        ? [
            {
              key: "include_children",
              label: (
                <span className={styles.deptMenuOption}>
                  <span>{t("resourceVisibility.includeChildren")}</span>
                  {grant?.include_children ? <Check size={12} /> : null}
                </span>
              ),
            },
            {
              key: "current_only",
              label: (
                <span className={styles.deptMenuOption}>
                  <span>{t("resourceVisibility.currentDeptOnly")}</span>
                  {grant && !grant.include_children ? (
                    <Check size={12} />
                  ) : null}
                </span>
              ),
            },
            { type: "divider" },
            {
              key: "remove",
              danger: true,
              label: t("resourceVisibility.removeGrant"),
            },
          ]
        : [
            {
              key: "include_children",
              label: t("resourceVisibility.includeChildren"),
            },
            {
              key: "current_only",
              label: t("resourceVisibility.currentDeptOnly"),
            },
          ];

      const handleMenuClick: MenuProps["onClick"] = ({
        key: action,
        domEvent,
      }) => {
        domEvent.stopPropagation();
        if (disabled) return;
        if (action === "remove") {
          removeGrant(node.company_id, node.org_id);
          return;
        }
        upsertGrant(node.company_id, node.org_id, action !== "current_only");
      };

      return (
        <div key={k}>
          <div
            className={cx(
              styles.deptTreeNodeRow,
              selected && styles.deptTreeNodeRowSelected,
              disabled && styles.deptTreeNodeRowDisabled,
            )}
            style={{ paddingLeft: `${depth * 18 + 8}px` }}
            onClick={() => toggleNode(node)}
          >
            <span className={styles.deptTreeNodeMain}>
              {hasChildren ? (
                <button
                  type="button"
                  className={styles.deptTreeCaret}
                  aria-label={expanded ? "collapse" : "expand"}
                  onClick={(e) => {
                    e.stopPropagation();
                    toggleExpand(k, expanded);
                  }}
                >
                  <svg
                    viewBox="0 0 10 10"
                    width="9"
                    height="9"
                    aria-hidden="true"
                    className={cx(
                      styles.deptTreeCaretSvg,
                      !expanded && styles.deptTreeCaretCollapsed,
                    )}
                  >
                    <path d="M1.5 3 L8.5 3 L5 7.5 Z" fill="currentColor" />
                  </svg>
                </button>
              ) : (
                <span className={styles.deptTreeCaretPlaceholder} />
              )}
              <span
                className={cx(
                  styles.deptTreeNodeName,
                  depth === 0 && styles.deptTreeNodeNameRoot,
                )}
              >
                {node.name}
              </span>
              <span
                className={
                  depth === 0
                    ? styles.deptTreeNodeCountRoot
                    : styles.deptTreeNodeCount
                }
              >
                ({count})
              </span>
            </span>
            {!disabled ? (
              <Dropdown
                trigger={["click"]}
                menu={{ items: menuItems, onClick: handleMenuClick }}
              >
                <button
                  type="button"
                  className={cx(
                    styles.deptTreeMoreBtn,
                    selected && styles.deptTreeMoreBtnVisible,
                  )}
                  aria-label={node.name}
                  onClick={(e) => e.stopPropagation()}
                >
                  <MoreHorizontal size={15} />
                </button>
              </Dropdown>
            ) : null}
          </div>
          {hasChildren && expanded ? renderTreeNodes(item.children, depth + 1) : null}
        </div>
      );
    });

  return (
    <div className={styles.erpSection}>
      <span className={styles.erpSectionLabel}>{label}</span>
      <Input
        allowClear
        size="small"
        disabled={disabled}
        className={styles.deptTreeSearch}
        prefix={<Search size={13} />}
        value={searchText}
        placeholder={placeholder}
        onChange={(e) => setSearchText(e.target.value)}
      />
      {filteredNodes.length > 0 ? (
        <div className={styles.deptTreeContainer}>
          {renderTreeNodes(filteredNodes, 0)}
        </div>
      ) : null}
      {value.length > 0 ? (
        <div className={styles.deptGrantList}>
          {value.map((grant) => {
            const key = deptKey(grant.company_id, grant.org_id);
            const dept = deptByKey.get(key);
            return (
              <div key={key} className={styles.deptGrantRow}>
                <span className={styles.deptGrantName}>
                  {dept ? dept.name : `#${grant.org_id}`}
                </span>
                <div className={styles.deptGrantActions}>
                  <Switch
                    size="small"
                    disabled={disabled}
                    checked={grant.include_children}
                    checkedChildren={t("resourceVisibility.includeChildren")}
                    unCheckedChildren={t("resourceVisibility.currentDeptOnly")}
                    onChange={(checked) => {
                      upsertGrant(grant.company_id, grant.org_id, checked);
                    }}
                  />
                  <Button
                    type="text"
                    size="small"
                    disabled={disabled}
                    aria-label={t("resourceVisibility.removeGrant")}
                    icon={<X size={12} />}
                    onClick={() => removeGrant(grant.company_id, grant.org_id)}
                  />
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

type PersonScopePickerProps = {
  label: string;
  placeholder: string;
  userIds: number[];
  erpEmployees: ErpEmployeeGrant[];
  employees: ErpEmployeeItem[];
  localUsers: ErpLocalUserItem[];
  disabled?: boolean;
  onChange: (nextUsers: number[], nextEmps: ErpEmployeeGrant[]) => void;
};

function PersonScopePicker({
  label,
  placeholder,
  userIds,
  erpEmployees,
  employees,
  localUsers,
  disabled,
  onChange,
}: PersonScopePickerProps) {
  const { t } = useTranslation();

  const selectedValues = useMemo(
    () => [
      ...erpEmployees.map((item) => empKey(item.company_id, item.employee_id)),
      ...userIds.map((uid) => userKey(uid)),
    ],
    [erpEmployees, userIds],
  );

  const searchTextByValue = useMemo(() => {
    const map = new Map<string, string>();
    employees.forEach((emp) => {
      const k = empKey(emp.company_id, emp.employee_id);
      map.set(
        k,
        `${emp.nick_name} ${emp.employee_no} ${emp.phone} ${(emp.org_names ?? []).join(" ")}`.toLowerCase(),
      );
    });
    localUsers.forEach((u) => {
      const k = userKey(u.user_id);
      map.set(k, `${u.display_name} ${u.username}`.toLowerCase());
    });
    return map;
  }, [employees, localUsers]);

  const options = useMemo(() => {
    const groups: {
      label: string;
      options: { value: string; label: ReactNode }[];
    }[] = [];
    if (employees.length > 0) {
      groups.push({
        label: t("resourceVisibility.erpEmployeeGroup"),
        options: employees.map((emp) => {
          const deptText = (emp.org_names ?? []).join(" / ");
          const sub = [emp.employee_no, deptText].filter(Boolean).join(" · ");
          return {
            value: empKey(emp.company_id, emp.employee_id),
            label: (
              <span className={styles.personOption}>
                <span className={styles.personMain}>
                  <span>{emp.nick_name || `#${emp.employee_id}`}</span>
                  {sub ? <span className={styles.personMeta}>{sub}</span> : null}
                </span>
                <Tag color={emp.has_octop_account ? "green" : "orange"}>
                  {emp.has_octop_account
                    ? t("resourceVisibility.boundAccountBadge")
                    : t("resourceVisibility.preLoginBadge")}
                </Tag>
              </span>
            ),
          };
        }),
      });
    }
    if (localUsers.length > 0) {
      groups.push({
        label: t("resourceVisibility.localUserGroup"),
        options: localUsers.map((u) => ({
          value: userKey(u.user_id),
          label: (
            <span className={styles.personOption}>
              <span className={styles.personMain}>
                <span>{u.display_name}</span>
                <span className={styles.personMeta}>@{u.username}</span>
              </span>
            </span>
          ),
        })),
      });
    }
    return groups;
  }, [employees, localUsers, t]);

  const handleSelectChange = (vals: string[]) => {
    const nextEmps: ErpEmployeeGrant[] = [];
    const nextUsers: number[] = [];
    for (const raw of vals) {
      if (raw.startsWith("emp:")) {
        const parts = raw.split(":");
        const cid = Number(parts[1]);
        const eid = Number(parts[2]);
        if (Number.isFinite(cid) && Number.isFinite(eid)) {
          nextEmps.push({ company_id: cid, employee_id: eid });
        }
      } else if (raw.startsWith("user:")) {
        const uid = Number(raw.slice(5));
        if (Number.isFinite(uid)) {
          nextUsers.push(uid);
        }
      }
    }
    onChange(nextUsers, nextEmps);
  };

  return (
    <div className={styles.erpSection}>
      <span className={styles.erpSectionLabel}>{label}</span>
      <Select
        mode="multiple"
        allowClear
        showSearch
        disabled={disabled}
        value={selectedValues}
        placeholder={placeholder}
        options={options}
        optionLabelProp="label"
        filterOption={(input, option) => {
          const q = input.trim().toLowerCase();
          if (!q) return true;
          const optVal =
            option && "value" in option ? String(option.value ?? "") : "";
          const text = searchTextByValue.get(optVal) ?? "";
          return text.includes(q);
        }}
        onChange={(vals) =>
          handleSelectChange(Array.isArray(vals) ? vals.map(String) : [])
        }
      />
    </div>
  );
}

type ErpScopePanelProps = {
  visibility: ScopedResourceVisibilityMode;
  scope: ResourceScopeSelection;
  onScopeChange: (next: ResourceScopeSelection) => void;
  disabled?: boolean;
  groupSelectNode?: ReactNode;
};

export function ErpScopePanel({
  visibility,
  scope,
  onScopeChange,
  disabled,
  groupSelectNode,
}: ErpScopePanelProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const hasInitialDeny =
    scope.denyDepartmentGrants.length > 0 ||
    scope.denyUserIds.length > 0 ||
    scope.denyErpEmployees.length > 0;
  const [denyOpen, setDenyOpen] = useState(hasInitialDeny);

  useEffect(() => {
    if (hasInitialDeny) {
      setDenyOpen(true);
    }
  }, [hasInitialDeny]);

  const needsCatalog =
    visibility === "company" || visibility === "scoped" || denyOpen;
  const { catalog, refresh } = useErpCatalog(needsCatalog);
  const [syncing, setSyncing] = useState(false);

  const companyIds = useMemo(() => {
    const ids = new Set<number>();
    catalog.orgStatus?.company_ids?.forEach((c) => {
      if (c > 0) ids.add(c);
    });
    catalog.departments.forEach((d) => {
      if (d.company_id > 0) ids.add(d.company_id);
    });
    catalog.employees.forEach((e) => {
      if (e.company_id > 0) ids.add(e.company_id);
    });
    return Array.from(ids).sort((a, b) => a - b);
  }, [catalog]);

  const activeCompanyId = useMemo(() => {
    if (scope.companyId > 0) return scope.companyId;
    if (companyIds.length === 1) return companyIds[0];
    return 0;
  }, [scope.companyId, companyIds]);

  const filteredTree = useMemo(() => {
    if (!activeCompanyId) return catalog.tree;
    return catalog.tree.filter((n) => n.company_id === activeCompanyId);
  }, [catalog.tree, activeCompanyId]);

  const filteredEmployees = useMemo(() => {
    if (!activeCompanyId) return catalog.employees;
    return catalog.employees.filter((e) => e.company_id === activeCompanyId);
  }, [catalog.employees, activeCompanyId]);

  const deptByKey = useMemo(() => {
    const map = new Map<string, ErpDepartmentItem>();
    catalog.departments.forEach((d) => {
      map.set(deptKey(d.company_id, d.org_id), d);
    });
    return map;
  }, [catalog.departments]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      await erpOrgApi.sync(activeCompanyId > 0 ? activeCompanyId : undefined);
      await refresh();
      message.success(t("resourceVisibility.erpSyncSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("resourceVisibility.erpSyncFailed"), t),
      );
    } finally {
      setSyncing(false);
    }
  };

  if (visibility === "owner") {
    return null;
  }

  const denyCount =
    scope.denyDepartmentGrants.length +
    scope.denyUserIds.length +
    scope.denyErpEmployees.length;

  const denyToggleButton = (
    <div>
      <Button
        type="link"
        size="small"
        style={{ padding: 0 }}
        icon={denyOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        onClick={() => setDenyOpen((prev) => !prev)}
      >
        {denyOpen
          ? t("resourceVisibility.hideDenyPanel")
          : t("resourceVisibility.showDenyPanel")}
        {denyCount > 0 ? ` (${denyCount})` : ""}
      </Button>
    </div>
  );

  if (!needsCatalog) {
    return <div className={styles.denyBlock}>{denyToggleButton}</div>;
  }

  return (
    <div className={styles.erpPanel}>
      <div className={styles.erpHeader}>
        <span className={styles.erpStats}>
          {t("resourceVisibility.erpStats", {
            depts:
              catalog.orgStatus?.department_count ?? catalog.departments.length,
            employees:
              catalog.orgStatus?.employee_count ?? catalog.employees.length,
          })}
        </span>
        <Button
          size="small"
          type="default"
          loading={syncing}
          disabled={disabled}
          icon={<RefreshCw size={12} />}
          onClick={() => void handleSync()}
        >
          {t("resourceVisibility.erpSyncButton")}
        </Button>
      </div>

      {companyIds.length === 0 && catalog.status === "ready" ? (
        <span className={styles.collapseHint}>
          {t("resourceVisibility.erpEmptyHint")}
        </span>
      ) : null}

      {companyIds.length > 1 &&
      (visibility === "company" || visibility === "scoped") ? (
        <div className={styles.erpSection}>
          <span className={styles.erpSectionLabel}>
            {t("resourceVisibility.companySelectLabel")}
          </span>
          <Select
            allowClear
            disabled={disabled}
            value={scope.companyId > 0 ? scope.companyId : undefined}
            placeholder={t("resourceVisibility.companySelectPlaceholder")}
            options={companyIds.map((cid) => ({
              value: cid,
              label: t("resourceVisibility.companyOption", { id: cid }),
            }))}
            onChange={(cid) => {
              const nextCid = cid ? Number(cid) : 0;
              onScopeChange({
                ...scope,
                companyId: nextCid,
                departmentGrants:
                  nextCid > 0
                    ? scope.departmentGrants.filter(
                        (g) => g.company_id === nextCid,
                      )
                    : scope.departmentGrants,
                erpEmployees:
                  nextCid > 0
                    ? scope.erpEmployees.filter(
                        (e) => e.company_id === nextCid,
                      )
                    : scope.erpEmployees,
                denyDepartmentGrants:
                  nextCid > 0
                    ? scope.denyDepartmentGrants.filter(
                        (g) => g.company_id === nextCid,
                      )
                    : scope.denyDepartmentGrants,
                denyErpEmployees:
                  nextCid > 0
                    ? scope.denyErpEmployees.filter(
                        (e) => e.company_id === nextCid,
                      )
                    : scope.denyErpEmployees,
              });
            }}
          />
        </div>
      ) : null}

      {visibility === "company" ? (
        <span className={styles.collapseHint}>
          {t("resourceVisibility.companyHint")}
        </span>
      ) : null}

      {visibility === "scoped" ? (
        <>
          <DepartmentScopePicker
            label={t("resourceVisibility.deptLabel")}
            placeholder={t("resourceVisibility.deptPlaceholder")}
            value={scope.departmentGrants}
            tree={filteredTree}
            employees={filteredEmployees}
            deptByKey={deptByKey}
            disabled={disabled}
            onChange={(next) =>
              onScopeChange({ ...scope, departmentGrants: next })
            }
          />
          <PersonScopePicker
            label={t("resourceVisibility.personLabel")}
            placeholder={t("resourceVisibility.personPlaceholder")}
            userIds={scope.userIds}
            erpEmployees={scope.erpEmployees}
            employees={filteredEmployees}
            localUsers={catalog.localUsers}
            disabled={disabled}
            onChange={(nextUsers, nextEmps) =>
              onScopeChange({
                ...scope,
                userIds: nextUsers,
                erpEmployees: nextEmps,
              })
            }
          />
          {groupSelectNode ? (
            <div className={styles.erpSection}>
              <span className={styles.erpSectionLabel}>
                {t("resourceVisibility.userGroupLabel")}
              </span>
              {groupSelectNode}
            </div>
          ) : null}
        </>
      ) : null}

      <div className={styles.denyBlock}>
        {denyToggleButton}
        {denyOpen ? (
          <>
            <span className={styles.collapseHint}>
              {t("resourceVisibility.denyHint")}
            </span>
            <DepartmentScopePicker
              label={t("resourceVisibility.denyDeptLabel")}
              placeholder={t("resourceVisibility.denyDeptPlaceholder")}
              value={scope.denyDepartmentGrants}
              tree={filteredTree}
              employees={filteredEmployees}
              deptByKey={deptByKey}
              disabled={disabled}
              onChange={(next) =>
                onScopeChange({ ...scope, denyDepartmentGrants: next })
              }
            />
            <PersonScopePicker
              label={t("resourceVisibility.denyPersonLabel")}
              placeholder={t("resourceVisibility.denyPersonPlaceholder")}
              userIds={scope.denyUserIds}
              erpEmployees={scope.denyErpEmployees}
              employees={filteredEmployees}
              localUsers={catalog.localUsers}
              disabled={disabled}
              onChange={(nextUsers, nextEmps) =>
                onScopeChange({
                  ...scope,
                  denyUserIds: nextUsers,
                  denyErpEmployees: nextEmps,
                })
              }
            />
          </>
        ) : null}
      </div>
    </div>
  );
}
