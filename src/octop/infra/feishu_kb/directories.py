"""Read-only Feishu directory discovery for explicitly selected import roots."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Literal

import httpx

from octop.infra.feishu_kb.client import _OPEN, FeishuClient, FeishuError, _data

DirectoryKind = Literal["wiki_space", "wiki_node", "drive_folder"]
DIRECTORY_SCOPES = (
    "docx:document:readonly",
    "wiki:wiki:readonly",
    "drive:drive.metadata:readonly",
    "offline_access",
)
MAX_SCAN_ENTRIES = 200
MAX_SCAN_PAGES = 40
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FeishuError("invalid_directory_id")
    return value


@dataclass(frozen=True)
class DirectoryEntry:
    id: str
    title: str
    type: str
    document_id: str | None = None
    has_children: bool = False


@dataclass(frozen=True)
class DirectoryPage:
    items: list[DirectoryEntry]
    next_page: str | None


@dataclass(frozen=True)
class DirectoryDocument:
    id: str
    title: str
    path: tuple[str, ...]


class FeishuDirectoryClient(FeishuClient):
    def _page(
        self, path: str, access_token: str, page: str | None, **params: str
    ) -> tuple[list[dict[str, object]], str | None]:
        query = {"page_size": "50", **params}
        if page is not None:
            if not page or len(page) > 2048:
                raise FeishuError("invalid_page_token")
            query["page_token"] = page
        try:
            response = self._http.get(
                f"{_OPEN}{path}", params=query, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.RequestError:
            raise FeishuError("unavailable") from None
        if response.status_code in (401, 403):
            raise FeishuError("forbidden")
        if response.status_code == 404:
            raise FeishuError("missing")
        if not response.is_success:
            raise FeishuError("unavailable")
        data = _data(response)
        items = data.get("items", data.get("files"))
        if (
            not isinstance(items, list)
            or len(items) > 50
            or any(not isinstance(item, dict) for item in items)
        ):
            raise FeishuError("invalid_response")
        next_page = data.get("page_token") or data.get("next_page_token")
        has_more = data.get("has_more", bool(next_page))
        if not isinstance(has_more, bool) or (
            has_more and (not isinstance(next_page, str) or not next_page or len(next_page) > 2048)
        ):
            raise FeishuError("invalid_response")
        return items, next_page if has_more and isinstance(next_page, str) else None

    def wiki_spaces(self, access_token: str, page: str | None = None) -> DirectoryPage:
        items, next_page = self._page("/open-apis/wiki/v2/spaces", access_token, page)
        return DirectoryPage(
            [
                DirectoryEntry(
                    identifier(row.get("space_id")),
                    _title(row.get("name")),
                    "space",
                    has_children=True,
                )
                for row in items
            ],
            next_page,
        )

    def wiki_nodes(
        self, access_token: str, space: str, parent: str | None = None, page: str | None = None
    ) -> DirectoryPage:
        params = {"parent_node_token": identifier(parent)} if parent else {}
        items, next_page = self._page(
            f"/open-apis/wiki/v2/spaces/{identifier(space)}/nodes", access_token, page, **params
        )
        return DirectoryPage(
            [
                DirectoryEntry(
                    identifier(row.get("node_token")),
                    _title(row.get("title")),
                    str(row.get("obj_type") or "unknown"),
                    identifier(row.get("obj_token")) if row.get("obj_type") == "docx" else None,
                    row.get("has_child") is True and row.get("node_type") != "shortcut",
                )
                for row in items
                if row.get("node_type") != "shortcut"
            ],
            next_page,
        )

    def drive_files(
        self, access_token: str, folder: str | None = None, page: str | None = None
    ) -> DirectoryPage:
        params = {"folder_token": identifier(folder)} if folder else {}
        items, next_page = self._page("/open-apis/drive/v1/files", access_token, page, **params)
        return DirectoryPage(
            [
                DirectoryEntry(
                    identifier(row.get("token")),
                    _title(row.get("name")),
                    str(row.get("type") or "unknown"),
                    identifier(row.get("token")) if row.get("type") == "docx" else None,
                    row.get("type") == "folder",
                )
                for row in items
            ],
            next_page,
        )

    def discover(
        self,
        access_token: str,
        kind: DirectoryKind,
        root: str,
        space: str | None,
        include_children: bool,
    ) -> tuple[list[DirectoryDocument], int]:
        """Enumerate a complete bounded snapshot or fail without publishing it."""
        identifier(root)
        if kind == "wiki_node":
            identifier(space)
        start = None if kind == "wiki_space" else root
        queue: deque[tuple[str | None, tuple[str, ...]]] = deque([(start, ())])
        visited = {start}
        documents: dict[str, DirectoryDocument] = {}
        skipped = 0
        pages = 0
        seen = 0
        while queue:
            parent, path = queue.popleft()
            token = None
            page_tokens: set[str] = set()
            while True:
                pages += 1
                if pages > MAX_SCAN_PAGES:
                    raise FeishuError("directory_too_large")
                if kind == "drive_folder":
                    result = self.drive_files(access_token, parent, token)
                else:
                    result = self.wiki_nodes(
                        access_token,
                        root if kind == "wiki_space" else identifier(space),
                        parent,
                        token,
                    )
                seen += len(result.items)
                if seen > MAX_SCAN_ENTRIES:
                    raise FeishuError("directory_too_large")
                for entry in result.items:
                    if entry.document_id:
                        documents.setdefault(
                            entry.document_id,
                            DirectoryDocument(entry.document_id, entry.title, path),
                        )
                    elif entry.type != "folder":
                        skipped += 1
                    if include_children and entry.has_children and entry.id not in visited:
                        visited.add(entry.id)
                        queue.append((entry.id, (*path, entry.title)))
                if result.next_page is None:
                    break
                if result.next_page in page_tokens:
                    raise FeishuError("invalid_response")
                token = result.next_page
                page_tokens.add(token)
        return list(documents.values()), skipped


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeishuError("invalid_response")
    return value.strip()[:255]
