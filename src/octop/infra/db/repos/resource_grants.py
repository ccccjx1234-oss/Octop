"""Unified resource scope policy and multi-subject grants repository."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


@dataclass(frozen=True)
class ResourceScopePolicyRow:
    resource_type: str
    resource_key: str
    company_id: int
    visibility_mode: str
    updated_by: int | None
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ResourceScopePolicyRow:
        return cls(
            resource_type=str(r["resource_type"]),
            resource_key=str(r["resource_key"]),
            company_id=int(r["company_id"] or 0),
            visibility_mode=str(r["visibility_mode"]),
            updated_by=int(r["updated_by"]) if r["updated_by"] is not None else None,
            updated_at=int(r["updated_at"] or 0),
        )


@dataclass(frozen=True)
class ResourceSubjectGrantRow:
    id: int
    company_id: int
    resource_type: str
    resource_key: str
    subject_type: str
    subject_id: str
    include_children: bool
    effect: str
    permission_level: str
    granted_by: int | None
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ResourceSubjectGrantRow:
        return cls(
            id=int(r["id"]),
            company_id=int(r["company_id"] or 0),
            resource_type=str(r["resource_type"]),
            resource_key=str(r["resource_key"]),
            subject_type=str(r["subject_type"]),
            subject_id=str(r["subject_id"]),
            include_children=int(r["include_children"] if r["include_children"] is not None else 1) == 1,
            effect=str(r["effect"] or "allow"),
            permission_level=str(r["permission_level"] or "use"),
            granted_by=int(r["granted_by"]) if r["granted_by"] is not None else None,
            created_at=int(r["created_at"] or 0),
        )


class ResourceGrantRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get_policy(
        self, resource_type: str, resource_key: str
    ) -> ResourceScopePolicyRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resource_scope_policy "
                "WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            ).fetchone()
        return ResourceScopePolicyRow.from_row(row) if row else None

    def list_policies_by_type(
        self, resource_type: str
    ) -> dict[str, ResourceScopePolicyRow]:
        with self._db.connect() as conn:
            rows = map_rows(
                conn.execute(
                    "SELECT * FROM resource_scope_policy WHERE resource_type = ?",
                    (resource_type,),
                ).fetchall(),
                ResourceScopePolicyRow,
            )
        return {row.resource_key: row for row in rows}

    def list_grants(
        self, resource_type: str, resource_key: str
    ) -> list[ResourceSubjectGrantRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_subject_grant "
                "WHERE resource_type = ? AND resource_key = ? "
                "ORDER BY id ASC",
                (resource_type, resource_key),
            ).fetchall()
        return map_rows(rows, ResourceSubjectGrantRow)

    def list_grants_by_type(
        self, resource_type: str
    ) -> dict[str, list[ResourceSubjectGrantRow]]:
        with self._db.connect() as conn:
            rows = map_rows(
                conn.execute(
                    "SELECT * FROM resource_subject_grant WHERE resource_type = ? ORDER BY id ASC",
                    (resource_type,),
                ).fetchall(),
                ResourceSubjectGrantRow,
            )
        out: dict[str, list[ResourceSubjectGrantRow]] = {}
        for row in rows:
            out.setdefault(row.resource_key, []).append(row)
        return out

    def set_policy(
        self,
        resource_type: str,
        resource_key: str,
        *,
        company_id: int = 0,
        visibility_mode: str,
        updated_by: int | None = None,
    ) -> None:
        ts = now_ts()
        with self._db.transaction() as conn:
            self._upsert_policy(
                conn,
                resource_type=resource_type,
                resource_key=resource_key,
                company_id=company_id,
                visibility_mode=visibility_mode,
                updated_by=updated_by,
                ts=ts,
            )

    def delete_policy(self, resource_type: str, resource_key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM resource_scope_policy WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            )

    def replace_grants(
        self,
        resource_type: str,
        resource_key: str,
        *,
        company_id: int = 0,
        visibility_mode: str,
        grants: Sequence[dict[str, Any]],
        updated_by: int | None = None,
        sync_legacy_group_grants: bool = True,
    ) -> None:
        ts = now_ts()
        cid = int(company_id or 0)
        deduped: list[tuple[str, str, int, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in grants:
            subject_type = str(item["subject_type"])
            subject_id = str(item["subject_id"])
            effect = str(item.get("effect") or "allow")
            include_children = 1 if bool(item.get("include_children", True)) else 0
            permission_level = str(item.get("permission_level") or "use")
            key = (subject_type, subject_id, effect)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((subject_type, subject_id, include_children, effect, permission_level))

        with self._db.transaction() as conn:
            self._upsert_policy(
                conn,
                resource_type=resource_type,
                resource_key=resource_key,
                company_id=cid,
                visibility_mode=visibility_mode,
                updated_by=updated_by,
                ts=ts,
            )
            conn.execute(
                "DELETE FROM resource_subject_grant WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            )
            for subject_type, subject_id, include_children, effect, permission_level in deduped:
                conn.execute(
                    "INSERT INTO resource_subject_grant("
                    "company_id, resource_type, resource_key, subject_type, subject_id, "
                    "include_children, effect, permission_level, granted_by, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        resource_type,
                        resource_key,
                        subject_type,
                        subject_id,
                        include_children,
                        effect,
                        permission_level,
                        updated_by,
                        ts,
                    ),
                )
            if sync_legacy_group_grants:
                conn.execute(
                    "DELETE FROM resource_group_grant WHERE resource_type = ? AND resource_key = ?",
                    (resource_type, resource_key),
                )
                if visibility_mode in {"groups", "scoped"}:
                    for subject_type, subject_id, _inc, effect, _perm in deduped:
                        if subject_type == "user_group" and effect == "allow":
                            conn.execute(
                                "INSERT INTO resource_group_grant("
                                "resource_type, resource_key, user_group_id"
                                ") VALUES (?, ?, ?)",
                                (resource_type, resource_key, subject_id),
                            )

    def append_allow_grants(
        self,
        resource_type: str,
        resource_key: str,
        *,
        company_id: int = 0,
        grants: Sequence[dict[str, Any]],
        granted_by: int | None = None,
    ) -> int:
        """Append allow grants to a scoped/groups resource without dropping existing rules."""
        ts = now_ts()
        cid = int(company_id or 0)
        added = 0
        with self._db.transaction() as conn:
            policy = conn.execute(
                "SELECT visibility_mode, company_id FROM resource_scope_policy "
                "WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            ).fetchone()
            legacy_groups = conn.execute(
                "SELECT user_group_id FROM resource_group_grant "
                "WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            ).fetchall()
            if policy is None:
                # Preserve existing group grants when upgrading a group-restricted resource to scoped.
                eff_mode = "scoped"
                self._upsert_policy(
                    conn,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    company_id=cid,
                    visibility_mode=eff_mode,
                    updated_by=granted_by,
                    ts=ts,
                )
                for row in legacy_groups:
                    gid = str(row["user_group_id"])
                    exists = conn.execute(
                        "SELECT 1 FROM resource_subject_grant "
                        "WHERE resource_type = ? AND resource_key = ? "
                        "AND subject_type = 'user_group' AND subject_id = ? AND effect = 'allow'",
                        (resource_type, resource_key, gid),
                    ).fetchone()
                    if exists is None:
                        conn.execute(
                            "INSERT INTO resource_subject_grant("
                            "company_id, resource_type, resource_key, subject_type, subject_id, "
                            "include_children, effect, permission_level, granted_by, created_at"
                            ") VALUES (?, ?, ?, 'user_group', ?, 1, 'allow', 'use', ?, ?)",
                            (cid, resource_type, resource_key, gid, granted_by, ts),
                        )
            elif str(policy["visibility_mode"]) == "all":
                return 0
            elif str(policy["visibility_mode"]) == "owner":
                self._upsert_policy(
                    conn,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    company_id=cid,
                    visibility_mode="scoped",
                    updated_by=granted_by,
                    ts=ts,
                )
            elif str(policy["visibility_mode"]) == "groups":
                self._upsert_policy(
                    conn,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    company_id=cid or int(policy["company_id"] or 0),
                    visibility_mode="scoped",
                    updated_by=granted_by,
                    ts=ts,
                )
            elif str(policy["visibility_mode"]) == "company":
                if cid > 0 and int(policy["company_id"] or 0) in {0, cid}:
                    return 0
                if int(policy["company_id"] or 0) == 0:
                    return 0
                self._upsert_policy(
                    conn,
                    resource_type=resource_type,
                    resource_key=resource_key,
                    company_id=0,
                    visibility_mode="scoped",
                    updated_by=granted_by,
                    ts=ts,
                )

            for item in grants:
                subject_type = str(item["subject_type"])
                subject_id = str(item["subject_id"])
                include_children = 1 if bool(item.get("include_children", True)) else 0
                permission_level = str(item.get("permission_level") or "use")
                exists = conn.execute(
                    "SELECT id, include_children FROM resource_subject_grant "
                    "WHERE resource_type = ? AND resource_key = ? "
                    "AND subject_type = ? AND subject_id = ? AND effect = 'allow'",
                    (resource_type, resource_key, subject_type, subject_id),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO resource_subject_grant("
                        "company_id, resource_type, resource_key, subject_type, subject_id, "
                        "include_children, effect, permission_level, granted_by, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'allow', ?, ?, ?)",
                        (
                            cid,
                            resource_type,
                            resource_key,
                            subject_type,
                            subject_id,
                            include_children,
                            permission_level,
                            granted_by,
                            ts,
                        ),
                    )
                    added += 1
                    if subject_type == "user_group":
                        legacy_exists = conn.execute(
                            "SELECT 1 FROM resource_group_grant "
                            "WHERE resource_type = ? AND resource_key = ? AND user_group_id = ?",
                            (resource_type, resource_key, subject_id),
                        ).fetchone()
                        if legacy_exists is None:
                            conn.execute(
                                "INSERT INTO resource_group_grant("
                                "resource_type, resource_key, user_group_id"
                                ") VALUES (?, ?, ?)",
                                (resource_type, resource_key, subject_id),
                            )
                elif include_children == 1 and int(exists["include_children"] or 0) == 0:
                    conn.execute(
                        "UPDATE resource_subject_grant SET include_children = 1 WHERE id = ?",
                        (int(exists["id"]),),
                    )
                    added += 1
        return added

    def delete_resource(self, resource_type: str, resource_key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM resource_subject_grant WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            )
            conn.execute(
                "DELETE FROM resource_scope_policy WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            )

    def rename_key(self, resource_type: str, old: str, new: str) -> None:
        if old == new:
            return
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE resource_subject_grant SET resource_key = ? "
                "WHERE resource_type = ? AND resource_key = ?",
                (new, resource_type, old),
            )
            conn.execute(
                "UPDATE resource_scope_policy SET resource_key = ? "
                "WHERE resource_type = ? AND resource_key = ?",
                (new, resource_type, old),
            )

    def rewrite_prefix(self, resource_type: str, old_prefix: str, new_prefix: str) -> None:
        if old_prefix == new_prefix:
            return
        needle = f"{old_prefix}/"
        with self._db.connect() as conn:
            grant_rows = conn.execute(
                "SELECT DISTINCT resource_key FROM resource_subject_grant WHERE resource_type = ?",
                (resource_type,),
            ).fetchall()
            policy_rows = conn.execute(
                "SELECT DISTINCT resource_key FROM resource_scope_policy WHERE resource_type = ?",
                (resource_type,),
            ).fetchall()
        all_keys = {str(r["resource_key"]) for r in [*grant_rows, *policy_rows]}
        updates: list[tuple[str, str]] = []
        for key in all_keys:
            if key == old_prefix:
                updates.append((new_prefix, key))
            elif key.startswith(needle):
                updates.append((new_prefix + key[len(old_prefix) :], key))
        if not updates:
            return
        with self._db.transaction() as conn:
            for new_key, old_key in updates:
                conn.execute(
                    "UPDATE resource_subject_grant SET resource_key = ? "
                    "WHERE resource_type = ? AND resource_key = ?",
                    (new_key, resource_type, old_key),
                )
                conn.execute(
                    "UPDATE resource_scope_policy SET resource_key = ? "
                    "WHERE resource_type = ? AND resource_key = ?",
                    (new_key, resource_type, old_key),
                )

    def delete_prefix(self, resource_type: str, prefix: str) -> None:
        needle = f"{prefix}/%"
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM resource_subject_grant "
                "WHERE resource_type = ? AND (resource_key = ? OR resource_key LIKE ?)",
                (resource_type, prefix, needle),
            )
            conn.execute(
                "DELETE FROM resource_scope_policy "
                "WHERE resource_type = ? AND (resource_key = ? OR resource_key LIKE ?)",
                (resource_type, prefix, needle),
            )

    @staticmethod
    def _upsert_policy(
        conn: Any,
        *,
        resource_type: str,
        resource_key: str,
        company_id: int,
        visibility_mode: str,
        updated_by: int | None,
        ts: int,
    ) -> None:
        existing = conn.execute(
            "SELECT 1 FROM resource_scope_policy WHERE resource_type = ? AND resource_key = ?",
            (resource_type, resource_key),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE resource_scope_policy SET company_id = ?, visibility_mode = ?, "
                "updated_by = ?, updated_at = ? WHERE resource_type = ? AND resource_key = ?",
                (int(company_id), visibility_mode, updated_by, ts, resource_type, resource_key),
            )
        else:
            conn.execute(
                "INSERT INTO resource_scope_policy("
                "resource_type, resource_key, company_id, visibility_mode, updated_by, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (resource_type, resource_key, int(company_id), visibility_mode, updated_by, ts),
            )
