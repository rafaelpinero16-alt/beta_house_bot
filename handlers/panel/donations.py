"""Módulo 6 · Donaciones: rangos en Stars de la sección de donaciones, todos los botones de donación del bot,
textos, enlace directo a la sección y publicaciones de donación (en posts.py)."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, TipEdit, admin_router, audit, back_kb, grid, replace, short
from handlers.panel.editor import check_stars_amount

router = admin_router("panel_donations")


async def _tip_rows(menu_id: str | None = config.DONATE_MENU) -> list[dict]:
    rows = await db.get_buttons_by_action("tip")
    return [r for r in rows if menu_id is None or r["menu_id"] == menu_id]


async def show(cb: CallbackQuery, bot: Bot | None = None) -> None:
    donate_tips = [r["target_value"] for r in await _tip_rows() if r["is_active"]]
    all_tips = await _tip_rows(None)
    username = (await cb.bot.get_me()).username if cb.bot else "bot"
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_tip_ranges"), callback_data="tp:ranges")
    b.button(text=config.ta("btn_tip_everywhere"), callback_data="tp:all")
    b.button(text=config.ta("btn_menu_buttons"), callback_data=f"pb:m:{config.DONATE_MENU}")
    b.button(text=config.ta("btn_tip_texts"), callback_data="panel:tc:donations")
    b.button(text=config.ta("btn_donation_posts"), callback_data="ps:list:donation")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(1)
    text = config.ta(
        "panel_donations_title", tips=" · ".join(donate_tips) or config.ta("panel_none"), count=len(all_tips),
        deeplink=html.escape(config.deep_link(username, f"m_{config.DONATE_MENU}")),
    )
    await replace(cb, text, b.as_markup())


async def ranges_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = await _tip_rows()
    b = InlineKeyboardBuilder()
    for row in rows:
        dot = "🟢" if row["is_active"] else "🔴"
        b.button(text=f"{dot} ⭐ {row['target_value']}", callback_data=f"pb:b:{config.DONATE_MENU}:{row['button_id']}")
    tail = 1
    if len(await db.get_buttons(config.DONATE_MENU)) < config.MAX_BUTTONS_PER_MENU:
        b.button(text=config.ta("btn_tip_add"), callback_data="tp:add")
        tail = 2
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:donations")
    grid(b, len(rows), columns=3, tail=tail)
    return config.ta("tip_ranges_title"), b.as_markup()


@router.callback_query(F.data == "tp:ranges")
async def ranges(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, *await ranges_view())


@router.callback_query(F.data == "tp:all")
async def everywhere(cb: CallbackQuery) -> None:
    rows = await _tip_rows(None)
    items = "\n".join(
        config.ta(
            "tip_everywhere_item",
            menu=html.escape(config.get_menu(r["menu_id"]).title if config.get_menu(r["menu_id"]) else r["menu_id"]),
            amount=html.escape(r["target_value"] or "?"), state="🟢" if r["is_active"] else "🔴",
        )
        for r in rows
    ) or config.ta("panel_none")
    b = InlineKeyboardBuilder()
    for row in rows:
        b.button(text=f"⭐ {row['target_value']} · {short(row['menu_id'], 15)}",
                 callback_data=f"pb:b:{row['menu_id']}:{row['button_id']}")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:donations")
    grid(b, len(rows))
    await cb.answer()
    await replace(cb, config.ta("tip_everywhere_title", items=items), b.as_markup())


@router.callback_query(F.data == "tp:add")
async def ask_amount(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TipEdit.amount)
    await cb.answer()
    await replace(cb, config.ta("tip_prompt_amount", max=config.STARS_PRICE_MAX), back_kb("tp:ranges"))


@router.message(TipEdit.amount, NOT_COMMAND)
async def add_range(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    try:
        if not raw.isdigit() or not 1 <= int(raw) <= config.STARS_PRICE_MAX:
            raise config.SettingError(config.ta("err_pb_tip", max=config.STARS_PRICE_MAX))
        await check_stars_amount(bot, int(raw))
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    if len(await db.get_buttons(config.DONATE_MENU)) >= config.MAX_BUTTONS_PER_MENU:
        await state.clear()
        await message.answer(config.ta("pb_full", max=config.MAX_BUTTONS_PER_MENU))
        return
    tips = await _tip_rows()
    button_id = await db.add_button(config.DONATE_MENU, config.CUSTOM_BUTTON_PREFIX, "⭐ {amount}", "⭐ {amount}", "tip", raw)
    last_order = max((r["sort_order"] for r in tips), default=0)
    await db.update_button(
        config.DONATE_MENU, button_id, is_active=1, sort_order=last_order + 1, same_row=int(len(tips) % 3 != 0),
    )
    await state.clear()
    await audit(bot, "Rango de donación", "—", f"{raw} ⭐", message.from_user.full_name)
    await message.answer(config.ta("tip_added"))
    text, kb = await ranges_view()
    await message.answer(text, reply_markup=kb)
