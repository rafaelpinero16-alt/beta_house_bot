"""Roles dentro de los chats con suscripción (BETA HOUSE): quién puede publicar y quién solo puede leer.

Cada producto tiene un `member_role`:
  creator → puede publicar texto, multimedia y enlaces (sus promociones).
  viewer  → solo lectura (o solo texto sin multimedia ni enlaces, según el ajuste `viewer_mode`).
  none    → el bot no toca los permisos del miembro.

Un miembro puede tener varias suscripciones activas en el mismo chat (p. ej. espectador y luego creador):
manda la de mayor rango (creator > viewer). Al vencer la de creador, si sigue activa la de espectador,
el bot lo baja a solo lectura en lugar de expulsarlo.

Requisito del grupo: los permisos GENERALES deben permitir enviar mensajes, multimedia y enlaces (si no,
tampoco los creadores podrían). El bot restringe a cada espectador de forma individual al entrar.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import ChatPermissions

import config
import database as db

log = logging.getLogger(__name__)

ROLE_RANK = {"creator": 2, "viewer": 1, "none": 0}
_ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
_IN_CHAT_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}

# Caché de lo último aplicado por (chat, usuario) para no repetir llamadas a Telegram.
_applied: dict[tuple[int, int], tuple[str, float]] = {}
_APPLIED_TTL = 600.0
# Caché de administradores por chat (los mensajes de admins nunca se moderan).
_admins: dict[int, tuple[set[int], float]] = {}
_ADMINS_TTL = 300.0


# ===========================================================================
# Permisos
# ===========================================================================
def creator_permissions() -> ChatPermissions:
    """Publicar todo (texto, multimedia, enlaces, encuestas, stickers). Sin permisos de administración."""
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
        can_change_info=False, can_pin_messages=False, can_manage_topics=False,
    )


def viewer_permissions() -> ChatPermissions:
    """Solo lectura (por defecto) o solo texto sin multimedia ni vistas previas de enlaces."""
    text_only = config.viewer_mode() == "text"
    return ChatPermissions(
        can_send_messages=text_only, can_send_audios=False, can_send_documents=False, can_send_photos=False,
        can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False,
        can_send_other_messages=False, can_add_web_page_previews=False, can_invite_users=False,
        can_change_info=False, can_pin_messages=False, can_manage_topics=False,
    )


def permissions_for(role: str) -> ChatPermissions | None:
    if role == "creator":
        return creator_permissions()
    if role == "viewer":
        return viewer_permissions()
    return None


def role_tag(role: str) -> str:
    return config.ta(f"role_tag_{role}" if role in config.MEMBER_ROLES else "role_tag_none")


# ===========================================================================
# Rol efectivo
# ===========================================================================
def is_role_chat(chat_id: int) -> bool:
    return chat_id in config.role_chat_ids()


async def effective_role(user_id: int, chat_id: int) -> str:
    """Rol que corresponde al miembro en el chat según sus suscripciones activas.

    En un chat con roles, quien no tiene suscripción activa (p. ej. entró por otro medio) queda como espectador:
    así nadie publica sin haber pagado como creador.
    """
    best = "none"
    for membership in await db.get_active_memberships_in_chat(user_id, chat_id):
        product = config.get_product(membership["product"])
        role = product.member_role if product else "none"
        if ROLE_RANK.get(role, 0) > ROLE_RANK[best]:
            best = role
    if best == "none" and is_role_chat(chat_id):
        return "viewer"
    return best


async def _member_status(bot: Bot, chat_id: int, user_id: int):
    try:
        return (await bot.get_chat_member(chat_id, user_id)).status
    except TelegramAPIError:
        return None


async def apply_role(bot: Bot, chat_id: int, user_id: int, role: str | None = None) -> tuple[str, str | None]:
    """Aplica en Telegram los permisos del rol del miembro.

    Devuelve (rol, resultado): resultado None = aplicado · "skip" = admin o fuera del chat · otro = error.
    """
    if not is_role_chat(chat_id):
        return "none", "skip"
    role = role or await effective_role(user_id, chat_id)
    permissions = permissions_for(role)
    if permissions is None:
        return role, "skip"
    status = await _member_status(bot, chat_id, user_id)
    if status not in _IN_CHAT_STATUSES:
        return role, "skip"   # admins (no se pueden restringir), fuera del chat o desconocido
    for attempt in range(2):
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id, permissions=permissions, use_independent_chat_permissions=True,
            )
            _applied[(chat_id, user_id)] = (role, time.monotonic())
            log.info("Rol %s aplicado a %s en %s", role, user_id, chat_id)
            return role, None
        except TelegramRetryAfter as exc:
            if attempt:
                return role, exc.message
            await asyncio.sleep(exc.retry_after)
        except TelegramAPIError as exc:
            log.warning("No se pudo aplicar el rol %s a %s en %s: %s", role, user_id, chat_id, exc.message)
            return role, exc.message
    return role, "retry"


def last_applied(chat_id: int, user_id: int) -> str | None:
    cached = _applied.get((chat_id, user_id))
    if cached and time.monotonic() - cached[1] < _APPLIED_TTL:
        return cached[0]
    return None


def forget(chat_id: int, user_id: int) -> None:
    _applied.pop((chat_id, user_id), None)


async def apply_user_roles(bot: Bot, user_id: int) -> list[tuple[int, str, str | None]]:
    """Reaplica el rol del miembro en todos los chats con roles donde tiene suscripción activa."""
    results = []
    chats = {m["chat_id"] for m in await db.get_user_active_memberships(user_id)} & config.role_chat_ids()
    for chat_id in sorted(chats):
        role, result = await apply_role(bot, chat_id, user_id)
        results.append((chat_id, role, result))
    return results


async def sync_chat(bot: Bot, chat_id: int) -> dict[str, int]:
    """Reaplica los permisos de todos los miembros con suscripción activa en el chat (tras cambiar roles/ajustes)."""
    counts = {"ok": 0, "fail": 0, "skipped": 0}
    for user_id in await db.list_active_members_in_chat(chat_id):
        _, result = await apply_role(bot, chat_id, user_id)
        counts["ok" if result is None else "skipped" if result == "skip" else "fail"] += 1
        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)
    return counts


async def sync_all(bot: Bot) -> dict[str, int]:
    total = {"ok": 0, "fail": 0, "skipped": 0}
    for chat_id in sorted(config.role_chat_ids()):
        for key, value in (await sync_chat(bot, chat_id)).items():
            total[key] += value
    return total


# ===========================================================================
# Administradores y configuración del grupo
# ===========================================================================
async def chat_admin_ids(bot: Bot, chat_id: int) -> set[int]:
    cached = _admins.get(chat_id)
    if cached and time.monotonic() - cached[1] < _ADMINS_TTL:
        return cached[0]
    try:
        admins = {m.user.id for m in await bot.get_chat_administrators(chat_id)}
    except TelegramAPIError:
        admins = cached[0] if cached else set()
    _admins[chat_id] = (admins, time.monotonic())
    return admins


def invalidate_admins(chat_id: int) -> None:
    _admins.pop(chat_id, None)


async def chat_setup_issues(bot: Bot, chat_id: int, title: str) -> list[str]:
    """Problemas de configuración que impedirían a los creadores publicar o al bot moderar."""
    issues: list[str] = []
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError as exc:
        return [exc.message]
    defaults = chat.permissions
    if defaults is not None:
        missing = []
        if not defaults.can_send_messages:
            missing.append(config.ta("perm_messages"))
        if not (defaults.can_send_photos and defaults.can_send_videos):
            missing.append(config.ta("perm_media"))
        if not defaults.can_add_web_page_previews:
            missing.append(config.ta("perm_links"))
        if missing:
            issues.append(config.ta("chat_defaults_block", title=title, missing=", ".join(missing)))
    if me.status != ChatMemberStatus.CREATOR and not getattr(me, "can_delete_messages", False):
        issues.append(config.ta("chat_missing_delete", title=title))
    return issues
