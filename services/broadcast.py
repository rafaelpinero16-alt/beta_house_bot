"""Anuncios y publicaciones de donación: envío a usuarios del bot y a los grupos/canales vinculados,
inmediato o programado (con repetición opcional)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services.telegram_service import send_rich

log = logging.getLogger(__name__)
_running: set[asyncio.Task] = set()
_SLUG_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_WHEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2})(?:\s+cada\s+(\d{1,4}))?$", re.I)


# ===========================================================================
# Botones de las publicaciones
# ===========================================================================
def _check_destination(dest: str) -> bool:
    dest = dest.strip()
    if config.is_http_url(dest):
        return True
    if dest.startswith("link:"):
        return dest[5:] in config.all_link_keys()
    if dest == "support":
        return True
    kind, _, value = dest.partition(":")
    if kind == "menu":
        return config.get_menu(value) is not None
    if kind in ("sub", "pay"):
        return config.get_product(value) is not None
    if kind == "tip":
        return value.isdigit() and 0 < int(value) <= config.STARS_PRICE_MAX
    return False


def parse_buttons(text: str) -> list[list[list[str]]]:
    """Una fila por línea; varios botones en la misma fila separados por «&&». Lanza SettingError."""
    if text.strip() in ("0", "-", ""):
        return []
    rows: list[list[list[str]]] = []
    for number, line in enumerate(text.strip().splitlines(), start=1):
        row = []
        for part in line.split("&&"):
            label, sep, dest = part.partition("|")
            label, dest = label.strip(), dest.strip()
            if not sep or not 1 <= len(label) <= 64 or set("<>&") & set(label) or not _check_destination(dest):
                raise config.SettingError(config.ta("err_post_buttons", line=number, text=part.strip()[:60]))
            row.append([label, dest])
        rows.append(row)
    return rows


def resolve_destination(dest: str, bot_username: str) -> str:
    if config.is_http_url(dest):
        return dest
    if dest.startswith("link:"):
        return config.link(dest[5:])
    if dest == "support":
        return config.deep_link(bot_username, "sp")
    kind, _, value = dest.partition(":")
    prefix = {"menu": "m", "sub": "s", "pay": "r", "tip": "t"}.get(kind)
    return config.deep_link(bot_username, f"{prefix}_{value}") if prefix and _SLUG_RE.match(value) else ""


async def build_keyboard(post: dict, bot_username: str) -> InlineKeyboardMarkup | None:
    rows = json.loads(post["buttons"] or "[]")
    if rows == "auto":  # un botón por cada rango de donación vigente, en filas de 3
        amounts = [int(b["target_value"]) for b in await db.get_buttons(config.DONATE_MENU, True)
                   if b["action_type"] == "tip" and (b["target_value"] or "").isdigit()]
        rows = [[[f"⭐ {a}", f"tip:{a}"] for a in amounts[i:i + 3]] for i in range(0, len(amounts), 3)]
    keyboard = []
    for row in rows:
        buttons = [
            InlineKeyboardButton(text=label, url=url)
            for label, dest in row
            if (url := resolve_destination(dest, bot_username))
        ]
        if buttons:
            keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def encode_buttons(text: str) -> str:
    """Valida el texto de botones del asistente y lo guarda como JSON (o "auto" para rangos de donación)."""
    if text.strip().lower() == "auto":
        return '"auto"'
    return json.dumps(parse_buttons(text), ensure_ascii=False)


# ===========================================================================
# Programación
# ===========================================================================
def parse_when(text: str) -> tuple[int | None, int]:
    """'ahora' → (None, 0) · 'AAAA-MM-DD HH:MM [cada N]' → (epoch, horas). Lanza SettingError."""
    raw = text.strip()
    if raw.lower() in ("ahora", "now", config.ta("post_when_now")):
        return None, 0
    match = _WHEN_RE.match(raw)
    if not match:
        raise config.SettingError(config.ta("err_post_when"))
    try:
        local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(config.TIMEZONE))
    except ValueError:
        raise config.SettingError(config.ta("err_post_when")) from None
    epoch = int(local.timestamp())
    if epoch <= int(time.time()):
        raise config.SettingError(config.ta("err_post_when"))
    return epoch, int(match.group(2) or 0)


def format_when(epoch: int | None) -> str:
    if not epoch:
        return config.ta("post_when_now")
    return datetime.fromtimestamp(epoch, ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M")


# ===========================================================================
# Envío
# ===========================================================================
async def _targets(post: dict) -> list[int]:
    target = post["target"]
    if target == "users":
        return await db.broadcast_user_ids()
    if target == "chats":
        ids = {p.chat_id for p in config.active_products()}
        ids |= {c["chat_id"] for c in await db.list_admin_chats() if c["can_post"]}
        return sorted(ids)
    kind, _, value = target.partition(":")
    return [int(value)] if kind in ("chat", "me") and value.lstrip("-").isdigit() else []


async def _send_one(bot: Bot, chat_id: int, post: dict, kb: InlineKeyboardMarkup | None) -> bool:
    for _ in range(2):
        try:
            await send_rich(bot, chat_id, post["text"], post["media"], reply_markup=kb)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramAPIError:
            return False
    return False


async def deliver(bot: Bot, post: dict) -> tuple[int, int]:
    me = await bot.get_me()
    kb = await build_keyboard(post, me.username)
    sent = failed = 0
    for chat_id in await _targets(post):
        if await _send_one(bot, chat_id, post, kb):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)
    return sent, failed


async def _run(bot: Bot, post_id: int) -> None:
    post = await db.get_post(post_id)
    if not post:
        return
    try:
        sent, failed = await deliver(bot, post)
    except Exception:
        log.exception("Error enviando la publicación %s", post_id)
        sent, failed = 0, 0
    next_at = None
    if post["repeat_hours"] and post["target"] != f"me:{post['created_by']}":
        next_at = max(int(time.time()), post["send_at"] or 0) + post["repeat_hours"] * 3600
    await db.finish_post(post_id, sent, failed, next_at)
    if post["created_by"]:
        try:
            await bot.send_message(post["created_by"], config.ta("post_done_dm", id=post_id, sent=sent, failed=failed))
        except TelegramAPIError:
            pass


def start(bot: Bot, post_id: int) -> None:
    """Envía una publicación en segundo plano (no bloquea el panel)."""
    task = asyncio.create_task(_run(bot, post_id), name=f"post_{post_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)


async def sweep(bot: Bot) -> None:
    for post in await db.claim_due_posts():
        start(bot, post["id"])
