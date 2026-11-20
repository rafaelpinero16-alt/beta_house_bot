"""Veto de miembros: el bot ignora todo lo que envíen y los saca de todos los chats vinculados."""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import config
import database as db
from services.access import revoke_user_access_link
from services.telegram_service import Contact, notify_topic

log = logging.getLogger(__name__)

BANNED: set[int] = set()  # caché en memoria para el middleware (se carga al arrancar)


async def load_banned() -> None:
    BANNED.clear()
    BANNED.update(await db.get_banned_ids())


def is_banned(user_id: int) -> bool:
    return user_id in BANNED


async def resolve_user(raw: str) -> int | None:
    """ID numérico o @usuario (solo de quien ya escribió al bot)."""
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        value = int(raw)
        return value if value > 0 else None
    if raw.startswith("@") and len(raw) > 1:
        row = await db.find_user_by_username(raw[1:])
        return row["user_id"] if row else None
    return None


async def ban_member(bot: Bot, user_id: int, reason: str | None, admin_id: int | None, admin_name: str) -> dict | None:
    """Veta al miembro: lo registra, revoca sus enlaces, cancela sus suscripciones, cierra su chat de soporte
    y lo veta en todos los chats vinculados. Devuelve {"links", "chats"} o None si ya estaba vetado."""
    if not await db.ban_user(user_id, reason, admin_id):
        return None
    BANNED.add(user_id)
    links = await revoke_user_access_link(bot, user_id)
    for membership in await db.get_user_active_memberships(user_id):
        await db.close_membership(membership["id"], "cancelled")
    await db.set_receipt_mode(user_id, None)
    await db.set_support_open(user_id, False)
    chats = 0
    for chat_id in config.product_chat_ids():
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            chats += 1
        except TelegramAPIError as exc:
            log.warning("No se pudo vetar a %s en %s: %s", user_id, chat_id, exc)
    contact = await Contact.from_db(user_id)
    await notify_topic(
        bot,
        contact,
        config.ta(
            "ban_topic", admin=html.escape(admin_name), reason=html.escape(reason or config.ta("ban_no_reason")),
            links=links, chats=chats,
        ),
    )
    log.info("Miembro %s vetado por %s (%s)", user_id, admin_id, reason)
    return {"links": links, "chats": chats}


async def unban_member(bot: Bot, user_id: int, admin_name: str) -> bool:
    if not await db.unban_user(user_id):
        return False
    BANNED.discard(user_id)
    for chat_id in config.product_chat_ids():
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        except TelegramAPIError as exc:
            log.warning("No se pudo levantar el veto de %s en %s: %s", user_id, chat_id, exc)
    contact = await Contact.from_db(user_id)
    await notify_topic(bot, contact, config.ta("unban_topic", admin=html.escape(admin_name)))
    return True


async def block_banned_join(bot: Bot, chat_id: int, user_id: int) -> None:
    """Un vetado entró a un chat vinculado: se le vuelve a sacar."""
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        log.warning("No se pudo retirar al vetado %s de %s: %s", user_id, chat_id, exc)
