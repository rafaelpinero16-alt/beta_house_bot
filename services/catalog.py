"""Catálogo dinámico: productos (grupos/canales con suscripción), planes, secciones y enlaces personalizados.

Mantiene las cachés de config sincronizadas con la base de datos y gestiona la vinculación de chats:
cuando el bot es agregado como administrador a un grupo o canal, lo registra y avisa a los admins.
"""
from __future__ import annotations

import html
import logging
import re
import unicodedata

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services.roles import chat_setup_issues

log = logging.getLogger(__name__)

# Permisos que se piden al agregar el bot desde el enlace del panel (deep link de Telegram con derechos).
GROUP_RIGHTS = "invite_users+restrict_members+delete_messages+manage_chat"
CHANNEL_RIGHTS = "invite_users+restrict_members+post_messages+manage_chat"


# ===========================================================================
# Cachés
# ===========================================================================
async def reload_products() -> None:
    products = [
        config.Product(
            key=r["key"], chat_id=r["chat_id"] or 0, chat_type=r["chat_type"] or "", name_es=r["name_es"],
            name_en=r["name_en"], is_active=bool(r["is_active"]), can_invite=bool(r["can_invite"]),
            can_restrict=bool(r["can_restrict"]), member_role=r.get("member_role") or "none",
        )
        for r in await db.list_products()
    ]
    plans = [
        config.Plan(
            product=r["product_key"], months=r["months"], stars_price=r["stars_price"], ext_price=r["ext_price"] or "",
            is_active=bool(r["is_active"]),
        )
        for r in await db.list_plans()
    ]
    config.set_products(products, plans)


async def reload_menus() -> None:
    config.set_menus([
        config.Menu(
            menu_id=r["menu_id"], title=r["title"], parent=r["parent"], text_es=r["text_es"], text_en=r["text_en"],
            media=r["media"] or "", is_system=bool(r["is_system"]),
        )
        for r in await db.list_menus()
    ])


async def reload_links() -> None:
    config.set_custom_links({r["key"]: (r["label"], r["url"]) for r in await db.list_custom_links()})


async def reload_all() -> None:
    await reload_products()
    await reload_menus()
    await reload_links()


# ===========================================================================
# Productos
# ===========================================================================
def _slug(text: str, limit: int = 16) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return slug[:limit].strip("_") or "producto"


async def unique_product_key(title: str) -> str:
    base = _slug(title)
    key, number = base, 2
    while await db.product_key_exists(key):
        key = f"{base[:13]}_{number}"
        number += 1
    return key


def _admin_perms(member, chat_type: str) -> tuple[bool, bool, bool]:
    """(invitar, expulsar, publicar) del bot en el chat."""
    if member.status == ChatMemberStatus.CREATOR:
        return True, True, True
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, False, chat_type != ChatType.CHANNEL and member.status == ChatMemberStatus.MEMBER
    can_post = bool(getattr(member, "can_post_messages", False)) if chat_type == ChatType.CHANNEL else True
    return bool(getattr(member, "can_invite_users", False)), bool(getattr(member, "can_restrict_members", False)), can_post


async def refresh_chat(bot: Bot, chat_id: int) -> dict:
    """Consulta el chat y los permisos del bot, y actualiza linked_chats y TODOS los productos de ese chat."""
    chat = await bot.get_chat(chat_id)
    member = await bot.get_chat_member(chat_id, bot.id)
    invite, restrict, post = _admin_perms(member, chat.type)
    await db.upsert_linked_chat(chat_id, chat.title or str(chat_id), chat.type, member.status, invite, restrict, post)
    products = await db.get_products_by_chat(chat_id)
    for product in products:
        await db.update_product(product["key"], can_invite=int(invite), can_restrict=int(restrict), chat_type=chat.type)
    if products:
        await reload_products()
    return {"title": chat.title or str(chat_id), "type": chat.type, "invite": invite, "restrict": restrict}


async def link_chat(bot: Bot, chat_id: int, extra: bool = False) -> tuple[str, bool]:
    """Vincula un chat como producto (planes creados desactivados). Devuelve (clave, ya_existía).

    Con `extra=True` crea otro producto (otra tarifa/rol) sobre un chat que ya estaba vinculado.
    """
    existing = await db.get_products_by_chat(chat_id)
    if existing and not extra:
        return existing[0]["key"], True
    info = await refresh_chat(bot, chat_id)
    key = await unique_product_key(info["title"])
    await db.create_product(
        key, chat_id, info["type"], info["title"][:64], info["title"][:64], info["invite"], info["restrict"],
        config.PLAN_MONTHS,
    )
    await reload_products()
    log.info("Chat %s vinculado como producto %s", chat_id, key)
    return key, False


async def check_product(bot: Bot, product_key: str) -> tuple[bool, str]:
    """Verifica que el bot pueda crear enlaces (obligatorio), expulsar/restringir (vencimientos y roles) y, en los
    chats con roles, que los permisos generales del grupo dejen publicar a los creadores."""
    product = config.get_product(product_key)
    if not product or not product.chat_id:
        return False, config.ta("product_unavailable")
    try:
        info = await refresh_chat(bot, product.chat_id)
    except TelegramAPIError as exc:
        return False, html.escape(exc.message)
    if not info["invite"]:
        return False, config.ta("chat_missing_perms", title=html.escape(info["title"]))
    if not info["restrict"] and info["type"] != ChatType.CHANNEL:
        return False, config.ta("chat_missing_perms", title=html.escape(info["title"]))
    if product.member_role in ("creator", "viewer") and info["type"] != ChatType.CHANNEL:
        issues = await chat_setup_issues(bot, product.chat_id, html.escape(info["title"]))
        if issues:
            return False, "\n".join(issues)
    return True, info["title"]


def add_bot_url(bot_username: str, kind: str) -> str:
    """Enlace que abre «agregar a grupo/canal» con los permisos de administrador ya marcados."""
    if kind == "channel":
        return f"https://t.me/{bot_username}?startchannel&admin={CHANNEL_RIGHTS}"
    return f"https://t.me/{bot_username}?startgroup=betahouse&admin={GROUP_RIGHTS}"


# ===========================================================================
# Eventos: el bot fue agregado / quitado / promovido en un chat
# ===========================================================================
async def on_bot_status_change(bot: Bot, chat: Chat, member) -> None:
    if chat.id == config.ADMIN_GROUP_ID or chat.type == ChatType.PRIVATE:
        return
    invite, restrict, post = _admin_perms(member, chat.type)
    status = member.status.value if hasattr(member.status, "value") else str(member.status)
    previous = await db.get_linked_chat(chat.id)
    await db.upsert_linked_chat(chat.id, chat.title or str(chat.id), chat.type, status, invite, restrict, post)
    products = await db.get_products_by_chat(chat.id)
    for product in products:
        await db.update_product(product["key"], can_invite=int(invite), can_restrict=int(restrict), chat_type=chat.type)

    kind = config.ta("chat_kind_channel" if chat.type == ChatType.CHANNEL else "chat_kind_group")
    title = html.escape(chat.title or str(chat.id))
    is_admin = status in ("administrator", "creator")
    was_admin = bool(previous and previous["status"] in ("administrator", "creator"))

    if is_admin and not products:
        yes, no = config.ta("perm_yes"), config.ta("perm_no")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=config.ta("btn_link_chat"), callback_data=f"pp:link:{chat.id}"),
            InlineKeyboardButton(text=config.ta("btn_ignore"), callback_data="pp:ignore"),
        ]])
        text = config.ta(
            "chat_added_dm", title=title, kind=kind, chat_id=chat.id,
            invite=yes if invite else no, restrict=yes if restrict else no,
        )
        await _dm_admins(bot, text, kb)
    elif was_admin and not is_admin:
        note = ""
        for product in products:
            if product["is_active"]:
                await db.update_product(product["key"], is_active=0)
                note += ("\n" if note else "") + config.ta("chat_removed_product_note", product=html.escape(product["name_es"]))
        await _dm_admins(bot, config.ta("chat_removed_dm", title=title, chat_id=chat.id, note=note))
    await reload_products()


async def _dm_admins(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except TelegramAPIError:
            log.debug("No se pudo avisar al admin %s", admin_id)
