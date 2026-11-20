"""Helpers de Telegram: temas del foro (Livegram), reenvío de mensajes, multimedia, textos largos y registros.

Los accesos y vencimientos viven en services/access.py, los vetos en services/moderation.py,
el catálogo de productos en services/catalog.py, las secciones en services/menus.py y los anuncios
en services/broadcast.py.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, User

import config
import database as db

log = logging.getLogger(__name__)
T = TypeVar("T")

# Tipos que se replican en ambos sentidos (mensajes de servicio se ignoran).
SUPPORTED_CONTENT_TYPES = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.DOCUMENT,
    ContentType.VOICE,
    ContentType.STICKER,
    ContentType.VIDEO,
    ContentType.VIDEO_NOTE,
    ContentType.AUDIO,
    ContentType.ANIMATION,
    ContentType.CONTACT,
    ContentType.LOCATION,
}

_TOPIC_GONE = ("thread not found", "topic_id_invalid", "topic_deleted")
_TOPIC_CLOSED = ("topic_closed",)

# Un candado por usuario: evita crear dos temas si escribe varias veces a la vez.
_topic_locks: dict[int, asyncio.Lock] = {}


# ===========================================================================
# Utilidades generales
# ===========================================================================
@dataclass(slots=True)
class Contact:
    id: int
    full_name: str
    username: str | None = None

    @classmethod
    def from_user(cls, user: User) -> "Contact":
        return cls(user.id, user.full_name, user.username)

    @classmethod
    async def from_db(cls, user_id: int) -> "Contact":
        row = await db.get_user(user_id)
        if row:
            return cls(user_id, row["full_name"] or str(user_id), row["username"])
        return cls(user_id, str(user_id), None)

    @property
    def mention(self) -> str:
        return f'<a href="tg://user?id={self.id}">{html.escape(self.full_name)}</a>'

    @property
    def card(self) -> str:
        username = f"@{html.escape(self.username)}" if self.username else config.ta("no_username")
        return config.ta("contact_card", mention=self.mention, username=username, user_id=self.id)


async def is_team_admin(bot: Bot, user_id: int) -> bool:
    """Admin = ID listado en ADMIN_IDS o administrador/creador del grupo de soporte."""
    if user_id in config.ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(config.ADMIN_GROUP_ID, user_id)
    except TelegramAPIError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


async def is_admin_message(bot: Bot, message: Message) -> bool:
    """Admite también a administradores anónimos (mensaje firmado por el propio grupo)."""
    if message.sender_chat and message.sender_chat.id == config.ADMIN_GROUP_ID:
        return True
    return bool(message.from_user) and await is_team_admin(bot, message.from_user.id)


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> bool:
    if not message_id:
        return False
    try:
        return await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        log.debug("No se pudo borrar el mensaje %s en %s: %s", message_id, chat_id, exc)
        return False


async def log_sale(bot: Bot, channel_id: int, text: str) -> None:
    """Publica un asiento contable en el canal indicado (ventas VIP o propinas). Nunca rompe el flujo."""
    if not channel_id:
        return
    try:
        await bot.send_message(chat_id=channel_id, text=text)
    except TelegramAPIError:
        log.exception("No se pudo publicar en el canal de registro %s.", channel_id)


# ===========================================================================
# Temas del foro (soporte estilo Livegram)
# ===========================================================================
def support_end_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=config.ta("btn_support_end"), callback_data=f"sup:end:{user_id}")
    ]])


async def _create_forum_topic(bot: Bot, contact: Contact) -> int:
    name = f"{contact.full_name} [{contact.id}]"[:128]
    topic = await bot.create_forum_topic(chat_id=config.ADMIN_GROUP_ID, name=name)
    topic_id = topic.message_thread_id
    await db.save_topic(contact.id, topic_id, contact.full_name, contact.username)
    await bot.send_message(
        chat_id=config.ADMIN_GROUP_ID,
        message_thread_id=topic_id,
        text=config.ta(
            "topic_header",
            card=contact.card,
            products=", ".join(f"<code>{p.key}</code>" for p in config.active_products()) or "—",
        ),
        reply_markup=support_end_kb(contact.id),
    )
    log.info("Tema %s creado para el usuario %s", topic_id, contact.id)
    return topic_id


async def get_or_create_topic(bot: Bot, contact: Contact) -> int:
    lock = _topic_locks.setdefault(contact.id, asyncio.Lock())
    async with lock:
        topic_id = await db.get_topic_id(contact.id)
        if topic_id:
            return topic_id
        return await _create_forum_topic(bot, contact)


async def with_topic(bot: Bot, contact: Contact, action: Callable[[int], Awaitable[T]]) -> T:
    """Ejecuta `action(topic_id)`. Si el tema fue borrado lo recrea; si está cerrado lo reabre."""
    for attempt in range(2):
        topic_id = await get_or_create_topic(bot, contact)
        try:
            return await action(topic_id)
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if attempt == 0 and any(k in err for k in _TOPIC_GONE):
                log.warning("Tema %s inexistente; se recreará para %s", topic_id, contact.id)
                await db.delete_topic(contact.id)
                continue
            if attempt == 0 and any(k in err for k in _TOPIC_CLOSED):
                await bot.reopen_forum_topic(chat_id=config.ADMIN_GROUP_ID, message_thread_id=topic_id)
                continue
            raise
    raise RuntimeError("No fue posible entregar el mensaje al tema del usuario.")


async def relay_to_topic(bot: Bot, contact: Contact, message: Message) -> None:
    """Usuario -> su tema. copy_message conserva texto, foto, documento, voz, sticker, etc."""
    await with_topic(
        bot,
        contact,
        lambda topic_id: bot.copy_message(
            chat_id=config.ADMIN_GROUP_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=topic_id,
        ),
    )


async def relay_to_user(bot: Bot, user_id: int, message: Message) -> None:
    """Tema -> chat privado del usuario (anónimo: no revela qué admin respondió)."""
    await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)


async def close_topic(bot: Bot, user_id: int) -> None:
    topic_id = await db.get_topic_id(user_id)
    if not topic_id:
        return
    try:
        await bot.close_forum_topic(chat_id=config.ADMIN_GROUP_ID, message_thread_id=topic_id)
    except TelegramAPIError as exc:
        log.debug("No se pudo cerrar el tema %s: %s", topic_id, exc)


async def notify_topic(
    bot: Bot, contact: Contact, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Publica un aviso del sistema en el tema del usuario (nunca rompe el flujo principal)."""
    try:
        return await with_topic(
            bot,
            contact,
            lambda topic_id: bot.send_message(
                chat_id=config.ADMIN_GROUP_ID,
                message_thread_id=topic_id,
                text=text,
                reply_markup=reply_markup,
            ),
        )
    except Exception:
        log.exception("No se pudo notificar en el tema del usuario %s", contact.id)
        return None


# ===========================================================================
# Envío de contenido: textos largos (sin límite) y multimedia
# ===========================================================================
def split_text(text: str, limit: int = config.TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Divide un texto largo en trozos ≤ limit, cortando por párrafos y luego por líneas."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        piece = block if not current else "\n\n" + block
        if len(current) + len(piece) <= limit:
            current += piece
            continue
        if current:
            chunks.append(current)
        current = ""
        for line in block.split("\n"):
            piece = line if not current else "\n" + line
            if len(current) + len(piece) <= limit:
                current += piece
            else:
                if current:
                    chunks.append(current)
                current = line[:limit]  # una sola línea gigante se recorta (caso extremo)
    if current:
        chunks.append(current)
    return chunks


async def send_long(
    bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None, **kwargs
) -> Message:
    """Envía un texto de cualquier longitud. Los botones van en el último mensaje, que es el que se devuelve."""
    parts = split_text(text)
    for part in parts[:-1]:
        await bot.send_message(chat_id, part, **kwargs)
    return await bot.send_message(chat_id, parts[-1], reply_markup=reply_markup, **kwargs)


async def send_media(bot: Bot, chat_id: int, kind: str, file_id: str) -> Message:
    senders = {
        "photo": lambda: bot.send_photo(chat_id, photo=file_id),
        "video": lambda: bot.send_video(chat_id, video=file_id),
        "animation": lambda: bot.send_animation(chat_id, animation=file_id),
        "sticker": lambda: bot.send_sticker(chat_id, sticker=file_id),
    }
    return await senders[kind]()


async def send_rich(
    bot: Bot, chat_id: int, text: str, media: str = "", reply_markup: InlineKeyboardMarkup | None = None, **kwargs
) -> Message:
    """Texto (de cualquier longitud) con multimedia opcional ("tipo:file_id") y botones en el último mensaje.
    Si el texto cabe como pie de foto, se envía todo en un solo mensaje."""
    if media and ":" in media:
        kind, file_id = media.split(":", 1)
        if kind in ("photo", "video", "animation") and len(text) <= config.CAPTION_LIMIT:
            sender = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation}[kind]
            return await sender(chat_id, file_id, caption=text or None, reply_markup=reply_markup, **kwargs)
        try:
            await send_media(bot, chat_id, kind, file_id)
        except TelegramAPIError:
            log.exception("No se pudo enviar la multimedia %s", kind)
    return await send_long(bot, chat_id, text, reply_markup=reply_markup, **kwargs)


def media_from_message(message: Message) -> str | None:
    """'tipo:file_id' de una foto, video, GIF o sticker; None si el mensaje no trae multimedia válida."""
    if message.photo:
        return f"photo:{message.photo[-1].file_id}"
    if message.animation:  # va antes que document: un GIF también trae document
        return f"animation:{message.animation.file_id}"
    if message.video:
        return f"video:{message.video.file_id}"
    if message.sticker:
        return f"sticker:{message.sticker.file_id}"
    return None


# ===========================================================================
# Donaciones
# ===========================================================================
async def announce_tip(bot: Bot, contact: Contact, tip_id: int, amount: int, charge_id: str) -> None:
    """Avisa al equipo en el tema del miembro y registra la donación en su canal contable."""
    user_total = await db.get_user_tip_total(contact.id)
    await notify_topic(
        bot,
        contact,
        config.ta(
            "tip_topic",
            amount=amount,
            tip_id=tip_id,
            user_total=user_total,
            charge_id=html.escape(charge_id),
        ),
    )
    await log_sale(
        bot,
        config.TIPS_LOG_CHANNEL_ID,
        config.ta(
            "tip_log",
            mention=contact.mention,
            user_id=contact.id,
            amount=amount,
            grand_total=await db.get_tips_total(),
        ),
    )
