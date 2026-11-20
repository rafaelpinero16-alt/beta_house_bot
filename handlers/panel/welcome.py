"""Módulo 1 · Bienvenida y anuncios: texto, multimedia y botones del mensaje de bienvenida, y anuncios."""
from __future__ import annotations

from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from handlers.panel.common import admin_router, media_label, replace

router = admin_router("panel_welcome")


async def show(cb: CallbackQuery) -> None:
    menu = config.get_menu("main")
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_welcome_text"), callback_data="panel:text:welcome")
    b.button(text=config.ta("btn_welcome_media"), callback_data="pm:o:main")
    b.button(text=config.ta("btn_welcome_buttons"), callback_data="pb:m:main")
    b.button(text=config.ta("btn_language_texts"), callback_data="panel:tc:welcome")
    b.button(text=config.ta("btn_posts"), callback_data="ps:list:announce")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(1)
    await replace(cb, config.ta("panel_welcome_title", media=media_label(menu.media if menu else "")), b.as_markup())
