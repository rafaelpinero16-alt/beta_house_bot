"""Módulo 3 · Enlaces: catálogo por categorías (en editor.py) y enlaces personalizados (métodos de pago nuevos,
otros productos, redes…) que cualquier botón puede usar. Muestra en qué botones se usa cada enlace."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, LinkEdit, admin_router, audit, back_kb, grid, replace, short
from services import catalog

router = admin_router("panel_links")
_LABEL_MAX = 40


async def usage(key: str) -> str:
    rows = await db.get_buttons_using_link(key)
    if not rows:
        return config.ta("link_usage_none")
    return ", ".join(
        f"{html.escape(config.get_menu(r['menu_id']).title if config.get_menu(r['menu_id']) else r['menu_id'])}"
        f" › {html.escape(short(r['name_es'], 25))}"
        for r in rows
    )


async def custom_links_view() -> tuple[str, InlineKeyboardMarkup]:
    links = config.custom_links()
    items = "\n".join(config.ta("custom_link_item", label=html.escape(label), key=key) for key, (label, _) in links.items())
    b = InlineKeyboardBuilder()
    for key, (label, _url) in links.items():
        b.button(text=f"🔗 {short(label, 40)}", callback_data=f"lk:o:{key}")
    b.button(text=config.ta("btn_custom_link_add"), callback_data="lk:add")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:links")
    grid(b, len(links), columns=1, tail=2)
    return config.ta("custom_links_title", items=items or config.ta("panel_none")), b.as_markup()


async def link_detail(key: str) -> tuple[str, InlineKeyboardMarkup] | None:
    if key not in config.custom_links():
        return None
    label, url = config.custom_links()[key]
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_custom_link_edit"), callback_data=f"lk:edit:{key}")
    b.button(text=config.ta("btn_custom_link_delete"), callback_data=f"lk:del:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data="lk:list")
    b.adjust(2, 1)
    text = config.ta("custom_link_detail", label=html.escape(label), key=key, url=html.escape(url), usage=await usage(key))
    return text, b.as_markup()


@router.callback_query(F.data == "lk:list")
async def list_links(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, *await custom_links_view())


@router.callback_query(F.data == "lk:add")
async def ask_new_link(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LinkEdit.new)
    await cb.answer()
    await replace(cb, config.ta("custom_link_prompt"), back_kb("lk:list"))


@router.message(LinkEdit.new, NOT_COMMAND)
async def create_link(message: Message, state: FSMContext, bot: Bot) -> None:
    label, sep, url = (message.text or "").partition("|")
    label = label.strip()
    try:
        if not sep or not 1 <= len(label) <= _LABEL_MAX or set("<>&") & set(label):
            raise config.SettingError(config.ta("err_custom_link"))
        url = config.check_url(url)
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    key = f"{config.CUSTOM_LINK_PREFIX}{catalog._slug(label, 20)}"
    number = 2
    while key in config.custom_links() or key in config.LINK_CATALOG:
        key = f"{config.CUSTOM_LINK_PREFIX}{catalog._slug(label, 17)}_{number}"
        number += 1
    await db.upsert_custom_link(key, label, url)
    await catalog.reload_links()
    await state.clear()
    await audit(bot, f"Enlace {key}", "—", url, message.from_user.full_name)
    await message.answer(config.ta("custom_link_saved"))
    detail = await link_detail(key)
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])


@router.callback_query(F.data.regexp(r"^lk:(o|edit|del|delok):"))
async def link_actions(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, op, key = cb.data.split(":", 2)
    if key not in config.custom_links():
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    label, url = config.custom_links()[key]
    if op == "o":
        await cb.answer()
        await replace(cb, *await link_detail(key))
    elif op == "edit":
        await state.set_state(LinkEdit.url)
        await state.update_data(key=key)
        await cb.answer()
        await replace(cb, config.ta("custom_link_url_prompt", label=html.escape(label)), back_kb(f"lk:o:{key}"))
    elif op == "del":
        b = InlineKeyboardBuilder()
        b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"lk:delok:{key}")
        b.button(text=config.ta("btn_panel_back"), callback_data=f"lk:o:{key}")
        b.adjust(2)
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=b.as_markup())
    elif op == "delok":
        await db.delete_custom_link(key)
        await catalog.reload_links()
        await audit(bot, f"Enlace {key}", url, "—", cb.from_user.full_name)
        await cb.answer(config.ta("custom_link_deleted"), show_alert=True)
        await replace(cb, *await custom_links_view())


@router.message(LinkEdit.url, NOT_COMMAND)
async def update_link(message: Message, state: FSMContext, bot: Bot) -> None:
    key = (await state.get_data()).get("key", "")
    if key not in config.custom_links():
        await state.clear()
        await message.answer(config.ta("panel_session_expired"))
        return
    try:
        url = config.check_url(message.text or "")
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    label, old = config.custom_links()[key]
    await db.upsert_custom_link(key, label, url)
    await catalog.reload_links()
    await state.clear()
    await audit(bot, f"Enlace {key}", old, url, message.from_user.full_name)
    await message.answer(config.ta("custom_link_saved"))
    detail = await link_detail(key)
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])
