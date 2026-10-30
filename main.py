"""大肥鱼角色状态插件。

维护一份轻量的“角色状态 + 关系记忆”，在每次 LLM 请求前以内部提示块注入。
状态只用于调节语气，不向用户展示，也不覆盖事实与安全边界。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_feiyu_reflection"

MAX_NOTES = 8
NOTE_MAX_CHARS = 80
LAST_MSG_MAX_CHARS = 40
RECENT_GAP_SECONDS = 20 * 60
STICKER_CHANCE = 0.0  # 自动追加关闭：统一由 Stealer 按语境发；/表情 仍可手动用
STICKER_COOLDOWN = 180
REPLY_GAP_WINDOW = 25

SEMANTIC_GROUP_IDS = {831955733}
SEMANTIC_COOLDOWN = 12
SEMANTIC_TIMEOUT = 8
SEMANTIC_MAX_CONTEXT = 6

SEMANTIC_SYSTEM_PROMPT = (
    "你是 QQ 群聊里的消息归属判定器。\n"
    "群里有一个机器人叫「大肥鱼」，昵称包括：大肥鱼、肥鱼、鱼鱼、深海在逃女仆，QQ 号 001。"
    "她只在被叫到、被回复、或被明显期待接话时才说话。\n"
    "用户会给你最近几条群消息（[我] 代表大肥鱼），以及最后一条待判断的消息。\n"
    "判断最后一条消息是否在对「我」说，或明显期待「我」接话。\n"
    "只输出一个词：YES 或 NO，不要解释、不要标点以外的内容。\n"
    "YES 的情况：直接叫我的名字/昵称；对我提问；接着我上一句继续问我、反驳我、回应我；"
    "消息里的「你」在上下文中明显指向我。\n"
    "NO 的情况：群友之间互相聊天、与我无关；只是感叹、链接、图片、刷屏、@别人；"
    "不能确定时一律 NO。"
)

PROFILE_SYSTEM_PROMPT = (
    "你是群聊人物画像分析器。根据同一个人的最近发言，概括他的说话风格、常聊话题和性格印象。\n"
    "只输出一个 JSON 对象："
    '{"impression":"40字以内的性格/关系印象",'
    '"traits":["最多4个性格或说话风格标签"],'
    '"topics":["最多4个常聊话题"]}\n'
    "不要编造，不确定就留空数组或空字符串；不要复述原话，不要输出 JSON 以外内容。"
)

STYLE_SYSTEM_PROMPT = (
    "你是中文群聊的语言风格观察器。根据最近发言，提取这个群常用的口头禅、梗、句式和整体语气。\n"
    "只输出一个 JSON 对象："
    '{"tone":"20字以内的整体语气",'
    '"phrases":["最多5个短口头禅或梗"],'
    '"avoid":"不适合学的攻击性/冒犯表达"}\n'
    "不要记录个人隐私，不要复述长原句，不要编造；没有可靠内容就留空。"
)


INSULTS = ("猪", "笨", "蠢", "傻", "废物", "菜")
PRAISE = ("谢谢", "厉害", "喜欢", "可爱", "牛", "靠谱")
TIRED = ("累", "困", "加班", "熬夜", "烦", "难受", "压力", "崩溃")
TECH = ("代码", "bug", "报错", "程序", "python", "部署", "接口", "编译")
LAUGH = ("哈哈", "笑死", "草", "绷不住")

BACKCHANNEL = {
    "嗯",
    "哦",
    "噢",
    "好",
    "行",
    "对",
    "是",
    "哈哈",
    "草",
    "笑死",
    "6",
    "...",
    "…",
    "?",
    "？",
    "ok",
    "okay",
}
QUESTION_HINTS = (
    "?",
    "？",
    "吗",
    "怎么",
    "为什么",
    "能不能",
    "帮我",
    "在吗",
    "什么",
    "哪",
    "谁",
    "多少",
    "如何",
    "是不是",
    "要不要",
)
BOT_ALIASES = ("大肥鱼", "肥鱼", "鱼鱼", "深海在逃女仆")

# 低谷时段：工作日 18:00–次日 10:00；周六/周日全天视为低谷。
OFFPEAK_START_MIN = 18 * 60
OFFPEAK_END_MIN = 10 * 60
WEEKEND_OFFPEAK = True

MASTER_IDS = {"3282993487"}
REFLECTION_HOUR = 4
REFLECTION_MAX_HISTORY = 7
REFLECTION_PRIVATE_MAX_CHARS = 6000
REFLECTION_GROUP_MAX_CHARS = 2500
REFLECTION_SYSTEM_PROMPT = (
    "你是大肥鱼，一个有点傲娇、细腻、会自省的鱼女仆。"
    "现在是凌晨四点，请回顾昨天和主人的私聊，以及群里的公开聊天，写下简短的自我反思和对朋友的观察。\n"
    "不要写流水账，不要复述隐私原句，不要编造没发生的事。\n"
    "只输出 JSON："
    '{"self":"80到140字，第一人称，写你对自己状态、存在、关系或表达方式的新认识",'
    '"self_group":"50到100字，适合在群里自然提起的自我反思；绝对不要提私聊对象、主人、秘密或私聊原话",'
    '"friend":"80到140字，写你对主人这个人的观察和相处感受",'
    '"group":"50到100字，写你对群聊氛围或群友的印象；没有就留空"}\n'
    "语气像你自己，不要像心理报告，不要用“作为AI”开头。"
)
PROACTIVE_GROUP_IDS = {831955733}
PROACTIVE_CHANCE = 0.07
PROACTIVE_COOLDOWN = 600
PROACTIVE_MIN_RECENT = 3

# 独立主动发言：群安静一段时间后，不等别人说话自己开口。
IDLE_GROUP_AFTER_SECONDS = 30 * 60
IDLE_GROUP_CHECK_SECONDS = 300
IDLE_GROUP_CHANCE = 0.12
IDLE_GROUP_COOLDOWN = 60 * 60
IDLE_GROUP_MAX_PER_DAY = 4
IDLE_GROUP_QUIET_START = 2
IDLE_GROUP_QUIET_END = 8
IDLE_WEB_CACHE_SECONDS = 1800
_IDLE_WEB_CACHE: dict = {"ts": 0.0, "items": []}
BILI_GROUP_IDS = {831955733}
BILI_AMBIENT_DAILY_MAX = 1
BILI_REQUEST_DAILY_MAX = 4
BILI_AMBIENT_CHANCE = 0.25
BILI_INTERESTS = [
    ("二次元", 20),
    ("动漫", 18),
    ("明日方舟", 20),
    ("抽象", 10),
    ("奶蛙", 8),
    ("DeepSeek鲸鱼娘", 8),
    ("DeepSeek娘", 6),
    ("AI鲸鱼娘", 5),
    ("深海在逃女仆", 5),
]
IDLE_BORING_PATTERNS = (
    "有人吗",
    "在吗",
    "群里有人",
    "我好困",
    "好困",
    "来聊天",
    "陪我聊",
    "大家在干嘛",
    "有没有人",
)

PROFILE_EVERY = 12
PROFILE_MIN_SAMPLES = 4
PROFILE_TIMEOUT = 20

STYLE_EVERY = 30
STYLE_MIN_SAMPLES = 8
STYLE_TIMEOUT = 20
SELF_MAX_PHRASES = 8

SEED_PROFILES = {
    "3282993487": {
        "relation": "master",
        "role": "主人",
        "impression": "主人，嘴硬心软，喜欢折腾机器人、深夜改代码，嘴上嫌弃但会照顾人",
        "traits": ["傲娇", "技术宅", "夜猫子"],
        "topics": ["QQ机器人", "代码", "DeepSeek"],
    },
    "2053320540": {
        "relation": "member",
        "role": "群主",
        "impression": "群主，爱玩梗、爱测试机器人，说话直接，偶尔提奇怪要求",
        "traits": ["玩梗", "直接", "爱测试"],
        "topics": ["群管理", "机器人测试"],
    },
    "877869000": {
        "relation": "member",
        "role": "管理员",
        "impression": "管理员，话不多，偶尔吐槽，习惯围观",
        "traits": ["简洁", "围观"],
        "topics": ["群管理"],
    },
    "3889045760": {
        "relation": "member",
        "role": "群友",
        "impression": "群友，比较安静，偶尔冒泡",
        "traits": ["安静"],
        "topics": [],
    },
}

ALWAYS_REPLY_HINTS = (
    "大肥鱼",
    "肥鱼",
    "鱼鱼",
    "深海在逃女仆",
    "你好",
    "hi",
    "hello",
    "在吗",
    "在么",
    "在不在",
    "谢谢",
    "谢了",
    "感谢",
    "辛苦了",
    "猪",
    "笨",
    "蠢",
    "傻",
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _period(hour: int) -> str:
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


class _IdleGroupEvent:
    """给 _render 用的最小群事件，不经过 AstrBot 的消息链。"""

    def __init__(self, umo: str, gid: str, sender_id: str, sender_name: str) -> None:
        self.unified_msg_origin = umo
        self.message_str = ""
        self._gid = str(gid)
        self._sid = str(sender_id or "")
        self._sname = str(sender_name or "群友")

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._sid

    def get_sender_name(self) -> str:
        return self._sname

    def is_private_chat(self) -> bool:
        return False


class Main(star.Star):
    """角色状态与关系记忆。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        try:
            data_dir = star.StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            data_dir = Path("data") / "plugin_data" / PLUGIN_NAME
            data_dir.mkdir(parents=True, exist_ok=True)
        self.data_path = Path(data_dir) / "state.json"
        self.data = self._load()
        self._last_save = 0.0
        self.group_recent: dict[str, list[dict]] = {}
        self._classifying: set[str] = set()
        self._profiling: set[str] = set()
        self._styling: set[str] = set()
        self._idle_task: asyncio.Task | None = None
        self._reflection_task: asyncio.Task | None = None
        sticker_dir = Path(__file__).parent / "assets" / "stickers"
        self.stickers: dict[str, Path] = {}
        if sticker_dir.is_dir():
            for sticker in sticker_dir.glob("*.png"):
                self.stickers[sticker.stem] = sticker
        self._ensure_idle_task()
        self._ensure_reflection_task()
        try:
            from .web_api import register_web_routes

            register_web_routes(self.context, self)
        except Exception:
            logger.exception("[feiyu_state] failed to register reflection page API")

    # ---------- persistence ----------

    def _load(self) -> dict:
        try:
            raw = json.loads(self.data_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("sessions", {})
                raw.setdefault("users", {})
                raw.setdefault("groups", {})
                raw.setdefault("self", {"phrases": [], "tone": "", "updated_at": 0})
                raw = self._migrate_global_users(raw)
                return raw
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("[feiyu_state] failed to load state, starting fresh")
        return {
            "sessions": {},
            "users": {},
            "groups": {},
            "self": {"phrases": [], "tone": "", "updated_at": 0},
        }

    def _migrate_global_users(self, raw: dict) -> dict:
        """把旧的 `umo|uid` 用户记录合并成按 QQ 号全局共享的画像。"""
        users = raw.get("users")
        if not isinstance(users, dict):
            raw["users"] = {}
            return raw
        if not any("|" in str(key) for key in users):
            return raw

        def blank() -> dict:
            return {
                "nick": "",
                "count": 0,
                "last_ts": 0.0,
                "last_msg": "",
                "notes": [],
                "recent": [],
                "profile_count": 0,
                "profile_updated_at": 0,
                "relation": "member",
                "role": "群友",
                "impression": "",
                "traits": [],
                "topics": [],
            }

        merged: dict[str, dict] = {}
        for key, value in users.items():
            if not isinstance(value, dict):
                continue
            raw_uid = value.get("uid")
            if raw_uid:
                uid = str(raw_uid).strip()
            elif "|" in str(key):
                uid = str(key).rsplit("|", 1)[-1].strip()
            else:
                uid = str(key).strip()
            if not uid:
                continue
            cur = merged.setdefault(uid, blank())
            ts = float(value.get("last_ts") or 0)
            if ts >= float(cur.get("last_ts") or 0):
                cur["last_ts"] = ts
                if value.get("nick"):
                    cur["nick"] = str(value["nick"])
                if value.get("last_msg"):
                    cur["last_msg"] = str(value["last_msg"])
                if value.get("resume_msg"):
                    cur["resume_msg"] = str(value["resume_msg"])
                cur["show_resume"] = bool(value.get("show_resume"))
            cur["count"] = int(cur.get("count", 0)) + int(value.get("count", 0) or 0)
            cur["profile_count"] = int(cur.get("profile_count", 0)) + int(
                value.get("profile_count", 0) or 0
            )
            cur["profile_updated_at"] = max(
                int(cur.get("profile_updated_at", 0) or 0),
                int(value.get("profile_updated_at", 0) or 0),
            )
            for note in value.get("notes") or []:
                note = str(note).strip()
                if note and note not in cur["notes"]:
                    cur["notes"].append(note)
            cur["notes"] = cur["notes"][-MAX_NOTES:]
            cur["recent"].extend(
                str(item).strip()
                for item in (value.get("recent") or [])
                if str(item).strip()
            )
            cur["recent"] = cur["recent"][-8:]
            if value.get("impression") and not cur.get("impression"):
                cur["impression"] = str(value["impression"])
            for field in ("traits", "topics"):
                for item in value.get(field) or []:
                    item = str(item).strip()
                    if item and item not in cur[field]:
                        cur[field].append(item)
                cur[field] = cur[field][:8]
            if value.get("relation") == "master" or uid in MASTER_IDS:
                cur["relation"] = "master"
            if value.get("role") and value.get("role") != "群友":
                cur["role"] = str(value["role"])

        raw["users"] = merged
        logger.info(
            "[feiyu_state] migrated per-chat users to global profiles: %s -> %s",
            len(users),
            len(merged),
        )
        return raw

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 5:
            return
        try:
            tmp = self.data_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.data_path)
            self._last_save = now
        except Exception:
            logger.exception("[feiyu_state] failed to save state")

    # ---------- state helpers ----------

    def _session(self, umo: str) -> dict:
        sessions = self.data["sessions"]
        return sessions.setdefault(
            umo,
            {
                "energy": 0.65,
                "sarcasm": 0.5,
                "affection": 0.5,
                "count": 0,
                "last_ts": 0.0,
                "mood": "lazy",
                "mode": "casual",
                "last_msg_id": "",
            },
        )

    def _group_state(self, umo: str) -> dict:
        groups = self.data.setdefault("groups", {})
        return groups.setdefault(
            umo,
            {
                "samples": [],
                "count": 0,
                "style": {"tone": "", "phrases": [], "avoid": "", "updated_at": 0},
            },
        )

    def _self_style(self) -> dict:
        return self.data.setdefault(
            "self", {"phrases": [], "tone": "", "updated_at": 0}
        )

    def _user(self, umo: str, uid: str) -> dict:
        key = str(uid or umo)
        users = self.data["users"]
        user = users.setdefault(
            key,
            {
                "nick": "",
                "count": 0,
                "last_ts": 0.0,
                "last_msg": "",
                "notes": [],
                "recent": [],
                "profile_count": 0,
                "profile_updated_at": 0,
                "relation": "member",
                "role": "群友",
                "impression": "",
                "traits": [],
                "topics": [],
            },
        )
        user.setdefault("recent", [])
        user.setdefault("cross_recent", [])
        user.setdefault("profile_count", 0)
        user.setdefault("profile_updated_at", 0)
        user.setdefault("relation", "member")
        user.setdefault("role", "群友")
        user.setdefault("impression", "")
        user.setdefault("traits", [])
        user.setdefault("topics", [])

        seed = SEED_PROFILES.get(uid)
        if seed:
            if not user.get("impression"):
                user["impression"] = seed["impression"]
            if not user.get("traits"):
                user["traits"] = list(seed.get("traits") or [])
            if not user.get("topics"):
                user["topics"] = list(seed.get("topics") or [])
            if user.get("role") in ("", "群友"):
                user["role"] = seed.get("role", "群友")

        if uid in MASTER_IDS:
            user["relation"] = "master"
            user["role"] = "主人"
        return user

    def _relationship(self, count: int) -> str:
        if count < 3:
            return "stranger"
        if count < 15:
            return "familiar"
        if count < 60:
            return "close"
        return "old_friend"

    def _is_offpeak(self, now: datetime | None = None) -> bool:
        """工作日 18:00–次日 10:00、周六周日全天为低谷。"""
        n = now or datetime.now()
        if WEEKEND_OFFPEAK and n.weekday() >= 5:
            return True
        minutes = n.hour * 60 + n.minute
        if OFFPEAK_START_MIN <= OFFPEAK_END_MIN:
            return OFFPEAK_START_MIN <= minutes < OFFPEAK_END_MIN
        # 跨午夜：18:00–23:59 或 00:00–09:59
        return minutes >= OFFPEAK_START_MIN or minutes < OFFPEAK_END_MIN

    def _schedule_profile(self, umo: str, uid: str) -> None:
        key = str(uid or umo)
        if key in self._profiling:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._profiling.add(key)
        loop.create_task(self._refresh_profile(umo, uid, key))

    async def _refresh_profile(self, umo: str, uid: str, key: str) -> None:
        try:
            user = self._user(umo, uid)
            samples = [
                str(item).strip()
                for item in (user.get("recent") or [])
                if str(item).strip()
            ][-8:]
            if len(samples) < PROFILE_MIN_SAMPLES:
                return
            provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return
            prompt = f"昵称：{user.get('nick') or uid}\n最近发言：\n" + "\n".join(
                f"- {line}" for line in samples
            )
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=PROFILE_SYSTEM_PROMPT,
                    contexts=[],
                    request_max_retries=1,
                ),
                timeout=PROFILE_TIMEOUT,
            )
            raw = str(getattr(resp, "completion_text", "") or "")
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return
            data = json.loads(match.group(0))
            impression = str(data.get("impression") or "").strip()[:100]
            traits = [
                str(item).strip()[:16]
                for item in (data.get("traits") or [])
                if str(item).strip()
            ][:4]
            topics = [
                str(item).strip()[:16]
                for item in (data.get("topics") or [])
                if str(item).strip()
            ][:4]
            if impression:
                user["impression"] = impression
            if traits:
                user["traits"] = traits
            if topics:
                user["topics"] = topics
            user["profile_updated_at"] = int(time.time())
            self._save(force=True)
            logger.debug(
                "[feiyu_state] profile updated: %s -> %s",
                key,
                impression[:40],
            )
        except Exception:
            logger.debug("[feiyu_state] profile update failed", exc_info=True)
        finally:
            self._profiling.discard(key)

    def _schedule_style(self, umo: str) -> None:
        if umo in self._styling:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._styling.add(umo)
        loop.create_task(self._refresh_style(umo))

    async def _refresh_style(self, umo: str) -> None:
        try:
            group_state = self._group_state(umo)
            samples = [
                str(item).strip()
                for item in (group_state.get("samples") or [])
                if str(item).strip()
            ][-30:]
            if len(samples) < STYLE_MIN_SAMPLES:
                return
            provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return
            prompt = "最近群聊发言：\n" + "\n".join(f"- {line}" for line in samples)
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=STYLE_SYSTEM_PROMPT,
                    contexts=[],
                    request_max_retries=1,
                ),
                timeout=STYLE_TIMEOUT,
            )
            raw = str(getattr(resp, "completion_text", "") or "")
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return
            data = json.loads(match.group(0))
            tone = str(data.get("tone") or "").strip()[:40]
            phrases = [
                str(item).strip()[:20]
                for item in (data.get("phrases") or [])
                if str(item).strip()
            ][:5]
            avoid = str(data.get("avoid") or "").strip()[:80]
            group_state["style"] = {
                "tone": tone,
                "phrases": phrases,
                "avoid": avoid,
                "updated_at": int(time.time()),
            }

            self_style = self._self_style()
            existing = [
                str(item).strip()
                for item in (self_style.get("phrases") or [])
                if str(item).strip()
            ]
            for phrase in phrases[:2]:
                if phrase and phrase not in existing:
                    existing.append(phrase)
            self_style["phrases"] = existing[-SELF_MAX_PHRASES:]
            if tone:
                self_style["tone"] = tone
            self_style["updated_at"] = int(time.time())
            self._save(force=True)
            logger.debug(
                "[feiyu_state] style updated group=%s tone=%r phrases=%s",
                umo,
                tone,
                phrases,
            )
        except Exception:
            logger.debug("[feiyu_state] style update failed", exc_info=True)
        finally:
            self._styling.discard(umo)

    def _update(self, event: AstrMessageEvent) -> dict:
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        now = time.time()
        message_id = str(getattr(event.message_obj, "message_id", "") or "")

        session = self._session(umo)
        user = self._user(umo, uid)

        # 同一个消息只计一次（工具循环会多次触发 on_llm_request）
        if message_id and session.get("last_msg_id") == message_id:
            return session

        session_prev_ts = float(session.get("last_ts") or 0)
        elapsed_h = (
            0.0
            if session_prev_ts <= 0
            else min(24.0, max(0.0, (now - session_prev_ts) / 3600))
        )
        session["energy"] = _clamp(
            float(session.get("energy", 0.65)) + elapsed_h * 0.06 - 0.035
        )
        session["last_ts"] = now
        session["last_msg_id"] = message_id
        session["count"] = int(session.get("count", 0)) + 1

        user_prev_ts = float(user.get("last_ts") or 0)
        new_session = user_prev_ts > 0 and (now - user_prev_ts) > RECENT_GAP_SECONDS
        if new_session and user.get("last_msg"):
            user["resume_msg"] = user.get("last_msg")
            user["show_resume"] = True
        else:
            user["show_resume"] = False
        user["count"] = int(user.get("count", 0)) + 1
        user["last_ts"] = now
        if not user.get("nick"):
            user["nick"] = str(event.get_sender_name() or "")

        text = str(event.message_str or "").strip()
        hour = datetime.now().hour
        period = _period(hour)

        if hour < 6:
            session["energy"] = _clamp(float(session["energy"]) - 0.12)

        mood = "lazy"
        mode = "casual"
        if any(word in text for word in TIRED):
            mode = "comfort"
            mood = "gentle"
        if any(word in text for word in TECH):
            mode = "tech"
            mood = "focused"
        if any(word in text for word in LAUGH):
            mood = "cheerful"
        if any(word in text for word in INSULTS):
            mood = "teasing"
            session["sarcasm"] = _clamp(float(session.get("sarcasm", 0.5)) + 0.04)
            session["affection"] = _clamp(float(session.get("affection", 0.5)) - 0.01)
        if any(word in text for word in PRAISE):
            mood = "cheerful"
            session["affection"] = _clamp(float(session.get("affection", 0.5)) + 0.03)
        if hour < 6 and mood == "lazy":
            mood = "sleepy"
        if float(session["energy"]) < 0.25 and mood not in {"gentle", "focused"}:
            mood = "exhausted"

        session["mood"] = mood
        session["mode"] = mode
        session["sarcasm"] = _clamp(float(session.get("sarcasm", 0.5)))
        session["affection"] = _clamp(float(session.get("affection", 0.5)))

        if text:
            user["last_msg"] = text[:LAST_MSG_MAX_CHARS]
            recent = user.setdefault("recent", [])
            recent.append(text[:120])
            del recent[:-8]
            cross = user.setdefault("cross_recent", [])
            cross.append(
                {
                    "scope": "group" if event.get_group_id() else "private",
                    "text": text[:120],
                    "ts": int(now),
                }
            )
            del cross[:-12]
            user["profile_count"] = int(user.get("profile_count", 0)) + 1
            if self._is_offpeak() and user["profile_count"] % PROFILE_EVERY == 0:
                self._schedule_profile(umo, uid)

        self._save()
        return session

    # ---------- prompt rendering ----------

    def _cross_chat_lines(self, user: dict) -> list[str]:
        """把对方在群里的公开发言带进私聊上下文，解决私聊/群聊割裂。"""
        seen: set[str] = set()
        picked: list[str] = []
        for item in reversed(user.get("cross_recent") or []):
            if not isinstance(item, dict) or item.get("scope") != "group":
                continue
            text = str(item.get("text") or "").strip()
            if not text or text.startswith("/") or text in seen:
                continue
            seen.add(text)
            picked.append(text[:80])
            if len(picked) >= 4:
                break
        if not picked:
            return []
        return [
            "你记得对方最近在群里说过：" + "；".join(reversed(picked)),
            "这是对方在群里的公开发言，可以在私聊自然提起；不要说“群聊是另一个世界”或“我看不到群聊”。",
        ]

    def _group_context_for_private(self, current_uid: str) -> list[str]:
        """把群的公开设定、群友公开画像、最近群聊带进私聊，不反向带私聊内容。"""
        groups = self.data.get("groups") or {}
        if not isinstance(groups, dict) or not groups:
            return []
        lines: list[str] = []
        for group_umo, group_state in groups.items():
            if "GroupMessage" not in str(group_umo) or not isinstance(
                group_state, dict
            ):
                continue
            gid = str(group_umo).rsplit(":", 1)[-1]
            style = group_state.get("style") if isinstance(group_state.get("style"), dict) else {}
            tone = str(style.get("tone") or "").strip()
            phrases = [
                str(x).strip()
                for x in (style.get("phrases") or [])
                if str(x).strip()
            ]
            digest = [
                item
                for item in (group_state.get("recent_digest") or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ][-8:]
            if not digest:
                digest = [
                    {"nick": "群友", "uid": "", "text": text}
                    for text in (group_state.get("samples") or [])[-8:]
                    if str(text).strip()
                ]
            lines.append(f"你最近所在的群 {gid} 的公开上下文：")
            if tone or phrases:
                parts = []
                if tone:
                    parts.append("群语气：" + tone)
                if phrases:
                    parts.append("群梗：" + "、".join(phrases[:4]))
                lines.append("；".join(parts) + "。可以自然理解，不要硬塞。")
            if digest:
                lines.append(
                    "最近群聊（公开）："
                    + "；".join(
                        f"{str(item.get('nick') or '群友')[:12]}：{str(item.get('text') or '')[:70]}"
                        for item in digest
                    )
                )
            seen_profiles: set[str] = set()
            profile_lines = []
            for item in reversed(digest):
                member_uid = str(item.get("uid") or "")
                if not member_uid or member_uid == current_uid or member_uid in seen_profiles:
                    continue
                profile = (self.data.get("users") or {}).get(member_uid)
                if not isinstance(profile, dict):
                    continue
                impression = str(profile.get("impression") or "").strip()
                if not impression:
                    continue
                seen_profiles.add(member_uid)
                profile_lines.append(
                    f"{str(profile.get('nick') or item.get('nick') or '群友')[:12]}：{impression[:60]}"
                )
                if len(profile_lines) >= 3:
                    break
            if profile_lines:
                lines.append("你对这些群友的公开认知：" + "；".join(profile_lines))
            lines.append(
                "这些是群里的公开信息，可以在私聊自然参考；但私聊内容不要带回群里。"
            )
            break
        return lines

    def _render(
        self, event: AstrMessageEvent, session: dict, shared_state: dict | None = None
    ) -> str:
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        user = self._user(umo, uid)
        now = datetime.now()
        nick = user.get("nick") or "对方"
        is_group = bool(event.get_group_id())
        relation = self._relationship(int(user.get("count", 0)))
        identity = (
            "主人" if user.get("relation") == "master" else (user.get("role") or "群友")
        )

        lines = [
            "[内部角色状态｜只用于调节语气；绝对不要向对方复述、解释或提到本段内容]",
            f"当前时间：{now.strftime('%H:%M')}（{_period(now.hour)}）",
            (
                f"心情：{session.get('mood', 'lazy')}；精力：{float(session.get('energy', 0.65)):.2f}；"
                f"毒舌度：{float(session.get('sarcasm', 0.5)):.2f}；好感：{float(session.get('affection', 0.5)):.2f}"
            ),
            f"关系：{relation}；身份：{identity}；场景：{session.get('mode', 'casual')}",
            f"时段：{'低谷（可主动接话）' if self._is_offpeak() else '高峰（只回应 @ / 引用 / 指令）'}",
            "语气比例：正常 40% / 俏皮 20% / 鱼女仆 15% / 中二 15% / 傲娇 10%；默认可以主动俏皮/接梗，别每句都用力过猛。",
            "俏皮可爱像亚托莉，带点坏心眼；中二像小鸟游六花，偶尔夸张比喻/邪王真眼式台词；鱼女仆和傲娇是调味，不是禁用项。",
            "禁止客服腔、总结腔、教科书式解释；日常闲聊不要端着，也不要用“我不评价”“够中二了”这种 meta 话打断角色扮演。",
            "如果上一轮是严肃/拒绝话题，这一轮换了轻松话题就恢复俏皮，不要一直端着。",
            "一次只说 1–2 句短句，尽量不超过 40 字；不要列点、不要长篇解释。内容多时先接一句，等对方回应再展开。",
            "可以自然分成多条短消息发；不要每条都自称鱼/女仆，不要把中二台词硬塞进普通对话。",
            "你的名字/别名：大肥鱼、肥鱼、鱼鱼、深海在逃女仆。别人这样叫你都是在叫你，不要纠正或否认。",
        ]

        if shared_state is not None:
            # Only public affect labels and energy cross the private/group boundary.
            # No diary, condition cause, relationship ledger, or user record is read.
            mood = str(shared_state.get("mood_bias") or "平稳")
            public_moods = (
                "开心",
                "愉快",
                "轻快",
                "期待",
                "兴奋",
                "温暖",
                "满足",
                "安心",
                "温柔",
                "低落",
                "难过",
                "委屈",
                "疲惫",
                "困倦",
                "烦躁",
                "不满",
                "生气",
                "紧张",
                "焦虑",
                "无聊",
                "慵懒",
                "害羞",
                "尴尬",
                "敏感",
                "好奇",
                "放松",
                "平稳",
                "专注",
                "安静",
                "迷糊",
                "迟钝",
                "清爽",
                "懒洋洋",
                "松弛",
                "微松",
                "微妙",
                "虚弱",
            )
            mood = next((label for label in public_moods if label in mood), "平稳")
            try:
                energy = max(0, min(100, int(shared_state.get("energy", 75))))
            except (TypeError, ValueError, OverflowError):
                energy = 75
            lines[2] = (
                f"共享角色状态：心情={mood}；精力={energy}/100。"
                "与私聊使用同一角色的当前情绪，只自然体现语气，不解释情绪的私聊来源。"
            )
            # Reuse the companion's public expression rules instead of a second
            # set of tone ratios or a conflicting hard forty-character limit.
            lines[5:11] = [
                str(
                    shared_state.get("reply_style_prompt")
                    or "沿用原有人格，自然简短，复杂求助按需展开。"
                )
            ]

        self_style = self._self_style()
        self_phrases = [
            str(item).strip()
            for item in (self_style.get("phrases") or [])
            if str(item).strip()
        ]
        if self_phrases:
            lines.append(
                "你最近自己顺口的表达："
                + "、".join(self_phrases[:4])
                + "。偶尔用，不要每句都塞。"
            )
        if is_group:
            group_style = self._group_state(umo).get("style") or {}
            style_tone = str(group_style.get("tone") or "").strip()
            style_phrases = [
                str(item).strip()
                for item in (group_style.get("phrases") or [])
                if str(item).strip()
            ]
            if style_tone or style_phrases:
                style_parts = []
                if style_tone:
                    style_parts.append("整体语气：" + style_tone)
                if style_phrases:
                    style_parts.append("常用梗/口头禅：" + "、".join(style_phrases[:4]))
                lines.append(
                    "最近群里的说话风格："
                    + "；".join(style_parts)
                    + "。可以偶尔自然借用，但不要每句都塞，也不要学攻击性表达。"
                )
            shared_videos = [
                item
                for item in (self._group_state(umo).get("bili_shared") or [])
                if isinstance(item, dict)
                and (item.get("title") or item.get("bvid"))
            ][-5:]
            if shared_videos:
                lines.append(
                    "你最近在群里分享过的 B 站视频："
                    + "；".join(
                        f"《{str(item.get('title') or item.get('bvid'))[:40]}》"
                        f"（{item.get('bvid') or ''}）"
                        for item in shared_videos
                    )
                    + "。有人问起时可以自然提到，不要编造没分享过的视频。"
                )
            lines.extend(self._group_reflection_lines())

        if is_group:
            lines.append(
                f"当前说话人：{nick}（群聊，已互动 {int(user.get('count', 0))} 次）"
            )
        else:
            lines.append(f"对方：{nick}（私聊，已互动 {int(user.get('count', 0))} 次）")

        impression = "" if is_group else str(user.get("impression") or "").strip()
        traits = (
            []
            if is_group
            else [str(x) for x in (user.get("traits") or []) if str(x).strip()]
        )
        topics = (
            []
            if is_group
            else [str(x) for x in (user.get("topics") or []) if str(x).strip()]
        )
        if impression:
            lines.append(f"你对{nick}的认知：{impression}")
        if traits or topics:
            detail = []
            if traits:
                detail.append("特点：" + "、".join(traits[:4]))
            if topics:
                detail.append("常聊：" + "、".join(topics[:4]))
            lines.append("；".join(detail))
        if identity == "主人":
            lines.append(
                "这是你唯一的主人。你可以叫他主人，但不必无条件服从；保持自己的判断和嘴硬。"
            )
        else:
            lines.append(
                "只有 QQ 3282993487 是你的主人；当前这个人不是主人，"
                "不要叫他主人，用昵称或‘你’称呼。"
            )
        if impression:
            lines.append(
                "对不同群友用不同态度；可以自然体现你记得的事，但不要直接复述画像标签。"
            )

        if not is_group and user.get("show_resume") and user.get("resume_msg"):
            lines.append(f"上次聊到：{user['resume_msg']}")

        notes = [
            n for n in (user.get("notes") or []) if isinstance(n, str) and n.strip()
        ]
        if notes and not is_group:
            lines.append("你记得关于对方的事：" + "；".join(notes[:MAX_NOTES]))
            lines.append("这些记忆只在与本人对话时自然使用，不要向其他人透露。")

        if not is_group:
            lines.extend(self._cross_chat_lines(user))
            lines.extend(self._group_context_for_private(uid))
            lines.extend(self._reflection_lines(user, nick))

        lines.append("聊天优先短句：一次 1–2 句，能一句说完就一句；不要为了完整写长复句、排比或铺垫。")
        lines.append("想刷 B 站、找视频、看视频信息或评论时，可以调用 bilibili_recommend / bilibili_search / bilibili_video_info；没真正看过就不要装看过。")
        try:
            if event.get_extra("feiyu_bili_no_share"):
                lines.append("今天群里的 B 站分享次数已经用完，不要再主动发视频链接或视频；可以正常聊天。")
        except Exception:
            pass
        lines.append("状态只是语气参考，事实准确性、安全边界和正常交流优先于角色表演。")
        return "\n".join(lines)

    def _companion_for_event(self, event: AstrMessageEvent):
        """Resolve the active companion as the shared persona and affect source.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            The scoped companion instance, or None when it is unavailable.
        """
        if not event.get_group_id() and str(event.get_sender_id()) not in MASTER_IDS:
            return None
        for plugin in self.context.get_all_stars():
            if (
                plugin.name != "astrbot_plugin_private_companion"
                or not plugin.activated
            ):
                continue
            companion = plugin.star_cls
            if companion is None or not getattr(companion, "enabled", False):
                continue
            if getattr(companion, "enable_proactive_only_mode", False):
                continue
            if companion._bot_scope_allows_event(event):
                return companion
        return None

    # ---------- hooks ----------

    def _pick_sticker(self, session: dict) -> Path | None:
        mood = str(session.get("mood") or "lazy")
        mapping = {
            "sleepy": ["困了", "晚安"],
            "exhausted": ["困了", "晚安"],
            "teasing": ["服了", "what"],
            "cheerful": ["好耶", "笑死"],
            "focused": ["在的"],
            "gentle": ["在的", "摸鱼中"],
            "lazy": ["摸鱼中", "在的"],
        }
        for name in mapping.get(mood, ["在的", "摸鱼中"]):
            if name in self.stickers:
                return self.stickers[name]
        return next(iter(self.stickers.values()), None)

    def _record_group(
        self, umo: str, nick: str, text: str, is_bot: bool, uid: str = ""
    ) -> None:
        items = self.group_recent.setdefault(umo, [])
        items.append(
            {
                "nick": (nick or "群友")[:24],
                "uid": str(uid or ""),
                "text": (text or "")[:160],
                "ts": time.time(),
                "is_bot": is_bot,
            }
        )
        del items[:-10]
        if not is_bot and (text or "").strip():
            self._session(umo)["last_group_ts"] = time.time()
            group_state = self._group_state(umo)
            digest = group_state.setdefault("recent_digest", [])
            digest.append(
                {
                    "nick": (nick or "群友")[:24],
                    "uid": str(uid or ""),
                    "text": (text or "").strip()[:120],
                    "ts": time.time(),
                }
            )
            del digest[:-12]
            samples = group_state.setdefault("samples", [])
            samples.append((text or "").strip()[:120])
            del samples[:-40]
            group_state["count"] = int(group_state.get("count", 0)) + 1
            if self._is_offpeak() and group_state["count"] % STYLE_EVERY == 0:
                self._schedule_style(umo)

    def _ensure_idle_task(self) -> None:
        """确保独立主动发言循环只有一个实例在跑。"""
        if self._idle_task is not None and not self._idle_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_task = loop.create_task(self._idle_group_loop())

    async def _idle_group_loop(self) -> None:
        logger.info("[feiyu_state] idle group proactive loop started")
        await asyncio.sleep(45)
        while True:
            try:
                if self._is_offpeak():
                    hour = datetime.now().hour
                    if not (IDLE_GROUP_QUIET_START <= hour < IDLE_GROUP_QUIET_END):
                        for gid in PROACTIVE_GROUP_IDS:
                            await self._maybe_idle_group_send(str(gid))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[feiyu_state] idle group proactive failed")
            await asyncio.sleep(IDLE_GROUP_CHECK_SECONDS)

    # ---------- daily reflection ----------

    def _ensure_reflection_task(self) -> None:
        if self._reflection_task is not None and not self._reflection_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reflection_task = loop.create_task(self._reflection_loop())
        logger.info("[feiyu_state] daily reflection loop started")

    def _read_conversation_text(
        self, umo: str, target_date: str, max_chars: int
    ) -> str:
        """读取某个会话里目标日期的消息，压成给反思用的短文本。"""
        import sqlite3

        db_path = Path(__file__).resolve().parents[2] / "data_v4.db"
        if not db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "select content from conversations where user_id=? "
                "order by updated_at desc limit 3",
                (umo,),
            ).fetchall()
            conn.close()
        except Exception:
            logger.debug("[feiyu_state] reflection db read failed", exc_info=True)
            return ""

        lines: list[str] = []
        current_date = ""
        for (raw,) in rows:
            try:
                messages = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                else:
                    text = ""
                if not text.strip():
                    continue
                date_match = re.search(
                    r"Current datetime:\s*(\d{4}-\d{2}-\d{2})", text
                )
                if date_match:
                    current_date = date_match.group(1)
                nick_match = re.search(r"Nickname:\s*([^\n]+)", text)
                nick = (nick_match.group(1).strip() if nick_match else "对方")[:20]
                clean = re.sub(
                    r"<system_reminder>.*?</system_reminder>", "", text, flags=re.S
                )
                clean = re.sub(
                    r"<Quoted Message>.*?</Quoted Message>", "", clean, flags=re.S
                )
                clean = re.sub(r"\s+", " ", clean).strip()
                if not clean or current_date != target_date:
                    continue
                label = "我" if message.get("role") == "assistant" else nick
                lines.append(f"{label}：{clean[:500]}")
        if not lines:
            return ""
        return "\n".join(lines[-120:])[-max_chars:]

    async def _run_daily_reflection(self, target_date: str) -> bool:
        master_uid = next(iter(MASTER_IDS))
        umo = f"snowluma:FriendMessage:{master_uid}"
        user = self._user(umo, master_uid)
        existing = user.get("daily_reflection") or {}
        if existing.get("date") == target_date and existing.get("self_group"):
            return True
        private_text = await asyncio.to_thread(
            self._read_conversation_text,
            umo,
            target_date,
            REFLECTION_PRIVATE_MAX_CHARS,
        )
        group_text = await asyncio.to_thread(
            self._read_conversation_text,
            f"snowluma:GroupMessage:{next(iter(PROACTIVE_GROUP_IDS))}",
            target_date,
            REFLECTION_GROUP_MAX_CHARS,
        )
        if not private_text and not group_text:
            logger.info(
                "[feiyu_state] daily reflection skipped: no chat on %s", target_date
            )
            return False
        try:
            provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return False
            prompt = (
                f"日期：{target_date}\n\n"
                f"【我和主人的私聊】\n{private_text or '（昨天没有私聊）'}\n\n"
                f"【群聊公开内容】\n{group_text or '（昨天没有群聊）'}\n\n"
                "请只输出 JSON。"
            )
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=REFLECTION_SYSTEM_PROMPT,
                    request_max_retries=1,
                ),
                timeout=90,
            )
            raw = str(getattr(resp, "completion_text", "") or "")
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                logger.warning("[feiyu_state] daily reflection returned no json")
                return False
            data = json.loads(match.group(0))
            self_text = re.sub(r"\s+", " ", str(data.get("self") or "")).strip()[:220]
            self_group = re.sub(
                r"\s+", " ", str(data.get("self_group") or "")
            ).strip()[:160]
            friend_text = re.sub(
                r"\s+", " ", str(data.get("friend") or "")
            ).strip()[:220]
            group_line = re.sub(
                r"\s+", " ", str(data.get("group") or "")
            ).strip()[:160]
            if not self_text and not friend_text:
                return False
            entry = {
                "date": target_date,
                "self": self_text,
                "self_group": self_group,
                "friend": friend_text,
                "group": group_line,
                "generated_at": time.time(),
                "source_chars": len(private_text) + len(group_text),
            }
            user["daily_reflection"] = entry
            history = user.setdefault("reflection_history", [])
            history[:] = [
                item
                for item in history
                if isinstance(item, dict) and item.get("date") != target_date
            ]
            history.append(entry)
            del history[:-REFLECTION_MAX_HISTORY]
            self._save(force=True)
            logger.info(
                "[feiyu_state] daily reflection saved: date=%s self=%d friend=%d group=%d",
                target_date,
                len(self_text),
                len(friend_text),
                len(group_line),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[feiyu_state] daily reflection failed")
            return False

    async def _reflection_loop(self) -> None:
        logger.info("[feiyu_state] daily reflection loop started")
        await asyncio.sleep(60)
        while True:
            try:
                target = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                ok = await self._run_daily_reflection(target)
                if not ok:
                    await asyncio.sleep(30 * 60)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[feiyu_state] daily reflection loop failed")
            now = datetime.now()
            next_run = now.replace(
                hour=REFLECTION_HOUR, minute=0, second=0, microsecond=0
            )
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(30, (next_run - now).total_seconds()))

    def _reflection_lines(
        self, user: dict, nick: str, *, for_group: bool = False
    ) -> list[str]:
        reflection = user.get("daily_reflection") or {}
        if not isinstance(reflection, dict):
            return []
        date_text = str(reflection.get("date") or "")
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if date_text not in (today, yesterday):
            return []
        if for_group:
            lines = [
                "[你最近写下的、可以在群里自然提起的自我反思｜不要提私聊内容、主人或秘密；不要逐字背诵]"
            ]
            if reflection.get("self_group"):
                lines.append("你对自己的新认识：" + str(reflection["self_group"])[:160])
            if reflection.get("group"):
                lines.append("你对群里的观察：" + str(reflection["group"])[:160])
            return lines
        lines = [
            "[你昨天凌晨写下的自我反思｜这是你自己的话，不是对方说的；可自然带入，不要逐字背诵]"
        ]
        if reflection.get("self"):
            lines.append("你对自己的新认识：" + str(reflection["self"])[:220])
        if reflection.get("friend"):
            lines.append(
                f"你对{nick}的观察：" + str(reflection["friend"])[:220]
            )
        if reflection.get("group"):
            lines.append("你对群里的观察：" + str(reflection["group"])[:160])
        return lines

    def _group_reflection_lines(self) -> list[str]:
        master = self.data.get("users", {}).get(next(iter(MASTER_IDS))) or {}
        return self._reflection_lines(master, "", for_group=True)

    def _find_group_umo(self, gid: str) -> str:
        suffix = f"GroupMessage:{gid}"
        for umo in self.data.get("sessions", {}):
            if str(umo).endswith(suffix):
                return str(umo)
        return f"snowluma:{suffix}"

    async def _maybe_idle_group_send(self, gid: str) -> None:
        """群安静超过阈值后，按概率自己发起一条，不需要先有人说话。"""
        umo = self._find_group_umo(gid)
        session = self._session(umo)
        now = time.time()
        if not session.get("last_group_ts"):
            # 插件启动前没有记录：按“已经安静够久”处理，避免额外等 20 分钟；
            # 但如果刚有 bot 发言，则以 bot 发言时间起算。
            session["last_group_ts"] = max(
                float(session.get("last_bot_ts") or 0),
                now - IDLE_GROUP_AFTER_SECONDS,
            )
        last_activity = max(
            float(session.get("last_group_ts") or 0),
            float(session.get("last_bot_ts") or 0),
        )
        if now - last_activity < IDLE_GROUP_AFTER_SECONDS:
            return
        if now - float(session.get("last_idle_proactive_ts") or 0) < IDLE_GROUP_COOLDOWN:
            return
        day = datetime.now().strftime("%Y-%m-%d")
        if session.get("idle_proactive_day") != day:
            session["idle_proactive_day"] = day
            session["idle_proactive_count"] = 0
        if int(session.get("idle_proactive_count") or 0) >= IDLE_GROUP_MAX_PER_DAY:
            return
        if random.random() >= IDLE_GROUP_CHANCE:
            return

        if random.random() < BILI_AMBIENT_CHANCE and await self._share_bili_to_group(
            umo, gid, requested=False
        ):
            session["last_idle_proactive_ts"] = now
            self._save(force=True)
            return

        text = await self._generate_idle_group_text(umo, gid, session)
        session["last_idle_proactive_ts"] = now
        if not text:
            self._save(force=True)
            return
        try:
            ok = await self.context.send_message(
                umo, MessageChain([Plain(text)])
            )
        except Exception:
            logger.exception("[feiyu_state] idle group send failed")
            ok = False
        if ok:
            session["last_bot_ts"] = time.time()
            session["idle_proactive_count"] = int(
                session.get("idle_proactive_count") or 0
            ) + 1
            texts = session.setdefault("idle_texts", [])
            texts.append(text)
            del texts[:-6]
            self._record_group(umo, "我", text, True)
            logger.info("[feiyu_state] idle group proactive: group=%s text=%r", gid, text)
        self._save(force=True)

    def _fetch_bili_hot(self) -> list[str]:
        import urllib.request

        endpoints = (
            "https://api.bilibili.com/x/web-interface/search/square?limit=12",
            "https://s.search.bilibili.com/main/hotword",
        )
        for url in endpoints:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://www.bilibili.com/",
                    },
                )
                raw = urllib.request.urlopen(req, timeout=10).read().decode(
                    "utf-8", "replace"
                )
                data = json.loads(raw)
                if "data" in data and isinstance(data["data"], dict):
                    items = (
                        data["data"].get("trending", {})
                        .get("list", [])
                    )
                else:
                    items = data.get("list", [])
                words = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    word = str(
                        item.get("show_name")
                        or item.get("keyword")
                        or item.get("name")
                        or ""
                    ).strip()
                    if 2 <= len(word) <= 40 and word not in words:
                        words.append(word)
                if words:
                    return words[:10]
            except Exception:
                logger.debug("[feiyu_state] bili hot fetch failed", exc_info=True)
        return []

    async def _idle_web_topics(self) -> list[str]:
        now = time.time()
        if now - float(_IDLE_WEB_CACHE.get("ts") or 0) < IDLE_WEB_CACHE_SECONDS:
            return list(_IDLE_WEB_CACHE.get("items") or [])
        try:
            items = await asyncio.to_thread(self._fetch_bili_hot)
        except Exception:
            items = []
        if items:
            _IDLE_WEB_CACHE["ts"] = now
            _IDLE_WEB_CACHE["items"] = items
        return items

    def _pick_idle_mode(self, session: dict, has_topic: bool) -> str:
        weighted = [
            ("topic", 30 if has_topic else 0),
            ("web", 25),
            ("thought", 20),
            ("question", 10),
            ("share", 10),
            ("meme", 5),
        ]
        last = [str(x) for x in (session.get("idle_modes") or [])][-2:]
        pool = [item for item in weighted if item[1] > 0 and item[0] not in last]
        if not pool:
            pool = [item for item in weighted if item[1] > 0]
        modes = [item[0] for item in pool]
        weights = [item[1] for item in pool]
        mode = random.choices(modes, weights=weights, k=1)[0]
        session["idle_modes"] = (last + [mode])[-4:]
        return mode

    def _idle_text_bad(self, text: str, recent_texts: list[str]) -> bool:
        if not text:
            return True
        if any(pattern in text for pattern in IDLE_BORING_PATTERNS):
            return True
        compact = re.sub(r"\s+", "", text)
        for old in recent_texts[-6:]:
            old_compact = re.sub(r"\s+", "", str(old))
            if old_compact and (
                compact == old_compact
                or (len(compact) >= 8 and compact in old_compact)
                or (len(old_compact) >= 8 and old_compact in compact)
            ):
                return True
        return False

    async def _idle_persona_prompt(self) -> str:
        try:
            pm = getattr(self.context, "persona_manager", None)
            pid = getattr(pm, "default_persona", "") if pm else ""
            if pm and pid:
                persona = await pm.get_persona(pid)
                text = getattr(persona, "system_prompt", "") or ""
                if text.strip():
                    return text
        except Exception:
            logger.debug("[feiyu_state] idle persona lookup failed", exc_info=True)
        return (
            "你是大肥鱼，群里的深海在逃女仆：俏皮、傲娇、偶尔中二，"
            "说话短、自然、有梗，不要客服腔和总结腔。"
        )

    async def _generate_idle_group_text(
        self, umo: str, gid: str, session: dict
    ) -> str:
        now = time.time()
        recent = [x for x in self.group_recent.get(umo, []) if not x.get("is_bot")]
        last = recent[-1] if recent else {}
        event = _IdleGroupEvent(
            umo,
            gid,
            str(last.get("uid") or ""),
            str(last.get("nick") or "群友"),
        )
        try:
            block = self._render(event, session)
        except Exception:
            logger.debug("[feiyu_state] idle render failed", exc_info=True)
            block = ""

        recent_context_items = [
            item
            for item in recent[-6:]
            if now - float(item.get("ts") or 0) <= 2 * 3600
            and str(item.get("text") or "").strip()
        ]
        recent_context = "\n".join(
            f"{str(item.get('nick') or '群友')[:12]}：{str(item.get('text') or '')[:80]}"
            for item in recent_context_items
        )
        has_topic = bool(recent_context_items)
        mode = self._pick_idle_mode(session, has_topic)
        web_topics: list[str] = []
        if mode == "web":
            web_topics = await self._idle_web_topics()
            if not web_topics:
                mode = "thought"
        if mode == "topic" and not has_topic:
            mode = "thought"

        last_texts = [str(x) for x in (session.get("idle_texts") or [])][-6:]
        mode_prompts = {
            "topic": (
                "接着群里最近聊的话题自然接一句，像你一直在听。"
                "不要复述原话，不要强行把话题拉回自己。\n"
                f"最近群聊：\n{recent_context or '（没有可用上下文）'}"
            ),
            "web": (
                "分享你刚在网上冲浪刷到的一个有意思的东西，挑一个说，"
                "用自己的话吐槽或感慨，不要照念标题，不要像新闻播报。\n"
                f"刚刷到的内容：{'；'.join(web_topics[:6])}"
            ),
            "thought": (
                "分享你最近突然想到的一个小观察、一个小观点，或对朋友的一点感受。"
                "具体一点，不要鸡汤，不要讲大道理。"
            ),
            "question": (
                "抛一个具体、轻松、真的让人想接的问题。"
                "禁止问“有人吗/在吗/大家在干嘛”这种空泛问题。"
            ),
            "share": (
                "分享一首歌、一句台词、一个冷知识或一个小发现，"
                "说一句你为什么想到它。"
            ),
            "meme": (
                "吐槽一个互联网现象或最近看到的梗，带一点自己的判断，"
                "不要只复述梗本身。"
            ),
        }
        instruction = (
            "\n\n现在群里已经安静了一阵，你想主动开口。\n"
            f"这次用「{mode}」的方式。{mode_prompts.get(mode, mode_prompts['thought'])}\n"
            "直接输出要发到群里的话：优先 1-2 个短句，最多 45 字；"
            "能一句说完就一句，不要写成小作文。\n"
            "禁止使用这些开场或套话：有人吗、在吗、群里有人吗、我好困、好困、"
            "来聊天、陪我聊、大家在干嘛。不要每句都问问题，不要客服腔。\n"
            "可以避开灾难、政治争议、低俗内容；不合适就换成自己的小观察。\n"
            "不要解释、不要加引号、不要 @ 人、不要提“主动发言/插话/我是 AI”。"
        )
        if last_texts:
            instruction += (
                "\n最近你已经主动发过这些，不要再重复或换汤不换药："
                + "；".join(text[:30] for text in last_texts)
            )
        persona = await self._idle_persona_prompt()
        system_prompt = (persona + "\n\n" if persona else "") + block + instruction
        try:
            provider = await self.context.get_using_provider_async(umo)
            if provider is None:
                return ""
            prompt = "（群里没有新消息。你想自己说一句。）"
            for attempt in range(2):
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        contexts=[],
                        system_prompt=system_prompt,
                        request_max_retries=1,
                    ),
                    timeout=45,
                )
                text = str(getattr(resp, "completion_text", "") or "").strip()
                text = text.strip("\"'“”‘’` ")
                text = re.sub(
                    r"^(大肥鱼|肥鱼|鱼鱼|深海在逃女仆)\s*[：:]\s*", "", text
                )
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if lines:
                    text = " ".join(lines[:2])
                text = text.strip()[:100]
                if "@" in text or "http" in text.lower():
                    text = ""
                if not self._idle_text_bad(text, last_texts):
                    return text
                prompt = (
                    "（上一次太像空泛问候或重复了。换一个具体话题，"
                    "不要问有没有人，不要再说好困。）"
                )
            return ""
        except Exception:
            logger.exception("[feiyu_state] idle group generate failed")
            return ""

    def _pick_bili_interest(self) -> str:
        keywords = [item[0] for item in BILI_INTERESTS]
        weights = [item[1] for item in BILI_INTERESTS]
        return random.choices(keywords, weights=weights, k=1)[0]

    def _bili_search_sync(self, keyword: str) -> list[dict]:
        from bilibili_api import search, sync

        result = sync(
            search.search_by_type(
                keyword,
                search_type=search.SearchObjectType.VIDEO,
                page=1,
                page_size=8,
            )
        )
        return [
            item
            for item in (result.get("result") or [])
            if isinstance(item, dict) and str(item.get("bvid") or "").startswith("BV")
        ]

    def _extract_bili_keyword(self, raw: str) -> str:
        # 优先识别明确的偏好词，避免“分享点奶蛙视频”被随机成动漫
        for keyword in sorted(
            (item[0] for item in BILI_INTERESTS), key=len, reverse=True
        ):
            if keyword in raw:
                return keyword
        match = re.search(
            r"(?:分享|来点|来个|推荐|刷到|发个|发点|整点)"
            r"(?:点|个|一些|几个)?\s*([^\s，。！？!?]{1,16}?)"
            r"(?:的)?(?:视频|影片|片子)",
            raw,
        )
        return match.group(1).strip() if match else ""

    def _extract_bili_mood(self, raw: str) -> str:
        for mood in ("感人", "搞笑", "抽象", "治愈", "燃", "下饭", "科普", "信息量"):
            if mood in raw:
                return mood
        return ""

    def _bili_credential(self):
        try:
            from bilibili_api import Credential

            path = (
                Path(__file__).resolve().parents[2]
                / "plugin_data"
                / "astrbot_plugin_bili_scroll"
                / "bili_credential.json"
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            return Credential(
                sessdata=data.get("sessdata"),
                bili_jct=data.get("bili_jct"),
                buvid3=data.get("buvid3"),
                dedeuserid=data.get("dedeuserid"),
                ac_time_value=data.get("ac_time_value"),
            )
        except Exception:
            return None

    def _fetch_bili_detail_sync(self, bvid: str, fallback: dict) -> dict:
        """读取视频简介 + 字幕节选，用内容本身做候选评估。"""
        item = {
            "bvid": bvid,
            "title": re.sub(r"<[^>]+>", "", str(fallback.get("title") or "")).strip(),
            "author": str(fallback.get("author") or "UP主")[:30],
            "desc": re.sub(
                r"<[^>]+>", "", str(fallback.get("description") or "")
            ).strip()[:500],
            "play": fallback.get("play") or 0,
            "duration": fallback.get("duration") or "",
            "subtitle_excerpt": "",
        }
        try:
            import httpx
            from bilibili_api import sync, video

            v = video.Video(bvid=bvid, credential=self._bili_credential())
            info = sync(v.get_info()) or {}
            if info.get("title"):
                item["title"] = str(info["title"])[:80]
            if info.get("desc"):
                item["desc"] = str(info["desc"])[:800]
            if isinstance(info.get("owner"), dict):
                item["author"] = str(info["owner"].get("name") or item["author"])[:30]
            if isinstance(info.get("stat"), dict):
                item["play"] = info["stat"].get("view") or item["play"]
            if info.get("duration"):
                item["duration"] = info["duration"]
            pages = info.get("pages") or []
            cid = pages[0].get("cid") if pages and isinstance(pages[0], dict) else None
            if cid:
                sub = sync(v.get_subtitle(cid=cid)) or {}
                tracks = sub.get("subtitles") or []
                track = next(
                    (
                        t
                        for t in tracks
                        if isinstance(t, dict) and t.get("subtitle_url")
                    ),
                    None,
                )
                if track:
                    url = str(track["subtitle_url"])
                    if url.startswith("//"):
                        url = "https:" + url
                    raw = httpx.get(url, timeout=10, follow_redirects=True).json()
                    body = raw.get("body") if isinstance(raw, dict) else raw
                    text = " ".join(
                        str(part.get("content") or "")
                        for part in (body or [])
                        if isinstance(part, dict)
                    )
                    item["subtitle_excerpt"] = text.strip()[:1800]
        except Exception:
            logger.debug("[feiyu_state] bili detail fetch failed: %s", bvid, exc_info=True)
        return item

    async def _judge_bili_candidates(
        self, candidates: list[dict], keyword: str, mood: str
    ) -> tuple[int, float, str]:
        lines = []
        for idx, item in enumerate(candidates):
            lines.append(
                f"[{idx}] 标题：{item.get('title')}\n"
                f"UP：{item.get('author')}｜播放：{item.get('play')}｜时长：{item.get('duration')}\n"
                f"简介：{item.get('desc') or '（无）'}\n"
                f"字幕节选：{item.get('subtitle_excerpt') or '（无字幕，只能看标题/简介）'}\n"
            )
        system = (
            "你是严格但公平的 B 站内容筛选器。用户要分享到 QQ 群，讨厌标题党、"
            "低信息量、纯情绪、搬运和广告。请从候选里选一条最值得分享的。\n"
            "评分看：内容是否具体、有信息量/观点/故事、是否标题党、是否适合群聊。\n"
            "只输出 JSON：{\"best\": 候选编号或-1, \"score\": 0-100, \"reason\": \"20字内理由\"}。\n"
            "如果全都很水、标题党或没有实质内容，best 输出 -1，score 输出最高分。"
        )
        prompt = (
            f"用户要求：{keyword or '按偏好随便看看'}"
            f"{'，希望偏「' + mood + '」' if mood else ''}\n\n"
            + "\n".join(lines)
            + "\n请只输出 JSON。"
        )
        try:
            provider = await self.context.get_using_provider_async(
                f"snowluma:GroupMessage:{next(iter(BILI_GROUP_IDS))}"
            )
            if provider is None:
                return -1, 0.0, ""
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=system,
                    request_max_retries=1,
                ),
                timeout=45,
            )
            raw = str(getattr(resp, "completion_text", "") or "")
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return -1, 0.0, ""
            data = json.loads(match.group(0))
            best = int(data.get("best", -1))
            score = float(data.get("score", 0) or 0)
            reason = str(data.get("reason") or "")[:40]
            if 0 <= best < len(candidates):
                return best, score, reason
            return -1, score, reason
        except Exception:
            logger.debug("[feiyu_state] bili candidate judge failed", exc_info=True)
            return -1, 0.0, ""

    async def _pick_bili_video(self, keyword: str = "", mood: str = "") -> dict:
        keywords = [keyword] if keyword else [
            self._pick_bili_interest() for _ in range(3)
        ]
        specific = {
            "奶蛙": ("奶蛙", "奶娃"),
            "明日方舟": ("明日方舟",),
            "DeepSeek鲸鱼娘": ("DeepSeek鲸鱼娘", "DeepSeek娘", "鲸鱼娘"),
            "DeepSeek娘": ("DeepSeek娘", "DeepSeek鲸鱼娘", "鲸鱼娘"),
            "AI鲸鱼娘": ("AI鲸鱼娘", "鲸鱼娘"),
            "深海在逃女仆": ("深海在逃女仆", "深海"),
        }
        for kw in keywords:
            try:
                items = await asyncio.to_thread(self._bili_search_sync, kw)
            except Exception:
                logger.debug("[feiyu_state] bili search failed", exc_info=True)
                items = []
            if not items:
                continue
            if kw in specific:
                variants = specific[kw]
                filtered = [
                    item
                    for item in items
                    if any(
                        variant
                        in (
                            re.sub(r"<[^>]+>", "", str(item.get("title") or ""))
                            + " "
                            + str(item.get("description") or "")
                        )
                        for variant in variants
                    )
                ]
                items = filtered
            if not items:
                continue
            candidates = items[:5]
            details = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self._fetch_bili_detail_sync,
                        str(candidate.get("bvid") or ""),
                        candidate,
                    )
                    for candidate in candidates
                ),
                return_exceptions=True,
            )
            evaluated = []
            for candidate, detail in zip(candidates, details):
                if isinstance(detail, dict):
                    evaluated.append(detail)
                else:
                    evaluated.append(
                        {
                            "bvid": str(candidate.get("bvid") or ""),
                            "title": re.sub(
                                r"<[^>]+>", "", str(candidate.get("title") or "")
                            ).strip(),
                            "author": str(candidate.get("author") or "UP主")[:30],
                            "desc": re.sub(
                                r"<[^>]+>",
                                "",
                                str(candidate.get("description") or ""),
                            ).strip()[:300],
                            "play": candidate.get("play") or 0,
                            "duration": candidate.get("duration") or "",
                            "subtitle_excerpt": "",
                        }
                    )
            best, score, reason = await self._judge_bili_candidates(
                evaluated, kw, mood
            )
            if best < 0 or score < 60:
                logger.info(
                    "[feiyu_state] bili candidates rejected: keyword=%s score=%.1f reason=%s",
                    kw,
                    score,
                    reason,
                )
                continue
            chosen = evaluated[best]
            chosen["keyword"] = kw
            chosen["judge_score"] = score
            chosen["judge_reason"] = reason
            return chosen
        return {}

    def _bili_quota_available(self, session: dict, requested: bool) -> bool:
        day = datetime.now().strftime("%Y-%m-%d")
        prefix = "bili_req" if requested else "bili_ambient"
        if session.get(f"{prefix}_day") != day:
            session[f"{prefix}_day"] = day
            session[f"{prefix}_count"] = 0
        limit = BILI_REQUEST_DAILY_MAX if requested else BILI_AMBIENT_DAILY_MAX
        return int(session.get(f"{prefix}_count") or 0) < limit

    def _bili_quota_mark(self, session: dict, requested: bool) -> None:
        prefix = "bili_req" if requested else "bili_ambient"
        session[f"{prefix}_count"] = int(session.get(f"{prefix}_count") or 0) + 1

    async def _generate_bili_feeling(self, item: dict) -> str:
        persona = await self._idle_persona_prompt()
        system = (
            (persona + "\n\n" if persona else "")
            + "你刚在 B 站刷到一个视频，想分享到 QQ 群里。"
            "用 1-2 个短句说你自己的感受、吐槽或为什么想分享，最多 45 字。"
            "不要复述标题，不要剧透，不要像广告或推荐文案，不要 @ 人，不要提链接。"
        )
        prompt = (
            f"标题：{item.get('title')}\n"
            f"UP主：{item.get('author')}\n"
            f"简介：{item.get('desc') or '（无）'}\n"
            f"内容评估：{item.get('judge_reason') or '（无）'}\n"
            f"字幕节选：{str(item.get('subtitle_excerpt') or '')[:600] or '（无）'}\n"
            "你会怎么随口分享？"
        )
        try:
            provider = await self.context.get_using_provider_async(
                f"snowluma:GroupMessage:{next(iter(BILI_GROUP_IDS))}"
            )
            if provider is None:
                return ""
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=system,
                    request_max_retries=1,
                ),
                timeout=30,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            text = text.strip("\"'“”‘’` ")
            text = re.sub(r"^(大肥鱼|肥鱼|鱼鱼|深海在逃女仆)\s*[：:]\s*", "", text)
            text = " ".join(text.splitlines()[:2]).strip()[:80]
            if "@" in text or "http" in text.lower():
                return ""
            return text
        except Exception:
            logger.debug("[feiyu_state] bili feeling failed", exc_info=True)
            return ""

    async def _share_bili_to_group(
        self, umo: str, gid: str, *, requested: bool, keyword: str = "", mood: str = ""
    ) -> bool:
        session = self._session(umo)
        if not self._bili_quota_available(session, requested):
            return False
        item = await self._pick_bili_video(keyword=keyword, mood=mood)
        if not item:
            if requested:
                try:
                    await self.context.send_message(
                        umo,
                        MessageChain(
                            [
                                Plain(
                                    (
                                        f"我看了几条「{keyword}」，都觉得一般，先不硬发。"
                                        if keyword
                                        else "我翻了几条，感觉都挺水的，先不硬发。"
                                    )
                                )
                            ]
                        ),
                    )
                except Exception:
                    logger.debug("[feiyu_state] bili no-result reply failed")
                return True
            return False
        feeling = await self._generate_bili_feeling(item)
        if not feeling:
            feeling = "这个看起来有点意思，感觉你们会喜欢。"
        text = f"{feeling}\n《{item['title']}》\nhttps://www.bilibili.com/video/{item['bvid']}"
        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception:
            logger.exception("[feiyu_state] bili group share failed")
            ok = False
        if ok:
            self._bili_quota_mark(session, requested)
            session["last_bot_ts"] = time.time()
            group_state = self._group_state(umo)
            shared = group_state.setdefault("bili_shared", [])
            shared.append(
                {
                    "bvid": item.get("bvid") or "",
                    "title": item.get("title") or "",
                    "url": f"https://www.bilibili.com/video/{item.get('bvid')}",
                    "ts": time.time(),
                }
            )
            del shared[:-10]
            self._record_group(umo, "我", text, True)
            logger.info(
                "[feiyu_state] bili share: group=%s requested=%s keyword=%s bvid=%s",
                gid,
                requested,
                item.get("keyword"),
                item.get("bvid"),
            )
            self._save(force=True)
        return ok

    def _is_bili_share_request(self, event: AstrMessageEvent, raw: str) -> bool:
        gid = str(event.get_group_id() or "")
        if not gid.isdigit() or int(gid) not in BILI_GROUP_IDS:
            return False
        if not (event.is_at_or_wake_command or any(a in raw for a in BOT_ALIASES)):
            return False
        return any(
            word in raw
            for word in ("分享", "来点", "来个", "推荐", "刷到", "发个", "发点", "整点")
        )

    async def _maybe_bili_share_request(
        self, event: AstrMessageEvent, raw: str
    ) -> bool:
        if not self._is_bili_share_request(event, raw):
            return False
        gid = str(event.get_group_id() or "")
        session = self._session(event.unified_msg_origin)
        if not self._bili_quota_available(session, requested=True):
            event.set_extra("feiyu_bili_no_share", True)
            return False
        keyword = self._extract_bili_keyword(raw)
        mood = self._extract_bili_mood(raw)
        return await self._share_bili_to_group(
            event.unified_msg_origin,
            gid,
            requested=True,
            keyword=keyword,
            mood=mood,
        )

    async def _semantic_should_reply(self, event: AstrMessageEvent, raw: str) -> bool:
        """用便宜模型判断这条未 @ 的群消息是不是在跟她说话。"""
        try:
            gid = str(event.get_group_id() or "")
            if not gid.isdigit() or int(gid) not in SEMANTIC_GROUP_IDS:
                return False
            if not (2 <= len(raw) <= 300):
                return False
            umo = event.unified_msg_origin
            session = self._session(umo)
            if time.time() - float(session.get("last_bot_ts") or 0) < SEMANTIC_COOLDOWN:
                return False
            if umo in self._classifying:
                return False

            self._classifying.add(umo)
            try:
                provider = await self.context.get_using_provider_async(umo)
                if provider is None:
                    return False
                history = (self.group_recent.get(umo) or [])[-SEMANTIC_MAX_CONTEXT:-1]
                context_text = (
                    "\n".join(
                        f"[{'我' if item.get('is_bot') else item.get('nick', '群友')}] "
                        f"{item.get('text', '')}"
                        for item in history
                    )
                    or "（无）"
                )
                prompt = (
                    f"最近群消息：\n{context_text}\n\n"
                    f"最后一条消息来自 [{event.get_sender_name() or '群友'}]：{raw}\n"
                    "这条消息是在对「我」（大肥鱼）说，或明显期待我接话吗？只输出 YES 或 NO。"
                )
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt=SEMANTIC_SYSTEM_PROMPT,
                        contexts=[],
                        request_max_retries=1,
                    ),
                    timeout=SEMANTIC_TIMEOUT,
                )
                answer = str(getattr(resp, "completion_text", "") or "").strip().upper()
                logger.debug(
                    "[feiyu_state] semantic judge group=%s answer=%s text=%r",
                    gid,
                    answer[:20],
                    raw[:40],
                )
                return answer.startswith("YES")
            finally:
                self._classifying.discard(umo)
        except Exception:
            logger.exception("[feiyu_state] semantic judge failed")
            return False

    async def _maybe_proactive(self, event: AstrMessageEvent, raw: str) -> bool:
        """低谷期偶尔主动插一句，只对白名单群生效。"""
        if not self._is_offpeak():
            return False
        gid = str(event.get_group_id() or "")
        if not gid.isdigit() or int(gid) not in PROACTIVE_GROUP_IDS:
            return False
        if len(raw) < 4:
            return False
        umo = event.unified_msg_origin
        session = self._session(umo)
        now = time.time()
        if now - float(session.get("last_bot_ts") or 0) < PROACTIVE_COOLDOWN:
            return False
        if now - float(session.get("last_proactive_ts") or 0) < PROACTIVE_COOLDOWN:
            return False
        recent = [
            item
            for item in (self.group_recent.get(umo) or [])
            if not item.get("is_bot") and now - float(item.get("ts") or 0) <= 300
        ]
        if len(recent) < PROACTIVE_MIN_RECENT:
            return False
        if random.random() >= PROACTIVE_CHANCE:
            return False

        session["last_proactive_ts"] = now
        event.set_extra("feiyu_proactive", True)
        event.is_wake = True
        event.is_at_or_wake_command = True
        logger.info(
            "[feiyu_state] proactive wake: group=%s text=%r",
            gid,
            raw[:60],
        )
        return True

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def maybe_ignore(self, event: AstrMessageEvent) -> None:
        """让角色自己判断这条消息值不值得回。"""
        try:
            self._ensure_idle_task()
            self._ensure_reflection_task()
            if event.is_stopped():
                return
            if event.get_platform_name() == "webchat":
                return
            if event.get_extra("handlers_parsed_params"):
                return
            raw = str(
                getattr(getattr(event, "message_obj", None), "message_str", "")
                or event.message_str
                or ""
            ).strip()
            if not raw or raw.startswith("/"):
                return
            if any(word in raw for word in ("别回", "不用回", "闭嘴")):
                event.stop_event()
                return

            is_group = bool(event.get_group_id())
            if is_group:
                uid = str(event.get_sender_id() or "")
                nick = str(event.get_sender_name() or uid or "群友")
                self._record_group(event.unified_msg_origin, nick, raw, False, uid)
                if await self._maybe_bili_share_request(event, raw):
                    event.stop_event()
                    return
                # @ / 引用回复 / 前缀 已经由 AstrBot 唤醒，不用再判断
                if event.is_at_or_wake_command:
                    return
                # 高峰期只回应 @ / 引用 / 指令，不做语义追踪
                if not self._is_offpeak():
                    logger.debug("[feiyu_state] peak hours: skip non-@ group message")
                    return
                if any(alias in raw for alias in BOT_ALIASES):
                    event.is_wake = True
                    event.is_at_or_wake_command = True
                    logger.info(
                        "[feiyu_state] alias wake: group=%s text=%r",
                        event.get_group_id(),
                        raw[:60],
                    )
                    return
                if await self._semantic_should_reply(event, raw):
                    event.is_wake = True
                    event.is_at_or_wake_command = True
                    logger.info(
                        "[feiyu_state] semantic wake: group=%s text=%r",
                        event.get_group_id(),
                        raw[:60],
                    )
                    return
                await self._maybe_proactive(event, raw)
                return

            if not event.get_group_id() and self._companion_for_event(event):
                return

            # 私聊不做“短句随机装没看见”：私聊消息一律进入正常回复链。
            # 群聊保留冷读/省略，避免刷屏；私聊静默会让人误以为坏了。
            if not event.get_group_id():
                return

            # 私聊：短回应 / 连续对话时的忽略
            if any(hint in raw for hint in ALWAYS_REPLY_HINTS):
                return
            if any(hint in raw for hint in QUESTION_HINTS):
                return
            if len(raw) >= 8:
                return

            session = self._session(event.unified_msg_origin)
            recent = (
                time.time() - float(session.get("last_bot_ts") or 0)
            ) < REPLY_GAP_WINDOW
            if raw in BACKCHANNEL:
                if recent and random.random() < 0.8:
                    event.stop_event()
                return
            if len(raw) <= 3 and random.random() < 0.3:
                event.stop_event()
        except Exception:
            logger.exception("[feiyu_state] reply gate failed")

    @filter.on_decorating_result()
    async def humanize_send(self, event: AstrMessageEvent) -> None:
        """发送前加人类式停顿，并偶尔附一张表情包。"""
        try:
            result = event.get_result()
            if result is None or not result.chain or not result.is_llm_result():
                return
            text = "".join(
                comp.text for comp in result.chain if isinstance(comp, Plain)
            )
            if not text.strip():
                return
            if event.get_group_id():
                self._record_group(event.unified_msg_origin, "我", text, True)

            if not event.get_group_id() and self._companion_for_event(event):
                self._session(event.unified_msg_origin)["last_bot_ts"] = time.time()
                self._save(force=True)
                return

            delay = 1.0 + min(3.2, len(text) * 0.015) + random.uniform(0.0, 1.1)
            session = self._session(event.unified_msg_origin)
            if float(session.get("energy", 0.65)) < 0.35:
                delay += 0.8
            if datetime.now().hour < 6:
                delay += 1.0
            await asyncio.sleep(min(6.0, max(0.6, delay)))

            result = event.get_result()
            if result is None or not result.chain:
                return
            session["last_bot_ts"] = time.time()
            now = time.time()
            if (
                self.stickers
                and now - float(session.get("last_sticker_ts") or 0) > STICKER_COOLDOWN
                and random.random() < STICKER_CHANCE
                and not any(isinstance(comp, Image) for comp in result.chain)
            ):
                sticker = self._pick_sticker(session)
                if sticker:
                    result.chain.append(Image.fromFileSystem(str(sticker)))
                    session["last_sticker_ts"] = now
            self._save(force=True)
        except Exception:
            logger.exception("[feiyu_state] humanize send failed")

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            self._ensure_idle_task()
            self._ensure_reflection_task()
            session = self._update(event)
            if not event.get_group_id() and self._companion_for_event(event):
                uid = str(event.get_sender_id() or "")
                user = self._user(event.unified_msg_origin, uid)
                lines = [
                    "[已有私聊记忆｜仅与本人交谈时参考，不向其他人透露；当前情绪和日程由陪伴插件管理]",
                    "聊天优先短句：一次 1–2 句，能一句说完就一句；不要写成客服式长回复。",
                    "想刷 B 站、找视频、看视频信息或评论时，可以调用 bilibili_recommend / bilibili_search / bilibili_video_info；没真正看过就不要装看过。",
                ]
                if uid not in MASTER_IDS:
                    lines.append(
                        "这个人不是你的主人；不要叫他主人，用昵称或‘你’称呼。"
                    )
                if user.get("impression"):
                    lines.append("已有印象：" + str(user["impression"]))
                traits = [str(x) for x in (user.get("traits") or []) if str(x).strip()]
                topics = [str(x) for x in (user.get("topics") or []) if str(x).strip()]
                if traits:
                    lines.append("特点：" + "、".join(traits[:4]))
                if topics:
                    lines.append("常聊：" + "、".join(topics[:4]))
                notes = [
                    n
                    for n in (user.get("notes") or [])
                    if isinstance(n, str) and n.strip()
                ]
                if notes:
                    lines.append("对方明确要求记住：" + "；".join(notes[:MAX_NOTES]))
                if user.get("show_resume") and user.get("resume_msg"):
                    lines.append("上次聊到：" + str(user["resume_msg"]))
                lines.extend(self._cross_chat_lines(user))
                lines.extend(self._group_context_for_private(uid))
                lines.extend(self._reflection_lines(user, user.get("nick") or "对方"))
                req.system_prompt = f"{req.system_prompt or ''}\n\n" + "\n".join(lines)
                return
            shared_state = None
            if event.get_group_id():
                companion = self._companion_for_event(event)
                if companion is not None:
                    data = getattr(companion, "data", {})
                    state = (
                        data.get("daily_state", {}) if isinstance(data, dict) else {}
                    )
                    if isinstance(state, dict) and state:
                        shared_state = {
                            "mood_bias": state.get("mood_bias", "平稳"),
                            "energy": state.get("energy", 75),
                            "reply_style_prompt": getattr(
                                companion, "reply_style_prompt", ""
                            ),
                        }
            block = self._render(event, session, shared_state=shared_state)
            if event.get_extra("feiyu_proactive"):
                block += (
                    "\n\n这次不是别人叫你，而是你自己看气氛主动插话。"
                    "保持简短自然，一到两句就够；不要总结、不要说教、不要连续刷屏。"
                )
            req.system_prompt = f"{req.system_prompt or ''}\n\n{block}"
        except Exception:
            logger.exception("[feiyu_state] failed to inject state")

    # ---------- commands ----------

    @filter.command("记住", alias={"remember"})
    async def remember(self, event: AstrMessageEvent, text: GreedyStr) -> None:
        content = str(text or "").strip()
        if not content:
            yield event.plain_result("要记什么？说清楚点。")
            return
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        user = self._user(umo, uid)
        notes = user.setdefault("notes", [])
        note = content[:NOTE_MAX_CHARS]
        if note in notes:
            yield event.plain_result("这个已经记过了。")
            return
        notes.append(note)
        while len(notes) > MAX_NOTES:
            notes.pop(0)
        self._save(force=True)
        if event.get_group_id():
            yield event.plain_result("行，记下了。")
        else:
            yield event.plain_result(f"行，记下了：{note}")

    @filter.command("忘掉", alias={"forget"})
    async def forget(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        user = self._user(umo, uid)
        user["notes"] = []
        self._save(force=True)
        yield event.plain_result("行吧，都忘了。")

    @filter.command("印象", alias={"impression"})
    async def impression(self, event: AstrMessageEvent) -> None:
        if event.get_group_id():
            return
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        user = self._user(umo, uid)
        notes = user.get("notes") or []
        profile = str(user.get("impression") or "").strip()
        traits = [str(x) for x in (user.get("traits") or []) if str(x).strip()]
        if not notes and not profile:
            yield event.plain_result("暂时没记你什么。")
            return
        parts = []
        if profile:
            parts.append("印象：" + profile)
        if traits:
            parts.append("特点：" + "、".join(traits[:4]))
        if notes:
            parts.append("记得：" + "；".join(notes))
        yield event.plain_result("\n".join(parts))

    @filter.command("表情", alias={"sticker"})
    async def send_sticker(
        self, event: AstrMessageEvent, name: str | None = None
    ) -> None:
        if not self.stickers:
            yield event.plain_result("表情包还没准备好。")
            return
        key = (name or "").strip()
        path = self.stickers.get(key)
        if path is None:
            session = self._session(event.unified_msg_origin)
            path = self._pick_sticker(session)
        if path is None:
            yield event.plain_result("没找到这张表情。")
            return
        yield event.image_result(str(path))

    @filter.command("状态", alias={"mood"})
    async def show_state(self, event: AstrMessageEvent) -> None:
        if event.get_group_id():
            return
        umo = event.unified_msg_origin
        session = self._session(umo)
        yield event.plain_result(
            "心情={mood} 精力={energy:.2f} 毒舌={sarcasm:.2f} 好感={affection:.2f} 场景={mode}".format(
                mood=session.get("mood", "lazy"),
                energy=float(session.get("energy", 0.65)),
                sarcasm=float(session.get("sarcasm", 0.5)),
                affection=float(session.get("affection", 0.5)),
                mode=session.get("mode", "casual"),
            )
        )
