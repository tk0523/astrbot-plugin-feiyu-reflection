"""Read-only reflection page API for feiyu_state."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_feiyu_reflection"
MASTER_ID = "3282993487"


class ReflectionPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _master(self) -> dict:
        return self.plugin.data.get("users", {}).get(MASTER_ID, {}) or {}

    async def get_overview(self) -> dict:
        master = self._master()
        reflection = master.get("daily_reflection") or {}
        history = [
            item
            for item in (master.get("reflection_history") or [])
            if isinstance(item, dict)
        ][-7:]
        sessions = []
        for umo, session in (self.plugin.data.get("sessions") or {}).items():
            if not isinstance(session, dict):
                continue
            if f":{MASTER_ID}" not in str(umo):
                continue
            sessions.append(
                {
                    "umo": str(umo),
                    "scope": "group" if "GroupMessage" in str(umo) else "private",
                    "mood": session.get("mood"),
                    "energy": session.get("energy"),
                    "affection": session.get("affection"),
                }
            )
        return {
            "ok": True,
            "reflection": reflection,
            "history": history,
            "profile": {
                "nick": master.get("nick") or "牛来",
                "impression": master.get("impression") or "",
                "traits": master.get("traits") or [],
                "topics": master.get("topics") or [],
            },
            "sessions": sessions,
            "effect": {
                "private_prompt": bool(reflection.get("self")),
                "group_prompt": bool(reflection.get("self_group")),
                "friend_private_only": True,
                "schedule_source": "feiyu_state reflection prompt",
            },
            "generated_at": reflection.get("generated_at"),
            "source_chars": reflection.get("source_chars"),
        }


def register_web_routes(context: Any, plugin: Any) -> int:
    if not hasattr(context, "register_web_api"):
        logger.debug("[feiyu_state] register_web_api unavailable")
        return 0
    api = ReflectionPageApi(plugin)
    context.register_web_api(
        f"/{PLUGIN_NAME}/reflection/overview",
        api.get_overview,
        ["GET"],
        "Feiyu reflection overview",
    )
    logger.info("[feiyu_state] reflection page API registered")
    return 1
