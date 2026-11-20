"""Inicio del /panel: resumen, 9 módulos, /cancelar y cerrar."""
from __future__ import annotations

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel import bans, buttons, clones, donations, editor, products, support, texts, welcome
from handlers.panel.common import ALL_STATES, admin_router, grid, replace

router = admin_router("panel_home")

GROUPS = ("welcome", "products", "links", "buttons", "texts", "donations", "bans", "support", "clones")
_VIEWS = {
    "welcome": welcome.show,
    "products": products.show,
    "buttons": buttons.show,
    "texts": texts.show,
    "donations": donations.show,
    "bans": bans.show,
    "support": support.show,
    "clones": clones.show,
}


async def home_text() -> str:
    tips = [b["target_value"] for b in await db.get_buttons(config.DONATE_MENU, True) if b["action_type"] == "tip"]
    return config.ta(
        "panel_home",
        products=len(config.active_products()),
        tips=" · ".join(tips) or config.ta("panel_none"),
        posts=await db.count_scheduled_posts(),
        banned=await db.count_banned(),
        clones=await db.count_clones(),
        support_mode=config.ta(f"support_mode_{config.support_mode()}"),
    )


def home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for group in GROUPS:
        b.button(text=config.ta(f"panel_group_{group}"), callback_data=f"panel:group:{group}")
    b.button(text=config.ta("btn_panel_close"), callback_data="panel:close")
    grid(b, len(GROUPS))
    return b.as_markup()


async def send_home(target: Message) -> None:
    await target.answer(await home_text(), reply_markup=home_kb())


@router.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_home(message)


@router.message(Command("cancelar"), StateFilter(*ALL_STATES))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(config.ta("panel_cancelled"))
    await send_home(message)


@router.callback_query(F.data == "panel:home")
async def panel_home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, await home_text(), home_kb())


@router.callback_query(F.data == "panel:close")
async def panel_close(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer(config.ta("panel_closed"))
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("panel:group:"))
async def open_group(cb: CallbackQuery, state: FSMContext) -> None:
    group = cb.data.split(":", 2)[2]
    await state.clear()
    await cb.answer()
    if group == "links":
        await replace(cb, config.ta("panel_links_title"), editor.link_categories_kb())
    elif group in _VIEWS:
        await _VIEWS[group](cb)
