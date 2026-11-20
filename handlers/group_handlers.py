"""Moderación por rol dentro de los chats con suscripción (BETA HOUSE).

Red de seguridad: los permisos de Telegram ya impiden publicar a los espectadores, pero si algo falla
(restricción no aplicada, miembro que entró por otra vía, mensaje «enviado como canal»…), el bot borra lo
que publique alguien que no es creador, vuelve a aplicar su rol y le explica por privado por qué.

Nunca toca mensajes de administradores, del propio grupo (admins anónimos), del canal vinculado ni de servicio.
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

import config
import database as db
from services import roles

log = logging.getLogger(__name__)
router = Router(name="group_roles")

# Contenido publicado por miembros (los mensajes de servicio —entradas, videochats, fijados…— no se moderan).
POSTABLE_TYPES = {
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.ANIMATION, ContentType.DOCUMENT,
    ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO_NOTE, ContentType.STICKER, ContentType.POLL,
    ContentType.CONTACT, ContentType.LOCATION, ContentType.VENUE, ContentType.DICE, ContentType.STORY,
    ContentType.PAID_MEDIA,
}
_LINK_ENTITIES = {"url", "text_link", "mention", "text_mention"}
_NOTICE_INTERVAL = 3600.0          # aviso por privado como máximo 1 vez por hora y miembro
_noticed: dict[int, float] = {}


def _in_role_chat(message: Message) -> bool:
    return message.chat.id in config.role_chat_ids()


def _has_link(message: Message) -> bool:
    entities = (message.entities or []) + (message.caption_entities or [])
    return any(e.type in _LINK_ENTITIES for e in entities)


def _allowed_for_viewer(message: Message) -> bool:
    """En modo «solo texto» el espectador puede escribir texto plano, sin enlaces ni multimedia."""
    return (
        config.viewer_mode() == "text"
        and message.content_type == ContentType.TEXT
        and not _has_link(message)
        and message.forward_origin is None
    )


async def _notify_viewer(bot: Bot, user_id: int) -> None:
    now = time.monotonic()
    if user_id in _noticed and now - _noticed[user_id] < _NOTICE_INTERVAL:
        return
    _noticed[user_id] = now
    try:
        await bot.send_message(user_id, config.t(await db.get_lang(user_id), "viewer_blocked"))
    except TelegramAPIError:
        pass   # el miembro no ha abierto el bot


async def _moderate(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    if message.content_type not in POSTABLE_TYPES or message.is_automatic_forward:
        return
    if message.sender_chat is not None:
        if message.sender_chat.id == chat_id:
            return   # administrador anónimo
        # Publicado «como canal»: no se puede atribuir a un creador → se borra.
        await _delete(message)
        return
    user = message.from_user
    if user is None or user.is_bot or user.id in config.ADMIN_IDS:
        return
    if user.id in await roles.chat_admin_ids(bot, chat_id):
        return
    role = await roles.effective_role(user.id, chat_id)
    if role == "creator" or role == "none" or (role == "viewer" and _allowed_for_viewer(message)):
        return
    await _delete(message)
    if roles.last_applied(chat_id, user.id) != role:
        await roles.apply_role(bot, chat_id, user.id, role)
    await _notify_viewer(bot, user.id)
    log.info("Mensaje de %s borrado en %s (rol %s)", user.id, chat_id, role)


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as exc:
        log.warning("No se pudo borrar el mensaje %s en %s: %s", message.message_id, message.chat.id, exc.message)


@router.message(F.func(_in_role_chat))
async def on_group_message(message: Message, bot: Bot) -> None:
    await _moderate(message, bot)


@router.edited_message(F.func(_in_role_chat))
async def on_group_edit(message: Message, bot: Bot) -> None:
    await _moderate(message, bot)
