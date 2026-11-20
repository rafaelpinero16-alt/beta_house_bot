"""Piezas compartidas del /panel: routers solo-admin, estados FSM, utilidades de teclado y auditoría."""
from __future__ import annotations

import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from services.telegram_service import log_sale

log = logging.getLogger("panel")
NOT_COMMAND = ~F.text.startswith("/")
AUDIT_MAX = 180
PREVIEW_MAX = 3000
FORMATTING_ENTITIES = {
    "bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre",
    "text_link", "text_mention", "blockquote", "expandable_blockquote", "custom_emoji",
}
HTML_TAG_RE = re.compile(r"</?(b|strong|i|em|u|ins|s|strike|del|code|pre|a|blockquote|tg-spoiler|span)\b", re.I)


def admin_router(name: str) -> Router:
    """Router cuyos handlers solo responden a ADMIN_IDS en su chat privado con el bot."""
    router = Router(name=name)
    router.message.filter(F.chat.type == "private", F.from_user.id.in_(config.ADMIN_IDS))
    router.callback_query.filter(F.message.chat.type == "private", F.from_user.id.in_(config.ADMIN_IDS))
    return router


# ---- Estados FSM de todo el panel ----------------------------------------------
class SettingEdit(StatesGroup):
    waiting_value = State()
    confirming = State()


class MenuEdit(StatesGroup):
    text = State()
    media = State()
    new = State()
    rename = State()


class ButtonEdit(StatesGroup):
    waiting = State()


class ProductEdit(StatesGroup):
    waiting = State()


class LinkEdit(StatesGroup):
    new = State()
    url = State()


class TipEdit(StatesGroup):
    amount = State()


class PostWizard(StatesGroup):
    content = State()
    buttons = State()
    target = State()
    when = State()


class BanAdd(StatesGroup):
    waiting = State()


class CloneWizard(StatesGroup):
    token = State()
    admin_group = State()
    vip = State()
    sales = State()
    tips = State()
    admins = State()
    confirm = State()


ALL_STATES = (SettingEdit, MenuEdit, ButtonEdit, ProductEdit, LinkEdit, TipEdit, PostWizard, BanAdd, CloneWizard)


# ---- Utilidades ------------------------------------------------------------------
def grid(builder: InlineKeyboardBuilder, items: int, columns: int = 2, tail: int = 1) -> None:
    """Opciones en filas de `columns`; los últimos `tail` botones (volver, cerrar…) en fila propia."""
    rows = [columns] * (items // columns) + ([items % columns] if items % columns else [])
    builder.adjust(*rows, *([1] * tail))


def flag(key: str) -> str:
    return config.ta("flag_custom" if config.setting_is_custom(key) else "flag_default")


def short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def media_label(value: str) -> str:
    return value.split(":", 1)[0] if value else config.ta("panel_none")


def back_kb(target: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_panel_back"), callback_data=target)
    return b.as_markup()


def yes_no(value: bool) -> str:
    return config.ta("perm_yes" if value else "perm_no")


async def replace(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    """Edita el mensaje del panel; si no se puede, envía uno nuevo."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await cb.message.answer(text, reply_markup=kb)


def template_source(message: Message) -> str:
    """Formato nativo de Telegram → HTML; HTML fuente pegado sin formato → tal cual."""
    has_formatting = any(e.type in FORMATTING_ENTITIES for e in (message.entities or message.caption_entities or ()))
    raw = message.text or message.caption or ""
    if not has_formatting and HTML_TAG_RE.search(raw):
        return raw
    return message.html_text if message.text is not None else (message.html_text or "")


async def audit(bot: Bot, label: str, old: str, new: str, admin_name: str) -> None:
    """Registra cada cambio del panel en el canal de ventas (#CONFIG)."""
    log.info("Ajuste '%s' modificado por %s", label, admin_name)
    await log_sale(
        bot,
        config.SALES_LOG_CHANNEL_ID,
        config.ta(
            "panel_audit_log", label=html.escape(label),
            old=html.escape(short(old, AUDIT_MAX)) or "—", new=html.escape(short(new, AUDIT_MAX)) or "—",
            admin=html.escape(admin_name),
        ),
    )


async def refresh_setting_cache(key: str) -> None:
    row = (await db.get_all_config()).get(key)
    if row:
        config.apply_override(key, row["value"], row["updated_at"])
    else:
        config.drop_override(key)


class Sample(dict):
    """Rellena variables desconocidas con [nombre] para las vistas previas."""

    def __missing__(self, key: str) -> str:
        return f"[{key}]"
