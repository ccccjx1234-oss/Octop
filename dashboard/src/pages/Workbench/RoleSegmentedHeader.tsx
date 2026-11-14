import type { WorkbenchRoleMeta } from "../../api/modules/workbench";
import styles from "./workbench.module.less";

interface RoleSegmentedHeaderProps {
  title: string;
  subtitle: string;
  roles: WorkbenchRoleMeta[];
  activeRole: string;
  onRoleChange: (roleKey: string) => void;
}

export default function RoleSegmentedHeader({
  title,
  subtitle,
  roles,
  activeRole,
  onRoleChange,
}: RoleSegmentedHeaderProps) {
  return (
    <div className={styles.topBar}>
      <div className={styles.titleGroup}>
        <h1 className={styles.pageTitle}>{title}</h1>
        <p className={styles.pageSub}>{subtitle}</p>
      </div>
      <div className={styles.roleRow}>
        <span className={styles.roleCaption}>当前角色</span>
        <div className={styles.rolePills} role="tablist" aria-label="业务角色">
          {roles.map((r) => {
            const active = r.key === activeRole;
            return (
              <button
                key={r.key}
                type="button"
                role="tab"
                aria-selected={active}
                className={
                  active
                    ? `${styles.roleBtn} ${styles.roleBtnActive}`
                    : styles.roleBtn
                }
                onClick={() => onRoleChange(r.key)}
              >
                {r.label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
