"""Thumbs up / down on assistant messages and the admin quality summary."""

from __future__ import annotations

import time
from typing import Any

from octop.infra.errors import ErrorCode, OctopError

MAX_COMMENT = 500
MAX_EXCERPT = 300
_DAY = 86_400


def record_feedback(
    repos: Any,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    user_id: int,
    rating: int,
    comment: str = "",
    excerpt: str = "",
) -> None:
    """Store one vote. ``rating`` 0 clears the user's vote on that message."""
    if not thread_id or not message_id:
        raise OctopError(ErrorCode.FORBIDDEN, "thread and message are required", status=400)
    if rating == 0:
        repos.message_feedback_repo.delete(
            agent_id=agent_id, thread_id=thread_id, message_id=message_id, user_id=user_id
        )
        return
    if rating not in (1, -1):
        raise OctopError(ErrorCode.FORBIDDEN, "rating must be 1, -1, or 0", status=400)
    repos.message_feedback_repo.upsert(
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        user_id=user_id,
        rating=rating,
        comment=comment.strip()[:MAX_COMMENT],
        excerpt=excerpt.strip()[:MAX_EXCERPT],
    )


def quality_summary(repos: Any, *, days: int = 30) -> dict[str, Any]:
    """Votes per expert and the latest thumbs-down with comments."""
    since = int(time.time()) - max(1, days) * _DAY
    agent_names = {row.agent_id: row.name for row in repos.agent_repo.list_all()}
    usernames = {int(u.id): u.username for u in repos.user_repo.list(include_disabled=True)}
    per_agent = []
    up_total = down_total = 0
    for row in repos.message_feedback_repo.counts_by_agent(since=since):
        votes = row["up"] + row["down"]
        up_total += row["up"]
        down_total += row["down"]
        per_agent.append(
            {
                **row,
                "agent_name": agent_names.get(row["agent_id"], row["agent_id"]),
                "satisfaction": round(row["up"] / votes, 4) if votes else None,
            }
        )
    recent_down = [
        {
            "agent_id": fb.agent_id,
            "agent_name": agent_names.get(fb.agent_id, fb.agent_id),
            "thread_id": fb.thread_id,
            "message_id": fb.message_id,
            "username": usernames.get(fb.user_id, str(fb.user_id)),
            "comment": fb.comment,
            "excerpt": fb.excerpt,
            "updated_at": fb.updated_at,
        }
        for fb in repos.message_feedback_repo.recent(since=since, rating=-1, limit=50)
    ]
    votes_total = up_total + down_total
    return {
        "days": days,
        "up": up_total,
        "down": down_total,
        "satisfaction": round(up_total / votes_total, 4) if votes_total else None,
        "per_agent": sorted(per_agent, key=lambda item: -(item["up"] + item["down"])),
        "recent_down": recent_down,
    }


__all__ = ["MAX_COMMENT", "MAX_EXCERPT", "quality_summary", "record_feedback"]
