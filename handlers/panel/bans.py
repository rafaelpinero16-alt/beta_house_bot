"""Módulo 7 · Baneos: vetar por ID o @usuario (de quien ya escribió al bot) y levantar vetos."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, BanAdd, admin_router, back_kb, grid, replace
from services.moderation import ban_member, resolve_user, unban_member

router = admin_router("panel_bans")


async def show(cb: CallbackQuery) -> None:
    await replace(cb, *await _bans_view())


async def _bans_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.list_banned()
    items = "\n".join(
        config.ta(
            "bans_item", user_id=r["user_id"], name=html.escape(r["full_name"] or ""),
            reason=html.escape(r["reason"] or config.ta("ban_no_reason")), date=(r["banned_at"] or "")[:10],
        )
        for r in rows
    ) or config.ta("bans_empty")
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=config.ta("btn_unban", user_id=r["user_id"]), callback_data=f"ban:un:{r['user_id']}")
    b.button(text=config.ta("btn_ban_add"), callback_data="ban:add")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    grid(b, len(rows), tail=2)
    return config.ta("bans_title", count=await db.count_banned(), items=items), b.as_markup()


@router.callback_query(F.data == "ban:add")
async def ban_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BanAdd.waiting)
    await cb.answer()
    await replace(cb, config.ta("ban_prompt"), back_kb("panel:group:bans"))


@router.message(BanAdd.waiting, NOT_COMMAND)
async def ban_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    raw_id, _, reason = (message.text or "").strip().partition(" ")
    user_id = await resolve_user(raw_id)
    if user_id is None:
        await message.answer(
            config.ta("err_ban_username", username=html.escape(raw_id.lstrip("@"))) if raw_id.startswith("@")
            else config.ta("err_ban_id")
        )
        return
    if user_id in config.ADMIN_IDS:
        await message.answer(config.ta("ban_admin_forbidden"))
        return
    result = await ban_member(bot, user_id, reason.strip() or None, message.from_user.id, message.from_user.full_name)
    await state.clear()
    if result is None:
        await message.answer(config.ta("ban_already"))
    else:
        await message.answer(config.ta("ban_done", user_id=user_id, links=result["links"], chats=result["chats"]))
    text, kb = await _bans_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("ban:un:"))
async def ban_remove(cb: CallbackQuery, bot: Bot) -> None:
    raw = cb.data.rsplit(":", 1)[1]
    if not raw.isdigit():
        await cb.answer(config.ta("err_ban_id"), show_alert=True)
        return
    user_id = int(raw)
    if await unban_member(bot, user_id, cb.from_user.full_name):
        await cb.answer(config.ta("unban_done", user_id=user_id)[:200].replace("<code>", "").replace("</code>", ""))
    else:
        await cb.answer(config.ta("ban_not_banned"), show_alert=True)
    await show(cb)


