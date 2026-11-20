"""/estado (diagnóstico con botones para corregir) y /sincronizar (reaplicar permisos de todos los miembros).
Solo ADMIN_IDS, en su chat privado con el bot."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from handlers.panel.common import admin_router
from services import roles, status
from services.commands import ensure_admin_commands

router = admin_router("panel_status")
log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _keyboard(report: status.Report) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for chat_id, title in report.fixable_chats:
        b.button(text=config.ta("btn_fix_group", title=title[:30]), callback_data=f"st:fix:{chat_id}")
    b.button(text=config.ta("btn_status_sync"), callback_data="st:sync")
    b.button(text=config.ta("btn_status_refresh"), callback_data="st:refresh")
    b.adjust(1)
    return b.as_markup()


async def _sync_and_report(bot: Bot, admin_id: int) -> None:
    try:
        counts = await roles.sync_all(bot)
        await bot.send_message(admin_id, config.ta("roles_synced", **counts))
    except Exception:
        log.exception("Error al sincronizar permisos")


@router.message(Command("estado"))
async def cmd_status(message: Message, bot: Bot) -> None:
    await ensure_admin_commands(bot, message.from_user.id)
    report = await status.build(bot)
    await message.answer(report.text(), reply_markup=_keyboard(report), disable_web_page_preview=True)


@router.message(Command("sincronizar"))
async def cmd_sync(message: Message, bot: Bot) -> None:
    await message.answer(config.ta("sync_started"))
    _background(_sync_and_report(bot, message.from_user.id))


@router.callback_query(F.data == "st:refresh")
async def refresh(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    report = await status.build(bot)
    try:
        await cb.message.edit_text(report.text(), reply_markup=_keyboard(report), disable_web_page_preview=True)
    except Exception:  # sin cambios
        pass


@router.callback_query(F.data == "st:sync")
async def sync(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer(config.ta("sync_started")[:190])
    _background(_sync_and_report(bot, cb.from_user.id))


@router.callback_query(F.data.startswith("st:fix:"))
async def fix_group(cb: CallbackQuery, bot: Bot) -> None:
    raw = cb.data.split(":", 2)[2]
    if not raw.lstrip("-").isdigit() or int(raw) not in config.role_chat_ids():
        await cb.answer()
        return
    chat_id = int(raw)
    await cb.answer(config.ta("sync_started")[:190])
    title = html.escape(config.chat_label(chat_id))
    synced, error = await roles.open_group_defaults(bot, chat_id)
    text = (config.ta("group_fix_failed", title=title, error=html.escape(error)) if error
            else config.ta("group_fixed", title=title, synced=synced))
    await bot.send_message(cb.from_user.id, text)
    report = await status.build(bot)
    await bot.send_message(cb.from_user.id, report.text(), reply_markup=_keyboard(report),
                           disable_web_page_preview=True)
