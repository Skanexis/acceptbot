from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
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

    @staticmethod
    def from_env() -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        channel_raw = os.getenv("CHANNEL_ID", "").strip()
        admin_raw = os.getenv("ADMIN_IDS", "").strip()
        db_path = os.getenv("DB_PATH", "join_guard.db").strip()
        min_account_age_days = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "30"))
        max_captcha_attempts = int(os.getenv("MAX_CAPTCHA_ATTEMPTS", "3"))

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

        return Settings(
            bot_token=bot_token,
            channel_id=int(channel_raw),
            admin_ids=admin_ids,
            db_path=db_path,
            min_account_age_days=min_account_age_days,
            max_captcha_attempts=max_captcha_attempts,
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
                captcha_question TEXT,
                captcha_answer INTEGER,
                captcha_attempts INTEGER NOT NULL DEFAULT 0,
                decision_by INTEGER,
                decision_at INTEGER,
                reason TEXT,
                UNIQUE(channel_id, user_id)
            )
            """
        )
        self.conn.commit()

    def create_or_refresh_request(self, join_request: ChatJoinRequest) -> int:
        user = join_request.from_user
        now = int(datetime.now(tz=timezone.utc).timestamp())

        self.conn.execute(
            """
            INSERT INTO join_requests (
                channel_id, user_id, user_chat_id, username, first_name, last_name, submitted_at,
                status, is_suspicious, estimated_age_days, captcha_question, captcha_answer,
                captcha_attempts, decision_by, decision_at, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'new', 0, NULL, NULL, NULL, 0, NULL, NULL, NULL)
            ON CONFLICT(channel_id, user_id) DO UPDATE SET
                user_chat_id=excluded.user_chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                submitted_at=excluded.submitted_at,
                status='new',
                is_suspicious=0,
                estimated_age_days=NULL,
                captcha_question=NULL,
                captcha_answer=NULL,
                captcha_attempts=0,
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

    def mark_pending_admin(self, request_id: int, reason: str, estimated_age_days: int) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET status = 'pending_admin',
                is_suspicious = 1,
                estimated_age_days = ?,
                reason = ?
            WHERE id = ?
            """,
            (estimated_age_days, reason, request_id),
        )
        self.conn.commit()

    def mark_pending_captcha(self, request_id: int, question: str, answer: int) -> None:
        self.conn.execute(
            """
            UPDATE join_requests
            SET status = 'pending_captcha',
                is_suspicious = 0,
                captcha_question = ?,
                captcha_answer = ?,
                captcha_attempts = 0,
                reason = NULL
            WHERE id = ?
            """,
            (question, answer, request_id),
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


def is_suspicious_account(user: User, min_account_age_days: int) -> tuple[bool, str, int]:
    if user.is_bot:
        return True, "richiesta inviata da un bot", 0

    estimated_age_days = estimate_account_age_days(user.id)
    if estimated_age_days < min_account_age_days:
        return (
            True,
            (
                f"eta stimata dell'account: {estimated_age_days} giorni, "
                f"inferiore alla soglia di {min_account_age_days} giorni."
            ),
            estimated_age_days,
        )

    return False, f"eta stimata dell'account: {estimated_age_days} giorni.", estimated_age_days


def generate_captcha() -> tuple[str, int, list[int]]:
    first = random.randint(2, 12)
    second = random.randint(1, 9)
    operation = random.choice(["+", "-"])

    if operation == "-" and second > first:
        first, second = second, first

    answer = first + second if operation == "+" else first - second
    options = {answer}

    while len(options) < 4:
        options.add(answer + random.randint(-7, 7))

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


class JoinGuardBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.bot = Bot(settings.bot_token)
        self.dp = Dispatcher()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.dp.message.register(self.on_start, CommandStart())
        self.dp.chat_join_request.register(self.on_join_request)
        self.dp.callback_query.register(self.on_captcha_callback, F.data.startswith("cap:"))
        self.dp.callback_query.register(self.on_admin_callback, F.data.startswith("adm:"))

    async def on_start(self, message: Message) -> None:
        await message.answer(
            "Il bot di moderazione delle richieste e attivo.\n"
            "Se hai inviato una richiesta al canale, segui le istruzioni che arriveranno in questa chat."
        )

    async def on_join_request(self, join_request: ChatJoinRequest) -> None:
        if join_request.chat.id != self.settings.channel_id:
            return

        request_id = self.storage.create_or_refresh_request(join_request)
        user = join_request.from_user

        await self.safe_send_user_message(
            join_request.user_chat_id,
            "La tua richiesta e stata ricevuta. Ora il bot controllera l'account e ti inviera il prossimo passaggio.",
        )

        suspicious, reason, estimated_age_days = is_suspicious_account(user, self.settings.min_account_age_days)
        if suspicious:
            self.storage.mark_pending_admin(request_id, reason, estimated_age_days)
            await self.notify_admins(join_request, request_id, reason, estimated_age_days)
            await self.safe_send_user_message(
                join_request.user_chat_id,
                "La richiesta e stata inviata a un amministratore per la verifica manuale. Ti aggiornero in questa chat.",
            )
            return

        question, answer, options = generate_captcha()
        self.storage.mark_pending_captcha(request_id, question, answer)
        await self.safe_send_user_message(
            join_request.user_chat_id,
            f"Controllo account completato.\nRisolvi il captcha per entrare nel canale:\n{question}",
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

        expected_answer = int(request["captcha_answer"])
        if selected_answer == expected_answer:
            approved = await self.try_approve_request(request)
            if approved:
                self.storage.complete(request_id, "approved", None, "captcha_passed")
                if callback.message:
                    await callback.message.edit_text("Captcha superato. Richiesta approvata, accesso al canale aperto.")
                await callback.answer("Richiesta approvata.")
            else:
                await callback.answer("Impossibile approvare la richiesta, riprova piu tardi.", show_alert=True)
            return

        attempts = self.storage.increment_captcha_attempts(request_id)
        if attempts >= self.settings.max_captcha_attempts:
            declined = await self.try_decline_request(request)
            if declined:
                self.storage.complete(request_id, "declined", None, "captcha_failed")
            if callback.message:
                await callback.message.edit_text(
                    "Limite tentativi raggiunto. Richiesta rifiutata. Invia una nuova richiesta al canale."
                )
            await callback.answer("Risposta errata.")
            return

        question, answer, options = generate_captcha()
        self.storage.refresh_captcha(request_id, question, answer)
        remaining = self.settings.max_captcha_attempts - attempts
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

        if callback.from_user.id not in self.settings.admin_ids:
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
        await callback.answer("Richiesta rifiutata.")

    async def notify_admins(
        self,
        join_request: ChatJoinRequest,
        request_id: int,
        reason: str,
        estimated_age_days: int,
    ) -> None:
        user = join_request.from_user
        username = f"@{user.username}" if user.username else "non impostato"
        text = (
            "Richiesta sospetta per il canale\n"
            f"Canale: {join_request.chat.title} ({join_request.chat.id})\n"
            f"Utente: {user.full_name}\n"
            f"ID: {user.id}\n"
            f"Username: {username}\n"
            f"Eta account stimata: {estimated_age_days} giorni.\n"
            f"Motivo: {reason}\n"
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
        return (
            "Richiesta elaborata\n"
            f"Utente: {request['first_name']} {request['last_name'] or ''}\n"
            f"ID: {request['user_id']}\n"
            f"Username: {username}\n"
            f"Stato: {status_text}\n"
            f"Amministratore: {admin_id}"
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
