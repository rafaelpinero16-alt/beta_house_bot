"""Secciones dinámicas: texto (ES/EN) + multimedia + botones configurables (tabla ui_buttons).

Callbacks que generan los botones:
  nav:<sección>             abrir sección
  a:<sección>:<botón>       acción del botón (alert, header, subscribe, receipt, tip, support, lang, url con ID de pago)
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services.telegram_service import send_rich

log = logging.getLogger(__name__)


def _action(row: dict) -> str:
    return config.LEGACY_ACTIONS.get(row["action_type"], row["action_type"])


def button_is_available(row: dict) -> bool:
    """Un botón activo se muestra solo si su destino existe y está disponible ahora mismo."""
    action, target = _action(row), row["target_value"] or ""
    if action == "menu":
        return config.get_menu(target) is not None
    if action == "url":
        return bool(config.resolve_button_url(target))
    if action == "alert":
        return bool(target.strip())
    if action == "subscribe":
        product = config.get_product(target)
        return bool(product and product.is_active and product.chat_id and config.stars_plans(target))
    if action == "receipt":
        if target:
            product = config.get_product(target)
            return bool(product and product.is_active and product.chat_id and config.ext_plans(target))
        return any(config.ext_plans(p.key) for p in config.active_products())
    if action == "tip":
        return config.TIPS_ENABLED and target.isdigit() and int(target) > 0
    return action in config.ALWAYS_AVAILABLE_ACTIONS


def to_inline(menu_id: str, row: dict, lang: str) -> InlineKeyboardButton | None:
    if not button_is_available(row):
        return None
    action, target = _action(row), row["target_value"] or ""
    name = config.render_button_name(
        row["name_en"] if lang == "en" else row["name_es"], amount=target if action == "tip" else ""
    )
    if action == "menu":
        return InlineKeyboardButton(text=name, callback_data=f"nav:{target}")
    if action == "url":
        url = config.resolve_button_url(target)
        if config.is_http_url(url):
            return InlineKeyboardButton(text=name, url=url)
    if action == "back":
        menu = config.get_menu(menu_id)
        return InlineKeyboardButton(text=name, callback_data=f"nav:{(menu.parent if menu else None) or 'main'}")
    if action == "home":
        return InlineKeyboardButton(text=name, callback_data="nav:main")
    if action == "header":  # botón divisor: solo texto; al pulsarlo muestra un aviso breve y no navega
        return InlineKeyboardButton(text=name, callback_data=f"a:{menu_id}:{row['button_id']}")
    return InlineKeyboardButton(text=name, callback_data=f"a:{menu_id}:{row['button_id']}")


def layout(items: list[tuple[InlineKeyboardButton, bool]]) -> list[list[InlineKeyboardButton]]:
    """Cada botón va en fila propia salvo que esté marcado como «misma fila que el anterior»."""
    rows: list[list[InlineKeyboardButton]] = []
    for button, same_row in items:
        if same_row and rows and len(rows[-1]) < config.BUTTONS_PER_ROW_MAX:
            rows[-1].append(button)
        else:
            rows.append([button])
    return rows


def back_row(lang: str, parent: str | None) -> list[list[InlineKeyboardButton]]:
    if not parent:
        return []
    key = "btn_home" if parent == "main" else "btn_back"
    return [[InlineKeyboardButton(text=config.t(lang, key), callback_data=f"nav:{parent}")]]


async def menu_keyboard(menu_id: str, lang: str) -> InlineKeyboardMarkup:
    menu = config.get_menu(menu_id)
    rows_db = (await db.get_buttons(menu_id, True))[: config.MAX_BUTTONS_PER_MENU]
    items = [(b, bool(r["same_row"])) for r in rows_db if (b := to_inline(menu_id, r, lang))]
    rows = layout(items)
    has_nav_back = any(_action(r) in ("back", "home") for r in rows_db)
    if menu and not has_nav_back:
        rows += back_row(lang, menu.parent)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(
    bot: Bot, chat_id: int, menu_id: str, lang: str, cb: CallbackQuery | None = None, **text_vars
) -> None:
    """Muestra una sección. Sin multimedia edita el mensaje actual; con multimedia envía uno nuevo."""
    menu = config.get_menu(menu_id)
    if menu is None:
        return
    text = config.menu_text(menu_id, lang, **text_vars)
    kb = await menu_keyboard(menu_id, lang)
    if cb is not None and not menu.media and len(text) <= config.TELEGRAM_TEXT_LIMIT and cb.message.text is not None:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    if cb is not None and menu.media:
        try:
            await cb.message.delete()  # el menú anterior se reemplaza por el nuevo mensaje con multimedia
        except TelegramBadRequest:
            pass
    await send_rich(bot, chat_id, text, menu.media, reply_markup=kb)
