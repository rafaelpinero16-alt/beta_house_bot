"""Moderación por rol dentro de los chats con suscripción (BETA HOUSE).

Red de seguridad: los permisos de Telegram ya impiden publicar a los espectadores, pero si algo falla
(restricción no aplicada, miembro que entró por otra vía, mensaje «enviado como canal»…), el bot borra lo
que publique alguien que no es creador, vuelve a aplicar su rol y le explica por privado por qué.

Nunca toca mensajes de administradores, del propio grupo (admins anónimos), del canal vinculado ni de servicio.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

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
_channel_noticed: dict[int, float] = {}
_videochat_sent: dict[int, float] = {}
_VIDEOCHAT_INTERVAL = 1800.0       # un aviso de videochat como máximo cada 30 min por grupo
_tasks: set[asyncio.Task] = set()


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
        # Publicado «como canal»: solo se permite si el canal está autorizado para un creador con plan activo.
        if await roles.channel_may_post(message.sender_chat.id, chat_id):
            return
        await _delete(message)
        await _notify_team_channel(bot, message)
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


async def _notify_team_channel(bot: Bot, message: Message) -> None:
    """Avisa al grupo de gestión (una vez por hora y canal) para que puedan autorizarlo con /canal."""
    channel = message.sender_chat
    now = time.monotonic()
    if channel.id in _channel_noticed and now - _channel_noticed[channel.id] < _NOTICE_INTERVAL:
        return
    _channel_noticed[channel.id] = now
    try:
        await bot.send_message(config.ADMIN_GROUP_ID, config.ta(
            "channel_post_deleted", chat=html.escape(config.chat_label(message.chat.id)),
            title=html.escape(channel.title or str(channel.id)), channel_id=channel.id,
        ))
    except TelegramAPIError as exc:
        log.warning("No se pudo avisar del canal %s: %s", channel.id, exc.message)


def _message_link(message: Message) -> str:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"
    internal = str(message.chat.id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{message.message_id}"


async def _videochat_alert(bot: Bot, message: Message) -> None:
    """Videochat iniciado en el grupo: aviso por privado a los miembros con suscripción activa."""
    chat_id = message.chat.id
    now = time.monotonic()
    if not config.videochat_alert_enabled() or (
        chat_id in _videochat_sent and now - _videochat_sent[chat_id] < _VIDEOCHAT_INTERVAL
    ):
        return
    _videochat_sent[chat_id] = now
    url = _message_link(message)
    sent = 0
    for user_id in await db.list_active_members_in_chat(chat_id):
        lang = await db.get_lang(user_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=config.t(lang, "btn_videochat_join"), url=url)
        ]])
        try:
            await bot.send_message(user_id, config.t(lang, "videochat_live"), reply_markup=kb)
            sent += 1
        except TelegramAPIError:
            pass
        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)
    log.info("Aviso de videochat en %s enviado a %s miembros", chat_id, sent)


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as exc:
        log.warning("No se pudo borrar el mensaje %s en %s: %s", message.message_id, message.chat.id, exc.message)


@router.message(F.func(_in_role_chat))
async def on_group_message(message: Message, bot: Bot) -> None:
    if message.video_chat_started is not None:
        task = asyncio.create_task(_videochat_alert(bot, message))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return
    await _moderate(message, bot)


@router.edited_message(F.func(_in_role_chat))
async def on_group_edit(message: Message, bot: Bot) -> None:
    await _moderate(message, bot)
