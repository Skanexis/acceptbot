from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command
from aiogram.types import CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from dotenv import load_dotenv


ID_DATE_ANCHORS: list[tuple[int, datetime]] = [
    (1, datetime(2013, 8, 14, tzinfo=timezone.utc)),
    (1_000_000_000, datetime(2017, 1, 1, tzinfo=timezone.utc)),
    (2_000_000_000, datetime(2019, 1, 1, tzinfo=timezone.utc)),
    (3_000_000_000, datetime(2021, 1, 1, tzinfo=timezone.utc)),
    (4_000_000_000, datetime(2022, 7, 1, tzinfo=timezone.utc)),
    (5_000_000_000, datetime(2023, 7, 1, tzinfo=timezone.utc)),
    (6_000_000_000, datetime(2024, 7, 1, tzinfo=timezone.utc)),
    (7_000_000_000, datetime(2025, 7, 1, tzinfo=timezone.utc)),
]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    channel_id: int
    admin_ids: set[int]
    db_path: str
    min_account_age_days: int
    max_captcha_attempts: int
    risk_score_to_admin: int
    risk_score_to_hard_captcha: int
    hard_captcha_attempts: int

    @staticmethod
    def from_env() -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        channel_raw = os.getenv("CHANNEL_ID", "").strip()
        admin_raw = os.getenv("ADMIN_IDS", "").strip()
        db_path = os.getenv("DB_PATH", "join_guard.db").strip()
        min_account_age_days = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "30"))
        max_captcha_attempts = int(os.getenv("MAX_CAPTCHA_ATTEMPTS", "3"))
        risk_score_to_admin = int(os.getenv("RISK_SCORE_TO_ADMIN", "7"))
        risk_score_to_hard_captcha = int(os.getenv("RISK_SCORE_TO_HARD_CAPTCHA", "4"))
        hard_captcha_attempts = int(os.getenv("HARD_CAPTCHA_ATTEMPTS", "1"))

        if not bot_token:
            raise ValueError("BOT_TOKEN is required")
        if not channel_raw:
            raise ValueError("CHANNEL_ID is required")
        if not admin_raw:
            raise ValueError("ADMIN_IDS is required (comma-separated user IDs)")

        admin_ids: set[int] = set()
        for raw in admin_raw.split(","):
            clean = raw.strip()
            if not clean:
                continue
            admin_ids.add(int(clean))

        if not admin_ids:
            raise ValueError("At least one admin ID must be provided in ADMIN_IDS")
        if max_captcha_attempts < 1:
            raise ValueError("MAX_CAPTCHA_ATTEMPTS must be >= 1")
        if hard_captcha_attempts < 1:
            raise ValueError("HARD_CAPTCHA_ATTEMPTS must be >= 1")
        if risk_score_to_hard_captcha < 0:
            raise ValueError("RISK_SCORE_TO_HARD_CAPTCHA must be >= 0")
        if risk_score_to_admin <= risk_score_to_hard_captcha:
            raise ValueError("RISK_SCORE_TO_ADMIN must be greater than RISK_SCORE_TO_HARD_CAPTCHA")

        return Settings(
            bot_token=bot_token,
            channel_id=int(channel_raw),
            admin_ids=admin_ids,
            db_path=db_path,
            min_account_age_days=min_account_age_days,
            max_captcha_attempts=max_captcha_attempts,
            risk_score_to_admin=risk_score_to_admin,
            risk_score_to_hard_captcha=risk_score_to_hard_captcha,
            hard_captcha_attempts=hard_captcha_attempts,
        )


class Storage:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS join_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT NOT NULL,
                last_name TEXT,
                submitted_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                is_suspicious INTEGER NOT NULL DEFAULT 0,
                estimated_age_days INTEGER,
                risk_score INTEGER NOT NULL DEFAULT 0,
                risk_reasons TEXT,
                captcha_question TEXT,
                captcha_answer INTEGER,
                captcha_attempts INTEGER NOT NULL DEFAULT 0,
                captcha_max_attempts INTEGER NOT NULL DEFAULT 3,
                captcha_difficulty TEXT NOT NULL DEFAULT 'normal',
                decision_by INTEGER,
                decision_at INTEGER,
                reason TEXT,
                UNIQUE(channel_id, user_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._migrate_join_requests_schema()
        self.conn.commit()

    def _migrate_join_requests_schema(self) -> None:
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(join_requests)").fetchall()
        }

        if "risk_score" not in columns:
            self.conn.execute("ALTER TABLE join_requests ADD COLUMN risk_score INTEGER NOT NULL DEFAULT 0")
        if "risk_reasons" not in columns:
            self.conn.execute("ALTER TABLE join_requests ADD COLUMN risk_reasons TEXT")
        if "captcha_max_attempts" not in columns:
            self.conn.execute("ALTER TABLE join_requests ADD COLUMN captcha_max_attempts INTEGER NOT NULL DEFAULT 3")
        if "captcha_difficulty" not in columns:
            self.conn.execute("ALTER TABLE join_requests ADD COLUMN captcha_difficulty TEXT NOT NULL DEFAULT 'normal'")

    def create_or_refresh_request(self, join_request: ChatJoinRequest) -> int:
        user = join_request.from_user
        now = int(datetime.now(tz=timezone.utc).timestamp())

        self.conn.execute(
            """
            INSERT INTO join_requests (
                channel_id, user_id, user_chat_id, username, first_name, last_name, submitted_at,
                status, is_suspicious, estimated_age_days, risk_score, risk_reasons, captcha_question, captcha_answer,
                captcha_attempts, captcha_max_attempts, captcha_difficulty, decision_by, decision_at, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'new', 0, NULL, 0, NULL, NULL, NULL, 0, 3, 'normal', NULL, NULL, NULL)
            ON CONFLICT(channel_id, user_id) DO UPDATE SET
                user_chat_id=excluded.user_chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                submitted_at=excluded.submitted_at,
                status='new',
                is_suspicious=0,
                estimated_age_days=NULL,
                risk_score=0,
                risk_reasons=NULL,
                captcha_question=NULL,
                captcha_answer=NULL,
                captcha_attempts=0,
                captcha_max_attempts=3,
                captcha_difficulty='normal',
                decision_by=NULL,
                decision_at=NULL,
                reason=NULL
            """,
            (
                join_request.chat.id,
                user.id,
                join_request.user_chat_id,
                user.username,
                user.first_name,
                user.last_name,
                now,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM join_requests WHERE channel_id = ? AND user_id = ?",
            (join_request.chat.id, user.id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to save join request")
        return int(row["id"])

    def set_risk_profile(
        self,
        request_id: int,
        estimated_age_days: int,
        risk_score: int,
        reasons: list[str],
    ) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET estimated_age_days = ?,
                risk_score = ?,
                risk_reasons = ?
            WHERE id = ?
            """,
            (estimated_age_days, risk_score, serialize_risk_reasons(reasons), request_id),
        )
        self.conn.commit()

    def mark_pending_admin(self, request_id: int, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET status = 'pending_admin',
                is_suspicious = 1,
                reason = ?
            WHERE id = ?
            """,
            (reason, request_id),
        )
        self.conn.commit()

    def mark_pending_captcha(
        self,
        request_id: int,
        question: str,
        answer: int,
        max_attempts: int,
        difficulty: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET status = 'pending_captcha',
                is_suspicious = CASE WHEN ? = 'hard' THEN 1 ELSE 0 END,
                captcha_question = ?,
                captcha_answer = ?,
                captcha_attempts = 0,
                captcha_max_attempts = ?,
                captcha_difficulty = ?,
                reason = NULL
            WHERE id = ?
            """,
            (difficulty, question, answer, max_attempts, difficulty, request_id),
        )
        self.conn.commit()

    def refresh_captcha(self, request_id: int, question: str, answer: int) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET captcha_question = ?,
                captcha_answer = ?
            WHERE id = ?
            """,
            (question, answer, request_id),
        )
        self.conn.commit()

    def increment_captcha_attempts(self, request_id: int) -> int:
        self.conn.execute(
            "UPDATE join_requests SET captcha_attempts = captcha_attempts + 1 WHERE id = ?",
            (request_id,),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT captcha_attempts FROM join_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise RuntimeError("Request not found while incrementing attempts")
        return int(row["captcha_attempts"])

    def complete(self, request_id: int, status: str, decision_by: int | None, reason: str) -> None:
        now = int(datetime.now(tz=timezone.utc).timestamp())
        self.conn.execute(
            """
            UPDATE join_requests
            SET status = ?,
                decision_by = ?,
                decision_at = ?,
                reason = ?
            WHERE id = ?
            """,
            (status, decision_by, now, reason, request_id),
        )
        self.conn.commit()

    def get(self, request_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone()

    def list_pending_admin(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM join_requests
            WHERE status = 'pending_admin'
            ORDER BY submitted_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def list_recent_decisions(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM join_requests
            WHERE decision_at IS NOT NULL
            ORDER BY decision_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def get_status_stats(self, since_ts: int) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM join_requests
            WHERE submitted_at >= ?
            GROUP BY status
            """,
            (since_ts,),
        ).fetchall()
        stats: dict[str, int] = {}
        for row in rows:
            stats[str(row["status"])] = int(row["cnt"])
        return stats

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_moderation_mode(self) -> str:
        value = self.get_setting("moderation_mode")
        if value not in {"hybrid", "manual"}:
            return "hybrid"
        return value

    def set_moderation_mode(self, mode: str) -> None:
        if mode not in {"hybrid", "manual"}:
            raise ValueError("Unsupported moderation mode")
        self.set_setting("moderation_mode", mode)

    def close(self) -> None:
        self.conn.close()


def estimate_created_at_from_user_id(user_id: int) -> datetime:
    if user_id <= ID_DATE_ANCHORS[0][0]:
        return ID_DATE_ANCHORS[0][1]

    for index in range(len(ID_DATE_ANCHORS) - 1):
        left_id, left_date = ID_DATE_ANCHORS[index]
        right_id, right_date = ID_DATE_ANCHORS[index + 1]
        if left_id <= user_id <= right_id:
            ratio = (user_id - left_id) / (right_id - left_id)
            left_ts = left_date.timestamp()
            right_ts = right_date.timestamp()
            current_ts = left_ts + ratio * (right_ts - left_ts)
            return datetime.fromtimestamp(current_ts, tz=timezone.utc)

    return ID_DATE_ANCHORS[-1][1]


def estimate_account_age_days(user_id: int) -> int:
    created_at = estimate_created_at_from_user_id(user_id)
    age_days = (datetime.now(tz=timezone.utc) - created_at).days
    return max(0, age_days)


@dataclass(frozen=True)
class RiskAssessment:
    estimated_age_days: int
    score: int
    reasons: list[str]


SPAM_BIO_PATTERN = re.compile(
    r"(t\.me/|telegram\.me/|https?://|airdrop|crypto|profit|casino|scommesse|pump|guadagno)",
    flags=re.IGNORECASE,
)
SUSPICIOUS_NAME_PATTERN = re.compile(r"(\d{4,}|(.)\2{3,})")


def name_looks_suspicious(user: User) -> bool:
    full_name = f"{user.first_name} {user.last_name or ''}".strip().lower()
    compact = full_name.replace(" ", "")
    if len(compact) < 4:
        return True
    return bool(SUSPICIOUS_NAME_PATTERN.search(compact))


def serialize_risk_reasons(reasons: list[str]) -> str:
    return json.dumps(reasons, ensure_ascii=False)


def deserialize_risk_reasons(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        return [raw]
    return [raw]


def build_risk_summary(score: int, reasons: list[str]) -> str:
    if not reasons:
        return f"risk_score={score}; reasons=none"
    joined = "; ".join(reasons)
    return f"risk_score={score}; reasons={joined}"


def generate_captcha(difficulty: str = "normal") -> tuple[str, int, list[int]]:
    if difficulty == "hard":
        first = random.randint(7, 19)
        second = random.randint(3, 13)
        operation = random.choice(["+", "-", "*"])
    else:
        first = random.randint(2, 12)
        second = random.randint(1, 9)
        operation = random.choice(["+", "-"])

    if operation == "-" and second > first:
        first, second = second, first

    if operation == "+":
        answer = first + second
    elif operation == "-":
        answer = first - second
    else:
        answer = first * second

    options = {answer}
    noise_limit = 20 if difficulty == "hard" else 7

    while len(options) < 4:
        options.add(answer + random.randint(-noise_limit, noise_limit))

    options_list = list(options)
    random.shuffle(options_list)
    question = f"{first} {operation} {second} = ?"
    return question, answer, options_list


def build_captcha_keyboard(request_id: int, options: list[int]) -> InlineKeyboardMarkup:
    first_row = [
        InlineKeyboardButton(text=str(options[0]), callback_data=f"cap:{request_id}:{options[0]}"),
        InlineKeyboardButton(text=str(options[1]), callback_data=f"cap:{request_id}:{options[1]}"),
    ]
    second_row = [
        InlineKeyboardButton(text=str(options[2]), callback_data=f"cap:{request_id}:{options[2]}"),
        InlineKeyboardButton(text=str(options[3]), callback_data=f"cap:{request_id}:{options[3]}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[first_row, second_row])


def build_admin_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Approva", callback_data=f"adm:approve:{request_id}"),
                InlineKeyboardButton(text="Rifiuta", callback_data=f"adm:decline:{request_id}"),
            ]
        ]
    )


def build_admin_menu_keyboard(mode: str) -> InlineKeyboardMarkup:
    mode_label = "Manuale" if mode == "manual" else "Ibrido"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Dashboard", callback_data="panel:dashboard"),
                InlineKeyboardButton(text="Pending", callback_data="panel:pending"),
            ],
            [
                InlineKeyboardButton(text="Canale", callback_data="panel:channel"),
                InlineKeyboardButton(text=f"Modo: {mode_label}", callback_data="panel:toggle_mode"),
            ],
        ]
    )


def build_pending_actions_keyboard(requests: list[sqlite3.Row], mode: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request in requests:
        request_id = int(request["id"])
        rows.append(
            [
                InlineKeyboardButton(text=f"Approva #{request_id}", callback_data=f"adm:approve:{request_id}"),
                InlineKeyboardButton(text=f"Rifiuta #{request_id}", callback_data=f"adm:decline:{request_id}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Aggiorna", callback_data="panel:pending"),
            InlineKeyboardButton(text="Dashboard", callback_data="panel:dashboard"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"Modo: {'Manuale' if mode == 'manual' else 'Ibrido'}",
                callback_data="panel:toggle_mode",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_captcha_callback(data: str) -> tuple[int, int] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    if parts[0] != "cap":
        return None

    try:
        request_id = int(parts[1])
        answer = int(parts[2])
    except ValueError:
        return None
    return request_id, answer


def parse_admin_callback(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    if parts[0] != "adm":
        return None
    action = parts[1]
    if action not in {"approve", "decline"}:
        return None

    try:
        request_id = int(parts[2])
    except ValueError:
        return None

    return action, request_id


def parse_panel_callback(data: str) -> str | None:
    parts = data.split(":")
    if len(parts) != 2:
        return None
    if parts[0] != "panel":
        return None
    action = parts[1]
    if action not in {"dashboard", "pending", "channel", "toggle_mode"}:
        return None
    return action


class JoinGuardBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.bot = Bot(settings.bot_token)
        self.dp = Dispatcher()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.dp.message.register(self.on_start, CommandStart())
        self.dp.message.register(self.on_admin_menu, Command("admin"))
        self.dp.message.register(self.on_stats_command, Command("stats"))
        self.dp.message.register(self.on_pending_command, Command("pending"))
        self.dp.message.register(self.on_channel_command, Command("channel"))
        self.dp.chat_join_request.register(self.on_join_request)
        self.dp.callback_query.register(self.on_captcha_callback, F.data.startswith("cap:"))
        self.dp.callback_query.register(self.on_admin_callback, F.data.startswith("adm:"))
        self.dp.callback_query.register(self.on_panel_callback, F.data.startswith("panel:"))

    def _is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids

    async def on_start(self, message: Message) -> None:
        if self._is_admin(message.from_user.id if message.from_user else None):
            await message.answer(
                "Pannello admin disponibile.\n"
                "Comandi: /admin, /stats, /pending, /channel."
            )
            return
        await message.answer("Il bot e attivo. Se hai inviato una richiesta al canale, segui le istruzioni in chat.")

    async def on_admin_menu(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            await message.answer("Non hai i permessi admin.")
            return
        mode = self.storage.get_moderation_mode()
        await message.answer(self._build_dashboard_text(), reply_markup=build_admin_menu_keyboard(mode))

    async def on_stats_command(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            await message.answer("Non hai i permessi admin.")
            return
        mode = self.storage.get_moderation_mode()
        await message.answer(self._build_dashboard_text(), reply_markup=build_admin_menu_keyboard(mode))

    async def on_pending_command(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            await message.answer("Non hai i permessi admin.")
            return
        pending = self.storage.list_pending_admin(limit=8)
        mode = self.storage.get_moderation_mode()
        await message.answer(self._build_pending_text(pending), reply_markup=build_pending_actions_keyboard(pending, mode))

    async def on_channel_command(self, message: Message) -> None:
        if not self._is_admin(message.from_user.id if message.from_user else None):
            await message.answer("Non hai i permessi admin.")
            return
        mode = self.storage.get_moderation_mode()
        await message.answer(await self._build_channel_text(), reply_markup=build_admin_menu_keyboard(mode))

    async def on_panel_callback(self, callback: CallbackQuery) -> None:
        if not callback.data:
            await callback.answer("Azione non valida.", show_alert=True)
            return
        if not self._is_admin(callback.from_user.id if callback.from_user else None):
            await callback.answer("Non hai i permessi admin.", show_alert=True)
            return

        action = parse_panel_callback(callback.data)
        if action is None:
            await callback.answer("Azione non valida.", show_alert=True)
            return

        if action == "toggle_mode":
            current_mode = self.storage.get_moderation_mode()
            next_mode = "manual" if current_mode == "hybrid" else "hybrid"
            self.storage.set_moderation_mode(next_mode)
            logging.info("moderation_mode_changed by=%s mode=%s", callback.from_user.id, next_mode)
            await callback.answer(f"Modo impostato: {next_mode}")
            action = "dashboard"
        else:
            await callback.answer()

        if not callback.message:
            return

        mode = self.storage.get_moderation_mode()
        if action == "dashboard":
            text = self._build_dashboard_text()
            markup = build_admin_menu_keyboard(mode)
        elif action == "pending":
            pending = self.storage.list_pending_admin(limit=8)
            text = self._build_pending_text(pending)
            markup = build_pending_actions_keyboard(pending, mode)
        else:
            text = await self._build_channel_text()
            markup = build_admin_menu_keyboard(mode)

        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def assess_risk(self, user: User) -> RiskAssessment:
        score = 0
        reasons: list[str] = []
        estimated_age_days = estimate_account_age_days(user.id)

        reasons.append(f"eta_account_stimata={estimated_age_days}g")
        if user.is_bot:
            score += 10
            reasons.append("account_bot")
        if estimated_age_days < self.settings.min_account_age_days:
            score += 5
            reasons.append(
                f"eta_sotto_soglia({estimated_age_days}<{self.settings.min_account_age_days})"
            )
        if not user.username:
            score += 3
            reasons.append("username_assente")
        if name_looks_suspicious(user):
            score += 2
            reasons.append("nome_sospetto")

        has_photo = await self._has_profile_photo(user.id)
        if has_photo is False:
            score += 2
            reasons.append("foto_profilo_assente")

        bio = await self._fetch_user_bio(user.id)
        if bio and SPAM_BIO_PATTERN.search(bio):
            score += 3
            reasons.append("bio_con_pattern_spam")

        return RiskAssessment(estimated_age_days=estimated_age_days, score=score, reasons=reasons)

    async def _has_profile_photo(self, user_id: int) -> bool | None:
        try:
            photos = await self.bot.get_user_profile_photos(user_id=user_id, limit=1)
            return photos.total_count > 0
        except TelegramBadRequest as exc:
            logging.info("risk_signal_photo_unavailable user_id=%s err=%s", user_id, str(exc))
            return None

    async def _fetch_user_bio(self, user_id: int) -> str | None:
        try:
            user_chat = await self.bot.get_chat(chat_id=user_id)
            bio = getattr(user_chat, "bio", None)
            if bio:
                return str(bio)
        except TelegramBadRequest as exc:
            logging.info("risk_signal_bio_unavailable user_id=%s err=%s", user_id, str(exc))
        return None

    async def on_join_request(self, join_request: ChatJoinRequest) -> None:
        if join_request.chat.id != self.settings.channel_id:
            return

        request_id = self.storage.create_or_refresh_request(join_request)
        user = join_request.from_user
        mode = self.storage.get_moderation_mode()

        await self.safe_send_user_message(
            join_request.user_chat_id,
            "La tua richiesta e stata ricevuta. Ora il bot controllera l'account e ti inviera il prossimo passaggio.",
        )

        risk = await self.assess_risk(user)
        self.storage.set_risk_profile(
            request_id=request_id,
            estimated_age_days=risk.estimated_age_days,
            risk_score=risk.score,
            reasons=risk.reasons,
        )
        logging.info(
            "join_request_received request_id=%s user_id=%s channel_id=%s mode=%s %s",
            request_id,
            user.id,
            join_request.chat.id,
            mode,
            build_risk_summary(risk.score, risk.reasons),
        )

        force_admin = mode == "manual" or risk.score >= self.settings.risk_score_to_admin
        if force_admin:
            route_reason = "manual_mode" if mode == "manual" else "risk_threshold_admin"
            self.storage.mark_pending_admin(
                request_id,
                f"route={route_reason}; {build_risk_summary(risk.score, risk.reasons)}",
            )
            await self.notify_admins(join_request, request_id, risk, route_reason)
            await self.safe_send_user_message(
                join_request.user_chat_id,
                "La richiesta e stata inviata a un amministratore per verifica manuale. Ti aggiorneremo in questa chat.",
            )
            return

        if risk.score >= self.settings.risk_score_to_hard_captcha:
            max_attempts = min(self.settings.max_captcha_attempts, self.settings.hard_captcha_attempts)
            question, answer, options = generate_captcha(difficulty="hard")
            self.storage.mark_pending_captcha(
                request_id=request_id,
                question=question,
                answer=answer,
                max_attempts=max_attempts,
                difficulty="hard",
            )
            await self.safe_send_user_message(
                join_request.user_chat_id,
                (
                    "Controllo avanzato attivato.\n"
                    f"Tentativi disponibili: {max_attempts}\n"
                    f"Risolvi il captcha:\n{question}"
                ),
                reply_markup=build_captcha_keyboard(request_id, options),
            )
            return

        question, answer, options = generate_captcha(difficulty="normal")
        self.storage.mark_pending_captcha(
            request_id=request_id,
            question=question,
            answer=answer,
            max_attempts=self.settings.max_captcha_attempts,
            difficulty="normal",
        )
        await self.safe_send_user_message(
            join_request.user_chat_id,
            (
                "Controllo account completato.\n"
                f"Risolvi il captcha per entrare nel canale:\n{question}"
            ),
            reply_markup=build_captcha_keyboard(request_id, options),
        )

    async def on_captcha_callback(self, callback: CallbackQuery) -> None:
        if not callback.data:
            await callback.answer("Captcha non valido.", show_alert=True)
            return

        parsed = parse_captcha_callback(callback.data)
        if parsed is None:
            await callback.answer("Captcha non valido.", show_alert=True)
            return

        request_id, selected_answer = parsed
        request = self.storage.get(request_id)
        if request is None:
            await callback.answer("Richiesta non trovata.", show_alert=True)
            return

        if int(request["user_id"]) != callback.from_user.id:
            await callback.answer("Questo captcha non e tuo.", show_alert=True)
            return

        if request["status"] != "pending_captcha":
            await callback.answer("La richiesta e gia stata elaborata.", show_alert=True)
            return

        max_attempts = int(request["captcha_max_attempts"] or self.settings.max_captcha_attempts)
        captcha_difficulty = str(request["captcha_difficulty"] or "normal")
        expected_answer = int(request["captcha_answer"])
        if selected_answer == expected_answer:
            approved = await self.try_approve_request(request)
            if approved:
                self.storage.complete(request_id, "approved", None, f"captcha_passed:{captcha_difficulty}")
                if callback.message:
                    await callback.message.edit_text("Captcha superato. Richiesta approvata, accesso al canale aperto.")
                await callback.answer("Richiesta approvata.")
            else:
                await callback.answer("Impossibile approvare la richiesta, riprova piu tardi.", show_alert=True)
            return

        attempts = self.storage.increment_captcha_attempts(request_id)
        if attempts >= max_attempts:
            declined = await self.try_decline_request(request)
            if declined:
                self.storage.complete(request_id, "declined", None, f"captcha_failed:{captcha_difficulty}")
            if callback.message:
                await callback.message.edit_text(
                    "Limite tentativi raggiunto. Richiesta rifiutata. Invia una nuova richiesta al canale."
                )
            await callback.answer("Risposta errata.")
            return

        question, answer, options = generate_captcha(difficulty=captcha_difficulty)
        self.storage.refresh_captcha(request_id, question, answer)
        remaining = max_attempts - attempts
        new_text = (
            "Risposta errata.\n"
            f"Tentativi rimasti: {remaining}\n"
            f"Nuovo captcha:\n{question}"
        )
        if callback.message:
            await callback.message.edit_text(new_text, reply_markup=build_captcha_keyboard(request_id, options))
        await callback.answer("Errato, riprova.")

    async def on_admin_callback(self, callback: CallbackQuery) -> None:
        if not callback.data:
            await callback.answer("Azione non valida.", show_alert=True)
            return

        if not self._is_admin(callback.from_user.id if callback.from_user else None):
            await callback.answer("Non hai i permessi per questa azione.", show_alert=True)
            return

        parsed = parse_admin_callback(callback.data)
        if parsed is None:
            await callback.answer("Azione non valida.", show_alert=True)
            return

        action, request_id = parsed
        request = self.storage.get(request_id)
        if request is None:
            await callback.answer("Richiesta non trovata.", show_alert=True)
            return

        if request["status"] != "pending_admin":
            await callback.answer("La richiesta e gia stata elaborata.", show_alert=True)
            return

        user_chat_id = int(request["user_chat_id"])
        if action == "approve":
            approved = await self.try_approve_request(request)
            if not approved:
                await callback.answer("Impossibile approvare la richiesta.", show_alert=True)
                return
            self.storage.complete(request_id, "approved", callback.from_user.id, "manual_approve")
            await self.safe_send_user_message(
                user_chat_id,
                "Un amministratore ha approvato la tua richiesta. Benvenuto nel canale.",
            )
            final_text = self._build_admin_result_text(request, "approvata", callback.from_user.id)
            if callback.message:
                await callback.message.edit_text(final_text)
            logging.info(
                "admin_decision request_id=%s action=approve admin_id=%s user_id=%s risk_score=%s",
                request_id,
                callback.from_user.id,
                request["user_id"],
                request["risk_score"],
            )
            await callback.answer("Richiesta approvata.")
            return

        declined = await self.try_decline_request(request)
        if not declined:
            await callback.answer("Impossibile rifiutare la richiesta.", show_alert=True)
            return
        self.storage.complete(request_id, "declined", callback.from_user.id, "manual_decline")
        await self.safe_send_user_message(
            user_chat_id,
            "Un amministratore ha rifiutato la tua richiesta al canale.",
        )
        final_text = self._build_admin_result_text(request, "rifiutata", callback.from_user.id)
        if callback.message:
            await callback.message.edit_text(final_text)
        logging.info(
            "admin_decision request_id=%s action=decline admin_id=%s user_id=%s risk_score=%s",
            request_id,
            callback.from_user.id,
            request["user_id"],
            request["risk_score"],
        )
        await callback.answer("Richiesta rifiutata.")

    async def notify_admins(
        self,
        join_request: ChatJoinRequest,
        request_id: int,
        risk: RiskAssessment,
        route_reason: str,
    ) -> None:
        user = join_request.from_user
        username = f"@{user.username}" if user.username else "non impostato"
        reasons = "\n".join(f"- {item}" for item in risk.reasons) if risk.reasons else "- none"
        text = (
            "Richiesta instradata a revisione admin\n"
            f"Canale: {join_request.chat.title} ({join_request.chat.id})\n"
            f"Utente: {user.full_name}\n"
            f"ID: {user.id}\n"
            f"Username: {username}\n"
            f"Eta account stimata: {risk.estimated_age_days} giorni\n"
            f"Route: {route_reason}\n"
            f"Risk score: {risk.score}\n"
            f"Dettagli rischio:\n{reasons}\n"
            f"request_id: {request_id}"
        )

        keyboard = build_admin_keyboard(request_id)
        for admin_id in self.settings.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, reply_markup=keyboard)
            except TelegramForbiddenError:
                logging.warning("Admin %s has not started the bot or blocked it", admin_id)
            except TelegramBadRequest as exc:
                logging.warning("Failed to notify admin %s: %s", admin_id, str(exc))

    async def safe_send_user_message(
        self,
        user_chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await self.bot.send_message(user_chat_id, text, reply_markup=reply_markup)
        except TelegramForbiddenError:
            logging.warning("Cannot message user_chat_id=%s (forbidden)", user_chat_id)
        except TelegramBadRequest as exc:
            logging.warning("Cannot message user_chat_id=%s: %s", user_chat_id, str(exc))

    async def try_approve_request(self, request: sqlite3.Row) -> bool:
        try:
            await self.bot.approve_chat_join_request(
                chat_id=int(request["channel_id"]),
                user_id=int(request["user_id"]),
            )
            return True
        except TelegramBadRequest as exc:
            logging.warning("approve_chat_join_request failed: %s", str(exc))
            return False

    async def try_decline_request(self, request: sqlite3.Row) -> bool:
        try:
            await self.bot.decline_chat_join_request(
                chat_id=int(request["channel_id"]),
                user_id=int(request["user_id"]),
            )
            return True
        except TelegramBadRequest as exc:
            logging.warning("decline_chat_join_request failed: %s", str(exc))
            return False

    def _build_admin_result_text(self, request: sqlite3.Row, status_text: str, admin_id: int) -> str:
        username = f"@{request['username']}" if request["username"] else "non impostato"
        risk_reasons = deserialize_risk_reasons(request["risk_reasons"])
        risk_details = "; ".join(risk_reasons) if risk_reasons else "none"
        return (
            "Richiesta elaborata\n"
            f"Utente: {request['first_name']} {request['last_name'] or ''}\n"
            f"ID: {request['user_id']}\n"
            f"Username: {username}\n"
            f"Risk score: {request['risk_score']}\n"
            f"Risk details: {risk_details}\n"
            f"Stato: {status_text}\n"
            f"Amministratore: {admin_id}"
        )

    def _build_dashboard_text(self) -> str:
        mode = self.storage.get_moderation_mode()
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        stats = self.storage.get_status_stats(now_ts - 24 * 3600)
        recent = self.storage.list_recent_decisions(limit=5)

        total = sum(stats.values())
        lines = [
            "JoinGuard Control Center (24h)",
            f"Modo moderazione: {mode}",
            f"Totale richieste: {total}",
            f"- new: {stats.get('new', 0)}",
            f"- pending_admin: {stats.get('pending_admin', 0)}",
            f"- pending_captcha: {stats.get('pending_captcha', 0)}",
            f"- approved: {stats.get('approved', 0)}",
            f"- declined: {stats.get('declined', 0)}",
            "",
            "Policy rischio:",
            f"- admin threshold: >= {self.settings.risk_score_to_admin}",
            f"- hard captcha threshold: >= {self.settings.risk_score_to_hard_captcha}",
            f"- hard captcha attempts: {self.settings.hard_captcha_attempts}",
        ]

        if recent:
            lines.append("")
            lines.append("Ultime decisioni:")
            for item in recent:
                decided_at = int(item["decision_at"]) if item["decision_at"] else 0
                stamp = datetime.fromtimestamp(decided_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                lines.append(
                    (
                        f"#{item['id']} {item['status']} user={item['user_id']} "
                        f"risk={item['risk_score']} at={stamp}"
                    )
                )
        return "\n".join(lines)

    def _build_pending_text(self, pending: list[sqlite3.Row]) -> str:
        if not pending:
            return "Nessuna richiesta in pending_admin."
        lines = [f"Pending review: {len(pending)}"]
        for item in pending:
            username = f"@{item['username']}" if item["username"] else "non impostato"
            reasons = deserialize_risk_reasons(item["risk_reasons"])
            short_reason = ", ".join(reasons[:2]) if reasons else "none"
            age_days = item["estimated_age_days"] if item["estimated_age_days"] is not None else "n/a"
            lines.append(
                (
                    f"#{item['id']} user={item['user_id']} {username} "
                    f"risk={item['risk_score']} eta={age_days}g"
                )
            )
            lines.append(f"reason: {short_reason}")
        return "\n".join(lines)

    async def _build_channel_text(self) -> str:
        mode = self.storage.get_moderation_mode()
        try:
            chat = await self.bot.get_chat(self.settings.channel_id)
            members = await self.bot.get_chat_member_count(self.settings.channel_id)
            me = await self.bot.get_me()
            bot_member = await self.bot.get_chat_member(self.settings.channel_id, me.id)
            can_invite = getattr(bot_member, "can_invite_users", None)
            can_manage_chat = getattr(bot_member, "can_manage_chat", None)
            return (
                "Channel control\n"
                f"Titolo: {chat.title}\n"
                f"ID: {chat.id}\n"
                f"Members: {members}\n"
                f"Bot can_invite_users: {can_invite}\n"
                f"Bot can_manage_chat: {can_manage_chat}\n"
                f"Modo moderazione: {mode}"
            )
        except TelegramBadRequest as exc:
            return (
                "Channel control\n"
                f"ID: {self.settings.channel_id}\n"
                f"Modo moderazione: {mode}\n"
                f"Errore Telegram: {str(exc)}"
            )

    async def run(self) -> None:
        # The bot is designed to work in long polling mode, so we disable webhook on startup.
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.dp.start_polling(self.bot, allowed_updates=self.dp.resolve_used_update_types())

    async def close(self) -> None:
        self.storage.close()
        await self.bot.session.close()


async def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    app = JoinGuardBot(settings)
    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
