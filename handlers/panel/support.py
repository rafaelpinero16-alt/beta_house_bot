"""Módulo 8 · Contactos directos y SUPPORT: modo del chat, enlaces de contacto, botones de las secciones de
soporte y de las respuestas automáticas, y textos. Las conversaciones se leen y se responden en el grupo de
gestión (un Tema por miembro), donde también se pueden finalizar."""
from __future__ import annotations

from aiogram import Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import admin_router, audit, replace

router = admin_router("panel_support")


def support_view() -> tuple[str, InlineKeyboardMarkup]:
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_support_mode"), callback_data="su:mode")
    b.button(text=config.ta("btn_support_links"), callback_data="panel:lc:support")
    b.button(text=config.ta("btn_custom_links"), callback_data="lk:list")
    b.button(text=config.ta("btn_support_menu"), callback_data=f"pb:m:{config.SUPPORT_MENU}")
    b.button(text=config.ta("btn_support_wait"), callback_data="pb:m:support_wait")
    b.button(text=config.ta("btn_receipt_wait"), callback_data="pb:m:receipt_wait")
    b.button(text=config.ta("btn_support_texts"), callback_data="panel:tc:support")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(1)
    return config.ta("panel_support_title", mode=config.ta(f"support_mode_{config.support_mode()}")), b.as_markup()


async def show(cb: CallbackQuery) -> None:
    await replace(cb, *support_view())


@router.callback_query(F.data == "su:home")
async def home(cb: CallbackQuery) -> None:
    await cb.answer()
    await show(cb)


@router.callback_query(F.data == "su:mode")
async def toggle_mode(cb: CallbackQuery, bot: Bot) -> None:
    old = config.support_mode()
    new = "always" if old == "button" else "button"
    await db.set_config("support_mode", new)
    config.apply_override("support_mode", new)
    await audit(bot, config.SETTINGS["support_mode"].label, old, new, cb.from_user.full_name)
    await cb.answer(config.ta(f"support_mode_{new}"))
    await show(cb)
