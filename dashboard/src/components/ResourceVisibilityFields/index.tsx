import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Button, Form, Radio, Select, Tag } from "antd";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useTranslation } from "react-i18next";
import cx from "classnames";
import {
  emptyScopeSelection,
  type ResourceScopeSelection,
  type ScopedResourceVisibilityMode,
} from "../../api/modules/erpOrg";
import {
  userGroupsApi,
  type ResourceVisibilityMode,
  type UserGroup,
} from "../../api/modules/userGroups";
import { ResourceAvatar, ROLE_AVATAR_PRESETS } from "../AvatarPicker";
import {
  ErpScopePanel,
  resetErpCatalogCacheForTest,
} from "./ErpScopePanel";
import {
  isVisibilityControlVisible,
  needsGroupCatalog,
  showGroupVisibilityOption,
  type AudienceSnapshot,
} from "./visibilityChrome";
import styles from "./ResourceVisibilityFields.module.less";

export type { ResourceVisibilityMode, ScopedResourceVisibilityMode };

export type ResourceVisibilityValue<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
> = {
  visibility: T;
  groupIds: string[];
};

export function resetResourceVisibilityCachesForTest() {
  audienceState = {
    status: "loading",
    userCount: null,
    groupCount: null,
    groupsLoaded: false,
    groupCountFromList: 0,
    groups: [],
    loadedAt: 0,
  };
  audienceInflight = null;
  resetErpCatalogCacheForTest();
}

type FieldsProps<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
> = {
  /** User-owned resources can stay private (`owner`). Admin resources omit it. */
  allowOwner: boolean;
  visibility: T;
  groupIds: string[];
  onChange: (next: ResourceVisibilityValue<T>) => void;
  disabled?: boolean;
  className?: string;
  enableErpScope?: boolean;
  isAdmin?: boolean;
  scope?: ResourceScopeSelection;
  onScopeChange?: (next: ResourceScopeSelection) => void;
};

type AudienceState = AudienceSnapshot & {
  groups: UserGroup[];
  loadedAt: number;
};

/** Collapse the gate and the fields mounting back-to-back into one request. */
const FRESH_MS = 500;

let audienceState: AudienceState = {
  status: "loading",
  userCount: null,
  groupCount: null,
  groupsLoaded: false,
  groupCountFromList: 0,
  groups: [],
  loadedAt: 0,
};
let audienceInflight: Promise<void> | null = null;
const audienceListeners = new Set<() => void>();

function publishAudience(next: AudienceState) {
  audienceState = next;
  audienceListeners.forEach((listener) => listener());
}

function loadAudience(): Promise<void> {
  if (
    audienceState.status === "ready" &&
    Date.now() - audienceState.loadedAt < FRESH_MS
  ) {
    return Promise.resolve();
  }
  if (audienceInflight) return audienceInflight;
  audienceInflight = (async () => {
    const counts = await userGroupsApi.audience().then(
      (row) => ({ ok: true as const, row }),
      () => ({ ok: false as const, row: null }),
    );
    const userCount = counts.ok ? counts.row.user_count : null;
    const groupCount = counts.ok ? counts.row.group_count : null;
    let groups: UserGroup[] = [];
    let groupsLoaded = false;
    if (needsGroupCatalog({ userCount, groupCount })) {
      try {
        groups = await userGroupsApi.list();
        groupsLoaded = true;
      } catch {
        groups = [];
      }
    } else {
      groupsLoaded = true;
    }
    publishAudience({
      status: "ready",
      userCount,
      groupCount,
      groupsLoaded,
      groupCountFromList: groups.length,
      groups,
      loadedAt: Date.now(),
    });
  })().finally(() => {
    audienceInflight = null;
  });
  return audienceInflight;
}

function useResourceAudience(): AudienceState {
  const [snap, setSnap] = useState(audienceState);

  useEffect(() => {
    const sync = () => setSnap(audienceState);
    audienceListeners.add(sync);
    void loadAudience().then(sync);
    return () => {
      audienceListeners.delete(sync);
    };
  }, []);

  return snap;
}

function GroupLabel({
  group,
  disabledLabel,
}: {
  group: UserGroup;
  disabledLabel: string;
}) {
  const name =
    group.enabled === false ? `${group.name} (${disabledLabel})` : group.name;
  return (
    <span className={styles.groupOption}>
      <ResourceAvatar
        url={group.avatar_url}
        icon={group.avatar_icon}
        presets={ROLE_AVATAR_PRESETS}
        className={styles.groupAvatar}
      />
      <span className={styles.groupName}>{name}</span>
    </span>
  );
}

/** Radio + optional multi-select for who may use a resource. */
export function ResourceVisibilityFields<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
>({
  allowOwner,
  visibility,
  groupIds,
  onChange,
  disabled = false,
  className,
  enableErpScope = false,
  isAdmin = false,
  scope,
  onScopeChange,
}: FieldsProps<T>) {
  const { t } = useTranslation();
  const audience = useResourceAudience();
  const groups = audience.groups;
  const byId = useMemo(
    () => new Map(groups.map((group) => [group.user_group_id, group])),
    [groups],
  );
  const disabledLabel = t("adminUsers.statusDisabled");
  const erpEnabled = Boolean(enableErpScope && isAdmin);
  const showGroups = showGroupVisibilityOption(audience, visibility);

  if (
    !isVisibilityControlVisible(audience, {
      allowOwner,
      visibility,
      enableErpScope,
      isAdmin,
    })
  ) {
    return null;
  }

  const radioOptions = erpEnabled
    ? [
        ...(allowOwner
          ? [{ value: "owner" as const, label: t("resourceVisibility.owner") }]
          : []),
        { value: "all" as const, label: t("resourceVisibility.all") },
        { value: "company" as const, label: t("resourceVisibility.company") },
        { value: "scoped" as const, label: t("resourceVisibility.scoped") },
        ...(visibility === "groups"
          ? [{ value: "groups" as const, label: t("resourceVisibility.groups") }]
          : []),
      ]
    : [
        ...(allowOwner
          ? [{ value: "owner" as const, label: t("resourceVisibility.owner") }]
          : []),
        { value: "all" as const, label: t("resourceVisibility.all") },
        ...(showGroups
          ? [{ value: "groups" as const, label: t("resourceVisibility.groups") }]
          : []),
      ];

  const options = groups.map((group) => ({
    value: group.user_group_id,
    label: <GroupLabel group={group} disabledLabel={disabledLabel} />,
  }));

  const groupSelectNode = (
    <Select
      className={styles.groups}
      mode="multiple"
      disabled={disabled}
      value={groupIds}
      placeholder={t("resourceVisibility.groupPlaceholder")}
      options={options}
      optionLabelProp="label"
      optionFilterProp="value"
      filterOption={(input, option) => {
        const group = byId.get(String(option?.value ?? ""));
        if (!group) return false;
        const q = input.trim().toLowerCase();
        return (
          group.name.toLowerCase().includes(q) ||
          group.user_group_id.toLowerCase().includes(q)
        );
      }}
      tagRender={(props) => {
        const group = byId.get(props.value);
        return (
          <Tag
            className={styles.groupTag}
            closable={props.closable}
            onClose={props.onClose}
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
          >
            {group ? (
              <GroupLabel group={group} disabledLabel={disabledLabel} />
            ) : (
              props.label
            )}
          </Tag>
        );
      }}
      onChange={(ids) => onChange({ visibility, groupIds: ids })}
    />
  );

  return (
    <div className={cx(styles.fields, className)}>
      <Radio.Group
        className={styles.radio}
        disabled={disabled}
        value={visibility}
        options={radioOptions}
        onChange={(event) => {
          const nextVis = event.target.value as T;
          onChange({
            visibility: nextVis,
            groupIds:
              nextVis === "groups" || nextVis === "scoped" ? groupIds : [],
          });
        }}
      />
      {visibility === "groups" ? groupSelectNode : null}
      {erpEnabled && onScopeChange ? (
        <ErpScopePanel
          visibility={visibility}
          scope={scope ?? emptyScopeSelection()}
          onScopeChange={onScopeChange}
          disabled={disabled}
          groupSelectNode={showGroups ? groupSelectNode : undefined}
        />
      ) : null}
    </div>
  );
}

type FormItemProps<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
> = {
  allowOwner: boolean;
  visibility: T;
  groupIds: string[];
  onChange: (next: ResourceVisibilityValue<T>) => void;
  disabled?: boolean;
  /** Override default ``resourceVisibility.label``. */
  label?: string;
  /** Hint under the control (e.g. skill package / connector copy). */
  extra?: string;
  className?: string;
  enableErpScope?: boolean;
  isAdmin?: boolean;
  scope?: ResourceScopeSelection;
  onScopeChange?: (next: ResourceScopeSelection) => void;
};

/** Drawer-friendly Form.Item wrapping ``ResourceVisibilityFields``. */
export function ResourceVisibilityFormItem<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
>({
  allowOwner,
  visibility,
  groupIds,
  onChange,
  disabled,
  label,
  extra,
  className,
  enableErpScope = false,
  isAdmin = false,
  scope,
  onScopeChange,
}: FormItemProps<T>) {
  const { t } = useTranslation();
  const audience = useResourceAudience();
  if (
    !isVisibilityControlVisible(audience, {
      allowOwner,
      visibility,
      enableErpScope,
      isAdmin,
    })
  ) {
    return null;
  }
  return (
    <Form.Item
      label={label ?? t("resourceVisibility.label")}
      extra={extra}
      className={cx(styles.formItem, className)}
      required={false}
    >
      <ResourceVisibilityFields
        allowOwner={allowOwner}
        visibility={visibility}
        groupIds={groupIds}
        onChange={onChange}
        disabled={disabled}
        enableErpScope={enableErpScope}
        isAdmin={isAdmin}
        scope={scope}
        onScopeChange={onScopeChange}
      />
    </Form.Item>
  );
}

type CollapseProps<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
> = {
  allowOwner?: boolean;
  visibility: T;
  groupIds: string[];
  onChange: (next: ResourceVisibilityValue<T>) => void;
  disabled?: boolean;
  /** Toggle label when collapsed (default: group access title). */
  title?: string;
  /** Toggle label when open. */
  hideTitle?: string;
  hint?: string;
  defaultOpen?: boolean;
  className?: string;
};

/**
 * Admin-style collapsible block (same interaction as model 「兼容性」).
 * Use inside provider/model forms for 「分组使用」.
 */
export function ResourceVisibilityCollapse<
  T extends ScopedResourceVisibilityMode = ResourceVisibilityMode,
>({
  allowOwner = false,
  visibility,
  groupIds,
  onChange,
  disabled,
  title,
  hideTitle,
  hint,
  defaultOpen = false,
  className,
}: CollapseProps<T>) {
  const { t } = useTranslation();
  const audience = useResourceAudience();
  const [open, setOpen] = useState(defaultOpen || visibility === "groups");
  const restricted = visibility === "groups";

  if (!isVisibilityControlVisible(audience, { allowOwner, visibility })) {
    return null;
  }
  return (
    <div className={cx(styles.collapse, className)}>
      <div className={styles.collapseToggle}>
        <Button
          type="link"
          size="small"
          style={{ padding: 0 }}
          icon={open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          onClick={() => setOpen((current) => !current)}
        >
          {open
            ? hideTitle ?? t("resourceVisibility.hideGroupAccess")
            : title ?? t("resourceVisibility.groupAccess")}
          {!open && restricted ? ` · ${t("resourceVisibility.groups")}` : null}
        </Button>
      </div>
      {open ? (
        <div className={styles.collapseBody}>
          {hint ? <p className={styles.collapseHint}>{hint}</p> : null}
          <ResourceVisibilityFields
            allowOwner={allowOwner}
            visibility={visibility}
            groupIds={groupIds}
            onChange={onChange}
            disabled={disabled}
          />
        </div>
      ) : null}
    </div>
  );
}

/** Hides the surrounding label when the picker itself has nothing to choose. */
export function ResourceVisibilityGate({
  allowOwner,
  visibility,
  enableErpScope = false,
  isAdmin = false,
  children,
}: {
  allowOwner: boolean;
  visibility: ScopedResourceVisibilityMode;
  enableErpScope?: boolean;
  isAdmin?: boolean;
  children: ReactNode;
}) {
  const audience = useResourceAudience();
  if (
    !isVisibilityControlVisible(audience, {
      allowOwner,
      visibility,
      enableErpScope,
      isAdmin,
    })
  ) {
    return null;
  }
  return children;
}

export default ResourceVisibilityFields;
