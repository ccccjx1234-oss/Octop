"""Huihuo ERP organization directory projection and closure queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts, sql_in_placeholders


def _get_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


@dataclass(frozen=True)
class ErpDepartmentRow:
    company_id: int
    org_id: int
    parent_id: int
    name: str
    org_code: str
    sort: int
    status: int
    synced_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ErpDepartmentRow:
        return cls(
            company_id=int(r["company_id"]),
            org_id=int(r["org_id"]),
            parent_id=int(r["parent_id"]),
            name=str(r["name"] or ""),
            org_code=str(r["org_code"] or ""),
            sort=int(r["sort"] or 0),
            status=int(r["status"] if r["status"] is not None else 1),
            synced_at=int(r["synced_at"] or 0),
        )


@dataclass(frozen=True)
class ErpDirectoryEmployeeRow:
    company_id: int
    employee_id: int
    account_user_id: int | None
    user_id: int | None
    nick_name: str
    phone: str
    employee_no: str
    status: int
    is_admin: int
    synced_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ErpDirectoryEmployeeRow:
        return cls(
            company_id=int(r["company_id"]),
            employee_id=int(r["employee_id"]),
            account_user_id=(
                int(r["account_user_id"]) if r["account_user_id"] is not None else None
            ),
            user_id=int(r["user_id"]) if r["user_id"] is not None else None,
            nick_name=str(r["nick_name"] or ""),
            phone=str(r["phone"] or ""),
            employee_no=str(r["employee_no"] or ""),
            status=int(r["status"] if r["status"] is not None else 1),
            is_admin=int(r["is_admin"] or 0),
            synced_at=int(r["synced_at"] or 0),
        )


@dataclass(frozen=True)
class UserErpContext:
    user_id: int
    company_id: int
    employee_id: int
    active_in_erp: bool
    direct_org_ids: frozenset[int]
    ancestor_org_ids: frozenset[int]
    admin_org_ids: frozenset[int]


def _build_closure_rows(
    company_id: int,
    parent_by_org: dict[int, int],
) -> list[tuple[int, int, int, int]]:
    rows: list[tuple[int, int, int, int]] = []
    for org_id in sorted(parent_by_org):
        curr: int | None = org_id
        depth = 0
        seen: set[int] = set()
        while curr is not None and curr not in seen and depth <= 100:
            seen.add(curr)
            rows.append((company_id, curr, org_id, depth))
            parent = parent_by_org.get(curr)
            if parent is None or parent == curr or parent not in parent_by_org:
                break
            curr = parent
            depth += 1
    return rows


class ErpOrgRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def sync_company_directory(
        self,
        company_id: int,
        departments: Sequence[Any],
        employees: Sequence[Any],
        *,
        synced_at: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts() if synced_at is None else int(synced_at)
        cid = int(company_id)
        dept_items: list[tuple[int, int, str, str, int, int]] = []
        seen_org_ids: set[int] = set()
        active_parents: dict[int, int] = {}
        for item in departments:
            raw_org_id = _get_field(item, "org_id", _get_field(item, "orgId"))
            if raw_org_id is None:
                continue
            org_id = int(raw_org_id)
            if org_id <= 0 or org_id in seen_org_ids:
                continue
            seen_org_ids.add(org_id)
            raw_parent = _get_field(item, "parent_id", _get_field(item, "parentId", 1))
            parent_id = int(raw_parent) if raw_parent is not None else 1
            name = str(_get_field(item, "name", "") or "").strip() or str(org_id)
            org_code = str(_get_field(item, "org_code", _get_field(item, "orgCode", "")) or "")
            raw_sort = _get_field(item, "sort", org_id)
            sort = int(raw_sort) if raw_sort is not None else org_id
            raw_status = _get_field(item, "status", 1)
            status = int(raw_status) if raw_status is not None else 1
            dept_items.append((org_id, parent_id, name, org_code, sort, status))
            if status == 1:
                active_parents[org_id] = parent_id

        closure_rows = _build_closure_rows(cid, active_parents)

        emp_items: list[
            tuple[int, int | None, str, str, str, int, int, list[int], set[int]]
        ] = []
        seen_emp_ids: set[int] = set()
        for item in employees:
            raw_emp_id = _get_field(item, "employee_id", _get_field(item, "employeeId"))
            if raw_emp_id is None:
                continue
            emp_id = int(raw_emp_id)
            if emp_id <= 0 or emp_id in seen_emp_ids:
                continue
            seen_emp_ids.add(emp_id)
            raw_acc = _get_field(item, "account_user_id", _get_field(item, "accountUserId"))
            acc_id = int(raw_acc) if raw_acc is not None else None
            nick_name = str(_get_field(item, "nick_name", _get_field(item, "nickName", "")) or "")
            phone = str(_get_field(item, "phone", "") or "")
            employee_no = str(
                _get_field(item, "employee_no", _get_field(item, "employeeNo", "")) or ""
            )
            raw_status = _get_field(item, "status", 1)
            status = int(raw_status) if raw_status is not None else 1
            raw_admin = _get_field(item, "is_admin", _get_field(item, "isAdmin", 0))
            is_admin = 1 if int(raw_admin or 0) == 1 else 0
            raw_orgs = _get_field(item, "org_ids", _get_field(item, "orgIds")) or []
            org_ids: list[int] = []
            seen_emp_orgs: set[int] = set()
            for raw_o in raw_orgs:
                oid = int(raw_o)
                if oid > 0 and oid not in seen_emp_orgs:
                    seen_emp_orgs.add(oid)
                    org_ids.append(oid)
            raw_admin_orgs = (
                _get_field(item, "admin_org_ids", _get_field(item, "adminOrgIds")) or []
            )
            admin_org_ids = {int(o) for o in raw_admin_orgs if int(o) > 0}
            emp_items.append(
                (emp_id, acc_id, nick_name, phone, employee_no, status, is_admin, org_ids, admin_org_ids)
            )

        revoked_user_ids: set[int] = set()
        with self._db.transaction() as conn:
            # Resolve known local Octop user_id mappings for this company.
            bound_map: dict[int, int] = {}
            for r in conn.execute(
                "SELECT employee_id, user_id, unlinked FROM youli_identity_binding WHERE company_id = ?",
                (cid,),
            ).fetchall():
                if int(r["unlinked"] or 0) == 0:
                    bound_map[int(r["employee_id"])] = int(r["user_id"])
            for r in conn.execute(
                "SELECT employee_id, user_id FROM youli_session "
                "WHERE company_id = ? AND status = 'active'",
                (cid,),
            ).fetchall():
                bound_map.setdefault(int(r["employee_id"]), int(r["user_id"]))
            existing_emp_rows = conn.execute(
                "SELECT employee_id, user_id FROM erp_directory_employees WHERE company_id = ?",
                (cid,),
            ).fetchall()
            existing_user_by_emp = {
                int(r["employee_id"]): (int(r["user_id"]) if r["user_id"] is not None else None)
                for r in existing_emp_rows
            }

            # Mark omitted departments inactive, then replace active definitions.
            existing_dept_ids = {
                int(r["org_id"])
                for r in conn.execute(
                    "SELECT org_id FROM erp_departments WHERE company_id = ?", (cid,)
                ).fetchall()
            }
            for missing_org_id in existing_dept_ids - seen_org_ids:
                conn.execute(
                    "UPDATE erp_departments SET status = 0, synced_at = ? "
                    "WHERE company_id = ? AND org_id = ?",
                    (ts, cid, missing_org_id),
                )
            for org_id, parent_id, name, org_code, sort, status in dept_items:
                if org_id in existing_dept_ids:
                    conn.execute(
                        "UPDATE erp_departments SET parent_id = ?, name = ?, org_code = ?, "
                        "sort = ?, status = ?, synced_at = ? WHERE company_id = ? AND org_id = ?",
                        (parent_id, name, org_code, sort, status, ts, cid, org_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO erp_departments("
                        "company_id, org_id, parent_id, name, org_code, sort, status, synced_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (cid, org_id, parent_id, name, org_code, sort, status, ts),
                    )

            conn.execute("DELETE FROM erp_department_closure WHERE company_id = ?", (cid,))
            for c_row in closure_rows:
                conn.execute(
                    "INSERT INTO erp_department_closure("
                    "company_id, ancestor_org_id, descendant_org_id, depth"
                    ") VALUES (?, ?, ?, ?)",
                    c_row,
                )

            # Mark employees omitted from full sync as resigned (status = 2) and clear memberships.
            for missing_emp_id in set(existing_user_by_emp) - seen_emp_ids:
                conn.execute(
                    "UPDATE erp_directory_employees SET status = 2, synced_at = ? "
                    "WHERE company_id = ? AND employee_id = ?",
                    (ts, cid, missing_emp_id),
                )
                conn.execute(
                    "DELETE FROM erp_employee_departments WHERE company_id = ? AND employee_id = ?",
                    (cid, missing_emp_id),
                )
                uid = existing_user_by_emp.get(missing_emp_id) or bound_map.get(missing_emp_id)
                if uid is not None:
                    revoked_user_ids.add(uid)

            for (
                emp_id,
                acc_id,
                nick_name,
                phone,
                employee_no,
                status,
                is_admin,
                org_ids,
                admin_org_ids,
            ) in emp_items:
                linked_uid = bound_map.get(emp_id)
                if linked_uid is None:
                    linked_uid = existing_user_by_emp.get(emp_id)
                if emp_id in existing_user_by_emp:
                    conn.execute(
                        "UPDATE erp_directory_employees SET account_user_id = ?, user_id = ?, "
                        "nick_name = ?, phone = ?, employee_no = ?, status = ?, is_admin = ?, "
                        "synced_at = ? WHERE company_id = ? AND employee_id = ?",
                        (
                            acc_id,
                            linked_uid,
                            nick_name,
                            phone,
                            employee_no,
                            status,
                            is_admin,
                            ts,
                            cid,
                            emp_id,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO erp_directory_employees("
                        "company_id, employee_id, account_user_id, user_id, nick_name, phone, "
                        "employee_no, status, is_admin, synced_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            cid,
                            emp_id,
                            acc_id,
                            linked_uid,
                            nick_name,
                            phone,
                            employee_no,
                            status,
                            is_admin,
                            ts,
                        ),
                    )

                conn.execute(
                    "DELETE FROM erp_employee_departments WHERE company_id = ? AND employee_id = ?",
                    (cid, emp_id),
                )
                if status == 1:
                    for oid in org_ids:
                        conn.execute(
                            "INSERT INTO erp_employee_departments("
                            "company_id, employee_id, org_id, is_dept_admin, synced_at"
                            ") VALUES (?, ?, ?, ?, ?)",
                            (cid, emp_id, oid, 1 if oid in admin_org_ids else 0, ts),
                        )
                elif linked_uid is not None:
                    revoked_user_ids.add(linked_uid)

        return {
            "company_id": cid,
            "department_count": len(dept_items),
            "employee_count": len(emp_items),
            "active_employee_count": sum(1 for item in emp_items if item[5] == 1),
            "synced_at": ts,
            "revoked_user_ids": sorted(revoked_user_ids),
        }

    def sync_single_employee(
        self,
        company_id: int,
        employee: Any,
        *,
        user_id: int | None = None,
        synced_at: int | None = None,
    ) -> int:
        ts = now_ts() if synced_at is None else int(synced_at)
        cid = int(company_id)
        emp_id = int(_get_field(employee, "employee_id", _get_field(employee, "employeeId")))
        raw_acc = _get_field(employee, "account_user_id", _get_field(employee, "accountUserId"))
        acc_id = int(raw_acc) if raw_acc is not None else None
        nick_name = str(_get_field(employee, "nick_name", _get_field(employee, "nickName", "")) or "")
        phone = str(_get_field(employee, "phone", "") or "")
        employee_no = str(
            _get_field(employee, "employee_no", _get_field(employee, "employeeNo", "")) or ""
        )
        raw_status = _get_field(employee, "status", 1)
        status = int(raw_status) if raw_status is not None else 1
        raw_admin = _get_field(employee, "is_admin", _get_field(employee, "isAdmin", 0))
        is_admin = 1 if int(raw_admin or 0) == 1 else 0
        raw_orgs = _get_field(employee, "org_ids", _get_field(employee, "orgIds")) or []
        org_ids: list[int] = []
        seen_orgs: set[int] = set()
        for raw_o in raw_orgs:
            oid = int(raw_o)
            if oid > 0 and oid not in seen_orgs:
                seen_orgs.add(oid)
                org_ids.append(oid)
        raw_admin_orgs = (
            _get_field(employee, "admin_org_ids", _get_field(employee, "adminOrgIds")) or []
        )
        admin_org_ids = {int(o) for o in raw_admin_orgs if int(o) > 0}

        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT user_id FROM erp_directory_employees WHERE company_id = ? AND employee_id = ?",
                (cid, emp_id),
            ).fetchone()
            resolved_uid = user_id if user_id is not None else (
                int(existing["user_id"]) if existing and existing["user_id"] is not None else None
            )
            if resolved_uid is not None:
                conn.execute(
                    "UPDATE erp_directory_employees SET user_id = NULL "
                    "WHERE user_id = ? AND NOT (company_id = ? AND employee_id = ?)",
                    (resolved_uid, cid, emp_id),
                )
            if existing is not None:
                conn.execute(
                    "UPDATE erp_directory_employees SET account_user_id = ?, user_id = ?, "
                    "nick_name = ?, phone = ?, employee_no = ?, status = ?, is_admin = ?, "
                    "synced_at = ? WHERE company_id = ? AND employee_id = ?",
                    (
                        acc_id,
                        resolved_uid,
                        nick_name,
                        phone,
                        employee_no,
                        status,
                        is_admin,
                        ts,
                        cid,
                        emp_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO erp_directory_employees("
                    "company_id, employee_id, account_user_id, user_id, nick_name, phone, "
                    "employee_no, status, is_admin, synced_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        emp_id,
                        acc_id,
                        resolved_uid,
                        nick_name,
                        phone,
                        employee_no,
                        status,
                        is_admin,
                        ts,
                    ),
                )
            conn.execute(
                "DELETE FROM erp_employee_departments WHERE company_id = ? AND employee_id = ?",
                (cid, emp_id),
            )
            if status == 1:
                for oid in org_ids:
                    conn.execute(
                        "INSERT INTO erp_employee_departments("
                        "company_id, employee_id, org_id, is_dept_admin, synced_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (cid, emp_id, oid, 1 if oid in admin_org_ids else 0, ts),
                    )
        return status

    def link_user(self, company_id: int, employee_id: int, user_id: int) -> None:
        cid = int(company_id)
        emp_id = int(employee_id)
        uid = int(user_id)
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE erp_directory_employees SET user_id = NULL "
                "WHERE user_id = ? AND NOT (company_id = ? AND employee_id = ?)",
                (uid, cid, emp_id),
            )
            conn.execute(
                "UPDATE erp_directory_employees SET user_id = ? "
                "WHERE company_id = ? AND employee_id = ?",
                (uid, cid, emp_id),
            )

    def unlink_user(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE erp_directory_employees SET user_id = NULL WHERE user_id = ?",
                (int(user_id),),
            )

    def list_departments(
        self,
        company_id: int | None = None,
        *,
        include_inactive: bool = False,
    ) -> list[ErpDepartmentRow]:
        sql = "SELECT * FROM erp_departments WHERE 1=1"
        params: list[object] = []
        if company_id is not None:
            sql += " AND company_id = ?"
            params.append(int(company_id))
        if not include_inactive:
            sql += " AND status = 1"
        sql += " ORDER BY company_id ASC, sort ASC, org_id ASC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return map_rows(rows, ErpDepartmentRow)

    def get_department(self, company_id: int, org_id: int) -> ErpDepartmentRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM erp_departments WHERE company_id = ? AND org_id = ?",
                (int(company_id), int(org_id)),
            ).fetchone()
        return ErpDepartmentRow.from_row(row) if row else None

    def get_employee(self, company_id: int, employee_id: int) -> ErpDirectoryEmployeeRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM erp_directory_employees WHERE company_id = ? AND employee_id = ?",
                (int(company_id), int(employee_id)),
            ).fetchone()
        return ErpDirectoryEmployeeRow.from_row(row) if row else None

    def list_employees(
        self,
        *,
        company_id: int | None = None,
        org_id: int | None = None,
        include_children: bool = True,
        query: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT DISTINCT e.* FROM erp_directory_employees e"
        params: list[object] = []
        if org_id is not None:
            if include_children:
                sql += (
                    " JOIN erp_employee_departments ed"
                    "   ON ed.company_id = e.company_id AND ed.employee_id = e.employee_id"
                    " JOIN erp_department_closure c"
                    "   ON c.company_id = ed.company_id AND c.descendant_org_id = ed.org_id"
                )
            else:
                sql += (
                    " JOIN erp_employee_departments ed"
                    "   ON ed.company_id = e.company_id AND ed.employee_id = e.employee_id"
                )
        sql += " WHERE 1=1"
        if company_id is not None:
            sql += " AND e.company_id = ?"
            params.append(int(company_id))
        if active_only:
            sql += " AND e.status = 1"
        if org_id is not None:
            if include_children:
                sql += " AND c.ancestor_org_id = ?"
            else:
                sql += " AND ed.org_id = ?"
            params.append(int(org_id))
        q = (query or "").strip().lower()
        if q:
            like = f"%{q}%"
            sql += (
                " AND (LOWER(e.nick_name) LIKE ? OR LOWER(e.phone) LIKE ?"
                " OR LOWER(e.employee_no) LIKE ?)"
            )
            params.extend([like, like, like])
        sql += " ORDER BY e.company_id ASC, e.is_admin DESC, e.employee_id ASC"
        with self._db.connect() as conn:
            emp_rows = map_rows(conn.execute(sql, params).fetchall(), ErpDirectoryEmployeeRow)
            if not emp_rows:
                return []
            dept_names: dict[tuple[int, int], str] = {}
            for d in conn.execute(
                "SELECT company_id, org_id, name FROM erp_departments WHERE status = 1"
            ).fetchall():
                dept_names[(int(d["company_id"]), int(d["org_id"]))] = str(d["name"] or "")
            emp_depts: dict[tuple[int, int], list[tuple[int, bool]]] = {}
            for m in conn.execute(
                "SELECT company_id, employee_id, org_id, is_dept_admin "
                "FROM erp_employee_departments ORDER BY org_id ASC"
            ).fetchall():
                emp_depts.setdefault((int(m["company_id"]), int(m["employee_id"])), []).append(
                    (int(m["org_id"]), int(m["is_dept_admin"] or 0) == 1)
                )
        out: list[dict[str, Any]] = []
        for emp in emp_rows:
            memberships = emp_depts.get((emp.company_id, emp.employee_id), [])
            org_ids = [oid for oid, _ in memberships]
            admin_org_ids = [oid for oid, is_mgr in memberships if is_mgr]
            org_names = [
                dept_names[(emp.company_id, oid)]
                for oid in org_ids
                if (emp.company_id, oid) in dept_names
            ]
            out.append(
                {
                    "company_id": emp.company_id,
                    "employee_id": emp.employee_id,
                    "account_user_id": emp.account_user_id,
                    "user_id": emp.user_id,
                    "has_octop_account": emp.user_id is not None,
                    "nick_name": emp.nick_name,
                    "phone": emp.phone,
                    "employee_no": emp.employee_no,
                    "status": emp.status,
                    "is_admin": bool(emp.is_admin),
                    "org_ids": org_ids,
                    "admin_org_ids": admin_org_ids,
                    "org_names": org_names,
                    "synced_at": emp.synced_at,
                }
            )
        return out

    def known_company_ids(self) -> list[int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT company_id FROM ("
                "  SELECT company_id FROM erp_departments"
                "  UNION SELECT company_id FROM erp_directory_employees"
                "  UNION SELECT company_id FROM youli_session WHERE status = 'active'"
                "  UNION SELECT company_id FROM youli_identity_binding WHERE unlinked = 0"
                ") t WHERE company_id > 0 ORDER BY company_id ASC"
            ).fetchall()
            sso_rows = conn.execute(
                "SELECT i.subject FROM user_sso_identities i "
                "JOIN sso_providers p ON p.id = i.provider_id "
                "WHERE p.kind = 'youli' AND i.subject LIKE 'youli:%' "
                "UNION "
                "SELECT u.sso_subject FROM users u "
                "JOIN sso_providers p ON p.id = u.sso_provider_id "
                "WHERE p.kind = 'youli' AND u.sso_subject LIKE 'youli:%'"
            ).fetchall()
        ids = {int(r[0]) for r in rows}
        for r in sso_rows:
            parts = str(r[0] or "").split(":")
            if len(parts) == 3 and parts[0] == "youli" and parts[1].isdigit():
                cid = int(parts[1])
                if cid > 0:
                    ids.add(cid)
        return sorted(ids)

    def get_sync_summary(self) -> dict[str, Any]:
        with self._db.connect() as conn:
            dept_count = int(
                conn.execute("SELECT COUNT(*) FROM erp_departments WHERE status = 1").fetchone()[0]
            )
            emp_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM erp_directory_employees WHERE status = 1"
                ).fetchone()[0]
            )
            last_synced = conn.execute(
                "SELECT MAX(synced_at) FROM ("
                "  SELECT MAX(synced_at) AS synced_at FROM erp_departments"
                "  UNION ALL SELECT MAX(synced_at) AS synced_at FROM erp_directory_employees"
                ") t"
            ).fetchone()[0]
        return {
            "company_ids": self.known_company_ids(),
            "department_count": dept_count,
            "employee_count": emp_count,
            "last_synced_at": int(last_synced) if last_synced else None,
        }

    def get_user_erp_context(self, user_id: int) -> UserErpContext | None:
        uid = int(user_id)
        with self._db.connect() as conn:
            company_id: int | None = None
            employee_id: int | None = None

            binding = conn.execute(
                "SELECT company_id, employee_id, unlinked FROM youli_identity_binding "
                "WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if binding is not None:
                if int(binding["unlinked"] or 0) == 0:
                    company_id = int(binding["company_id"])
                    employee_id = int(binding["employee_id"])
                else:
                    # Quarantined/unlinked binding: require an active session if any.
                    sess = conn.execute(
                        "SELECT company_id, employee_id FROM youli_session "
                        "WHERE user_id = ? AND status = 'active' "
                        "ORDER BY created_at DESC, id DESC LIMIT 1",
                        (uid,),
                    ).fetchone()
                    if sess is None:
                        return None
                    company_id = int(sess["company_id"])
                    employee_id = int(sess["employee_id"])

            if company_id is None or employee_id is None:
                sess = conn.execute(
                    "SELECT company_id, employee_id FROM youli_session "
                    "WHERE user_id = ? AND status = 'active' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                if sess is not None:
                    company_id = int(sess["company_id"])
                    employee_id = int(sess["employee_id"])

            if company_id is None or employee_id is None:
                emp_link = conn.execute(
                    "SELECT company_id, employee_id FROM erp_directory_employees "
                    "WHERE user_id = ? ORDER BY synced_at DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                if emp_link is not None:
                    company_id = int(emp_link["company_id"])
                    employee_id = int(emp_link["employee_id"])

            if company_id is None or employee_id is None:
                sso_row = conn.execute(
                    "SELECT u.sso_subject FROM users u "
                    "JOIN sso_providers p ON p.id = u.sso_provider_id "
                    "WHERE u.id = ? AND p.kind = 'youli' AND u.sso_subject IS NOT NULL",
                    (uid,),
                ).fetchone()
                subject = str(sso_row["sso_subject"]) if sso_row else ""
                if not subject:
                    ident_row = conn.execute(
                        "SELECT i.subject FROM user_sso_identities i "
                        "JOIN sso_providers p ON p.id = i.provider_id "
                        "WHERE i.user_id = ? AND p.kind = 'youli' LIMIT 1",
                        (uid,),
                    ).fetchone()
                    subject = str(ident_row["subject"]) if ident_row else ""
                parts = subject.split(":")
                if len(parts) == 3 and parts[0] == "youli" and all(p.isdigit() for p in parts[1:]):
                    company_id = int(parts[1])
                    employee_id = int(parts[2])

            if company_id is None or employee_id is None:
                return None

            emp_row = conn.execute(
                "SELECT status FROM erp_directory_employees "
                "WHERE company_id = ? AND employee_id = ?",
                (company_id, employee_id),
            ).fetchone()
            if emp_row is not None and int(emp_row["status"] if emp_row["status"] is not None else 1) != 1:
                return UserErpContext(
                    user_id=uid,
                    company_id=company_id,
                    employee_id=employee_id,
                    active_in_erp=False,
                    direct_org_ids=frozenset(),
                    ancestor_org_ids=frozenset(),
                    admin_org_ids=frozenset(),
                )

            dept_rows = conn.execute(
                "SELECT ed.org_id, ed.is_dept_admin "
                "FROM erp_employee_departments ed "
                "JOIN erp_departments d ON d.company_id = ed.company_id AND d.org_id = ed.org_id "
                "WHERE ed.company_id = ? AND ed.employee_id = ? AND d.status = 1",
                (company_id, employee_id),
            ).fetchall()
            direct_org_ids = frozenset(int(r["org_id"]) for r in dept_rows)
            admin_org_ids = frozenset(
                int(r["org_id"]) for r in dept_rows if int(r["is_dept_admin"] or 0) == 1
            )
            ancestor_org_ids = direct_org_ids
            if direct_org_ids:
                placeholders = sql_in_placeholders(len(direct_org_ids))
                closure_rows = conn.execute(
                    "SELECT DISTINCT c.ancestor_org_id "
                    "FROM erp_department_closure c "
                    "JOIN erp_departments d "
                    "  ON d.company_id = c.company_id AND d.org_id = c.ancestor_org_id "
                    f"WHERE c.company_id = ? AND d.status = 1 AND c.descendant_org_id IN ({placeholders})",
                    (company_id, *sorted(direct_org_ids)),
                ).fetchall()
                ancestor_org_ids = direct_org_ids | frozenset(
                    int(r["ancestor_org_id"]) for r in closure_rows
                )
            return UserErpContext(
                user_id=uid,
                company_id=company_id,
                employee_id=employee_id,
                active_in_erp=True,
                direct_org_ids=direct_org_ids,
                ancestor_org_ids=ancestor_org_ids,
                admin_org_ids=admin_org_ids,
            )
