"""Módulo 5 · Textos y multimedia: el texto (ES/EN) y la imagen/video de cada sección, secciones nuevas,
mensajes automáticos y mensajes del equipo."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel import editor
from handlers.panel.common import (
    NOT_COMMAND,
    MenuEdit,
    admin_router,
    audit,
    back_kb,
    grid,
    media_label,
    replace,
    short,
    template_source,
)
from services import catalog
from services.telegram_service import media_from_message, send_media

router = admin_router("panel_texts")
_MENU_NAME_MAX = 40


async def show(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_texts_menus"), callback_data="pm:list")
    b.button(text=config.ta("btn_texts_auto"), callback_data="panel:tcs")
    b.button(text=config.ta("btn_texts_team"), callback_data="panel:tc:team")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(1)
    await replace(cb, config.ta("panel_texts_title"), b.as_markup())


# ===========================================================================
# Secciones
# ===========================================================================
def menus_kb(prefix: str = "pm:o", back: str = "panel:group:texts", with_new: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    menus = config.all_menus()
    for menu in menus:
        icon = "🖼 " if menu.media else ""
        b.button(text=f"{icon}{short(menu.title, 40)}", callback_data=f"{prefix}:{menu.menu_id}")
    tail = 1
    if with_new:
        b.button(text=config.ta("btn_menu_new"), callback_data="pm:new")
        tail = 2
    b.button(text=config.ta("btn_panel_back"), callback_data=back)
    grid(b, len(menus), columns=1, tail=tail)
    return b.as_markup()


async def menu_detail(menu_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    menu = config.get_menu(menu_id)
    if menu is None:
        return None
    parent = config.get_menu(menu.parent) if menu.parent else None
    buttons = await db.get_buttons(menu_id)
    text_es = config.menu_text(menu_id, "es", mention="[mention]", id="[id]", name="[name]")
    text = config.ta(
        "menu_detail", title=html.escape(menu.title), menu_id=menu_id,
        parent=html.escape(parent.title) if parent else config.ta("menu_root"),
        media=media_label(menu.media), buttons=len(buttons), text_es=html.escape(short(text_es, 1500)),
    )
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_menu_text_es"), callback_data=f"pm:txt:{menu_id}:es")
    b.button(text=config.ta("btn_menu_text_en"), callback_data=f"pm:txt:{menu_id}:en")
    b.button(text=config.ta("btn_menu_media_set"), callback_data=f"pm:media:{menu_id}")
    if menu.media:
        b.button(text=config.ta("btn_menu_media_del"), callback_data=f"pm:mdel:{menu_id}")
    b.button(text=config.ta("btn_menu_buttons"), callback_data=f"pb:m:{menu_id}")
    if not menu.is_system:
        b.button(text=config.ta("btn_menu_rename"), callback_data=f"pm:ren:{menu_id}")
        b.button(text=config.ta("btn_menu_delete"), callback_data=f"pm:del:{menu_id}")
    b.button(text=config.ta("btn_panel_back"), callback_data="pm:list")
    b.adjust(2, *([2] if menu.media else [1]), 1, *([2] if not menu.is_system else []), 1)
    return text, b.as_markup()


@router.callback_query(F.data == "pm:list")
async def list_menus(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, config.ta("panel_menus_title"), menus_kb())


@router.callback_query(F.data.startswith("pm:o:"))
async def open_menu_detail(cb: CallbackQuery, state: FSMContext) -> None:
    detail = await menu_detail(cb.data.split(":", 2)[2])
    if not detail:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await replace(cb, *detail)


@router.callback_query(F.data.startswith("pm:txt:"))
async def edit_menu_text(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, menu_id, lang = cb.data.split(":", 3)
    menu = config.get_menu(menu_id)
    if not menu or lang not in config.LANGS:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await cb.answer()
    if menu.is_system and menu_id in config.SYSTEM_MENUS:  # texto del sistema: editor genérico
        await editor.open_setting(cb, state, f"{config.SYSTEM_MENUS[menu_id][0]}_{lang}", back=f"pm:o:{menu_id}")
        return
    await state.clear()
    await state.set_state(MenuEdit.text)
    await state.update_data(menu_id=menu_id, lang=lang)
    variables = ", ".join(f"<code>{{{p}}}</code>" for p in sorted(config.GLOBAL_PLACEHOLDERS))
    current = (menu.text_en if lang == "en" else menu.text_es) or ""
    prompt = config.ta(
        "menu_prompt_text", lang=config.LANG_FLAGS.get(lang, lang), title=html.escape(menu.title), variables=variables
    )
    await replace(cb, f"{prompt}\n\n<pre>{html.escape(short(current, 3000))}</pre>", back_kb(f"pm:o:{menu_id}"))


@router.message(MenuEdit.text, NOT_COMMAND)
async def receive_menu_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    menu = config.get_menu(data.get("menu_id", ""))
    if not menu or not message.text:
        await message.answer(config.ta("panel_need_text"))
        return
    value = template_source(message).strip()
    try:
        if not value or len(value) > config.LONG_TEXT_MAX_LENGTH:
            raise config.SettingError(config.ta("err_too_long", length=len(value), max=config.LONG_TEXT_MAX_LENGTH))
        config.check_template(value, config.GLOBAL_PLACEHOLDERS)
        await editor.preview_template(message, bot, menu.title, value)
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    field = "text_en" if data.get("lang") == "en" else "text_es"
    old = getattr(menu, field) or ""
    await db.update_menu(menu.menu_id, **{field: value})
    await catalog.reload_menus()
    await state.clear()
    await audit(bot, f"Sección {menu.menu_id} · {field}", old, value, message.from_user.full_name)
    detail = await menu_detail(menu.menu_id)
    await message.answer(config.ta("menu_saved"))
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])


@router.callback_query(F.data.startswith("pm:media:"))
async def ask_media(cb: CallbackQuery, state: FSMContext) -> None:
    menu = config.get_menu(cb.data.split(":", 2)[2])
    if not menu:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await state.set_state(MenuEdit.media)
    await state.update_data(menu_id=menu.menu_id)
    await cb.answer()
    await replace(cb, config.ta("menu_prompt_media", title=html.escape(menu.title)), back_kb(f"pm:o:{menu.menu_id}"))


@router.message(MenuEdit.media, NOT_COMMAND)
async def receive_media(message: Message, state: FSMContext, bot: Bot) -> None:
    menu = config.get_menu((await state.get_data()).get("menu_id", ""))
    value = media_from_message(message)
    if not menu or not value:
        await message.answer(config.ta("panel_need_media"))
        return
    await db.update_menu(menu.menu_id, media=value)
    await catalog.reload_menus()
    await state.clear()
    await send_media(bot, message.chat.id, *value.split(":", 1))
    await audit(bot, f"Sección {menu.menu_id} · multimedia", menu.media, value, message.from_user.full_name)
    detail = await menu_detail(menu.menu_id)
    await message.answer(config.ta("menu_saved"))
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])


@router.callback_query(F.data.startswith("pm:mdel:"))
async def delete_media(cb: CallbackQuery, bot: Bot) -> None:
    menu = config.get_menu(cb.data.split(":", 2)[2])
    if not menu:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await db.update_menu(menu.menu_id, media="")
    await catalog.reload_menus()
    await cb.answer(config.ta("menu_media_removed"))
    await audit(bot, f"Sección {menu.menu_id} · multimedia", menu.media, "", cb.from_user.full_name)
    detail = await menu_detail(menu.menu_id)
    if detail:
        await replace(cb, *detail)


def _valid_name(value: str) -> str:
    value = (value or "").strip()
    if not 1 <= len(value) <= _MENU_NAME_MAX or set("<>&") & set(value):
        raise config.SettingError(config.ta("err_menu_name"))
    return value


@router.callback_query(F.data == "pm:new")
async def ask_new_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(MenuEdit.new)
    await cb.answer()
    await replace(cb, config.ta("menu_prompt_new"), back_kb("pm:list"))


@router.message(MenuEdit.new, NOT_COMMAND)
async def create_menu(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        title = _valid_name(message.text)
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    menu_id = await db.create_menu(config.CUSTOM_MENU_PREFIX, title, "main")
    await catalog.reload_menus()
    await state.clear()
    await audit(bot, f"Sección nueva {menu_id}", "—", title, message.from_user.full_name)
    await message.answer(config.ta("menu_created", title=html.escape(title)))
    detail = await menu_detail(menu_id)
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])


@router.callback_query(F.data.startswith("pm:ren:"))
async def ask_rename(cb: CallbackQuery, state: FSMContext) -> None:
    menu = config.get_menu(cb.data.split(":", 2)[2])
    if not menu or menu.is_system:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await state.set_state(MenuEdit.rename)
    await state.update_data(menu_id=menu.menu_id)
    await cb.answer()
    await replace(cb, config.ta("menu_prompt_rename"), back_kb(f"pm:o:{menu.menu_id}"))


@router.message(MenuEdit.rename, NOT_COMMAND)
async def rename_menu(message: Message, state: FSMContext) -> None:
    menu = config.get_menu((await state.get_data()).get("menu_id", ""))
    try:
        title = _valid_name(message.text)
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    if menu:
        await db.update_menu(menu.menu_id, title=title)
        await catalog.reload_menus()
    await state.clear()
    detail = await menu_detail(menu.menu_id) if menu else None
    await message.answer(config.ta("menu_saved"))
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])


@router.callback_query(F.data.startswith("pm:del:"))
async def ask_delete(cb: CallbackQuery) -> None:
    menu = config.get_menu(cb.data.split(":", 2)[2])
    if not menu or menu.is_system:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await cb.answer()
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"pm:delok:{menu.menu_id}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"pm:o:{menu.menu_id}")
    b.adjust(2)
    try:
        await cb.message.edit_reply_markup(reply_markup=b.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("pm:delok:"))
async def delete_menu(cb: CallbackQuery, bot: Bot) -> None:
    menu = config.get_menu(cb.data.split(":", 2)[2])
    if not menu or menu.is_system:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await db.delete_menu(menu.menu_id)
    await catalog.reload_menus()
    await audit(bot, f"Sección {menu.menu_id}", menu.title, "—", cb.from_user.full_name)
    await cb.answer(config.ta("menu_deleted"), show_alert=True)
    await replace(cb, config.ta("panel_menus_title"), menus_kb())
