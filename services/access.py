"""Accesos a productos: enlaces personales con caducidad, aprobación de solicitudes de ingreso, inicio de la
suscripción al entrar, renovaciones, recordatorios y expulsión al vencer.

Roles (BETA HOUSE): al entrar, renovar, cambiar de plan o vencer, el bot aplica los permisos del rol del miembro
en el grupo (services/roles.py): creador publica; espectador solo lectura.

Modos (config.access_mode()):
  request → enlace con solicitud de ingreso: el bot SOLO aprueba al miembro que pagó y rechaza a cualquier otro.
  link    → enlace de 1 solo uso (member_limit=1).
El tiempo del plan empieza a contar cuando el miembro ENTRA (o al pagar, si renueva estando dentro).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from services import roles
from services.telegram_service import Contact, notify_topic, safe_delete

log = logging.getLogger(__name__)
_grant_locks: dict[int, asyncio.Lock] = {}
_IN_CHAT = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR, ChatMemberStatus.RESTRICTED}


def renew_kb(lang: str, product_key: str) -> InlineKeyboardMarkup:
    """Botón de renovación: abre los planes del producto."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=config.t(lang, "btn_renew"), callback_data=f"rn:{product_key}")]]
    )


def _expiry_after(current_expiry: int | None, months: int, now: int | None = None) -> int | None:
    """Nuevo vencimiento sumando `months` (0 = de por vida) sobre el vigente o desde ahora."""
    if months <= 0:
        return None
    now = now or int(time.time())
    base = max(now, current_expiry) if current_expiry else now
    return config.add_months(base, months)


async def _is_in_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in _IN_CHAT


async def _create_invite(bot: Bot, chat_id: int, user_id: int, payment_id: int | None, tag: str) -> tuple[str, int]:
    expire_at = int(time.time()) + config.invite_ttl_seconds()
    params = {"chat_id": chat_id, "name": f"{tag} u{user_id} p{payment_id or 0}"[:32], "expire_date": expire_at}
    if config.access_mode() == "request":
        params["creates_join_request"] = True
    else:
        params["member_limit"] = 1
    link = await bot.create_chat_invite_link(**params)
    return link.invite_link, expire_at


async def _revoke_link(bot: Bot, chat_id: int, invite_link: str) -> None:
    try:
        await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite_link)
    except TelegramAPIError as exc:
        log.debug("Revocación de enlace ignorada (%s): %s", invite_link, exc)


async def close_grant(bot: Bot, grant: dict, status: str) -> bool:
    """Cierra un acceso emitido: borra el mensaje del miembro y anula el enlace. Idempotente."""
    if not await db.close_grant(grant["id"], status):
        return False
    await safe_delete(bot, grant["user_id"], grant["delivery_message_id"])
    await _revoke_link(bot, grant["chat_id"], grant["invite_link"])
    return True


async def sync_member_role(
    bot: Bot, chat_id: int, user_id: int, old_role: str | None = None, announce: bool = False
) -> str | None:
    """Aplica el rol vigente del miembro en el chat. Si cambió respecto a `old_role` (o `announce`), avisa al
    miembro por privado y deja constancia en su Tema. Devuelve el rol aplicado (None si el chat no usa roles)."""
    if not roles.is_role_chat(chat_id):
        return None
    role, result = await roles.apply_role(bot, chat_id, user_id)
    contact = await Contact.from_db(user_id)
    chat = html.escape(config.chat_label(chat_id))
    if result not in (None, "skip"):
        await notify_topic(
            bot, contact, config.ta("role_apply_failed_topic", role=roles.role_tag(role), chat=chat, error=html.escape(result))
        )
        return role
    changed = old_role is not None and old_role != role
    if changed:
        await notify_topic(
            bot, contact, config.ta("role_changed_topic", chat=chat, old=roles.role_tag(old_role), new=roles.role_tag(role))
        )
    elif announce and result is None:
        await notify_topic(bot, contact, config.ta("role_applied_topic", chat=chat, role=roles.role_tag(role)))
    if (changed or announce) and result is None and role in ("creator", "viewer"):
        try:
            await bot.send_message(user_id, config.t(await db.get_lang(user_id), f"role_{role}_on"))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    return role


async def deliver_access(bot: Bot, user_id: int, payment_id: int | None, product_key: str, months: int) -> dict:
    """Entrega lo pagado. Si el miembro ya tiene una suscripción activa la amplía al instante (y solo envía enlace
    si no está dentro del chat); si no, envía su enlace personal y la suscripción empieza cuando entre.

    Si el miembro ya está dentro del chat por otra suscripción (p. ej. espectador que paga el plan de creador), la
    nueva suscripción empieza al instante, sin enlace, y el bot actualiza sus permisos.

    Devuelve {"kind": "link" | "renewed" | "activated", "expires_at", "grant_id"}. Lanza excepción si no se pudo entregar.
    """
    product = config.get_product(product_key)
    if not product or not product.chat_id:
        raise RuntimeError(config.ta("product_unavailable"))
    lock = _grant_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        lang = await db.get_lang(user_id)
        name = config.product_name(lang, product.key)
        membership = await db.get_active_membership(user_id, product.key)
        in_chat = await _is_in_chat(bot, product.chat_id, user_id)
        old_role = await roles.effective_role(user_id, product.chat_id) if in_chat else None

        if membership:  # renovación: se suma al vencimiento vigente
            expires_at = _expiry_after(membership["expires_at"], months) if membership["expires_at"] else None
            await db.save_membership(membership["id"], user_id, product.key, product.chat_id, expires_at, payment_id)
            contact = await Contact.from_db(user_id)
            date = config.format_date(expires_at) if expires_at else config.plan_label(lang, 0)
            await notify_topic(
                bot, contact,
                config.ta("access_renewed_topic", product=html.escape(config.product_name_admin(product.key)), date=date),
            )
            if in_chat:
                await bot.send_message(user_id, config.t(lang, "access_renewed", product=name, date=date))
                await sync_member_role(bot, product.chat_id, user_id, old_role)
                return {"kind": "renewed", "expires_at": expires_at, "grant_id": None}
            pending_start, plan_shown = False, config.plan_text(lang, months, expires_at)
        elif in_chat:  # ya está dentro con otra suscripción del mismo chat: empieza ahora, sin enlace
            expires_at = _expiry_after(None, months)
            await db.save_membership(None, user_id, product.key, product.chat_id, expires_at, payment_id)
            for old in await db.get_active_grants(user_id, product.chat_id):
                await close_grant(bot, old, "revoked")
            await bot.send_message(
                user_id,
                config.t(lang, "access_activated", product=name, plan=config.plan_text(lang, months, expires_at)),
            )
            await sync_member_role(bot, product.chat_id, user_id, old_role)
            log.info("Suscripción %s activada al instante para %s (ya estaba en el chat)", product.key, user_id)
            return {"kind": "activated", "expires_at": expires_at, "grant_id": None}
        else:
            expires_at = None
            pending_start, plan_shown = True, config.plan_label(lang, months)

        for old in await db.get_active_grants(user_id, product.chat_id):  # 1 enlace vigente por producto
            await close_grant(bot, old, "revoked")
        invite_link, link_expire = await _create_invite(bot, product.chat_id, user_id, payment_id, product.key)
        grant_id = await db.create_grant(
            user_id, payment_id, product.chat_id, invite_link, link_expire,
            product_key=product.key, plan_months=months, pending_start=pending_start,
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=config.t(lang, "btn_join", product=name), url=invite_link)]]
        )
        try:
            msg = await bot.send_message(
                chat_id=user_id,
                text=config.t(lang, "access_granted", product=name, plan=plan_shown),
                reply_markup=kb,
                protect_content=True,  # no se puede reenviar ni copiar
            )
        except Exception:
            await db.close_grant(grant_id, "revoked")
            await _revoke_link(bot, product.chat_id, invite_link)
            raise
        await db.set_grant_message(grant_id, msg.message_id)
        log.info("Acceso %s #%s emitido a %s (pago %s, %s meses)", product.key, grant_id, user_id, payment_id, months)
        return {"kind": "link", "expires_at": expires_at, "grant_id": grant_id}


async def _start_membership(grant: dict) -> int | None:
    """Empieza (o amplía) la suscripción cuando el miembro entra. Devuelve el vencimiento (None = de por vida)."""
    if not grant.get("pending_start"):
        return None
    months = grant.get("plan_months") or 0
    fallback = config.product_by_chat(grant["chat_id"])
    product_key = grant.get("product_key") or (fallback.key if fallback else None)
    if not product_key:
        return None
    membership = await db.get_active_membership(grant["user_id"], product_key)
    if membership:
        expires_at = _expiry_after(membership["expires_at"], months) if membership["expires_at"] else None
    else:
        expires_at = _expiry_after(None, months)
    await db.save_membership(
        membership["id"] if membership else None, grant["user_id"], product_key, grant["chat_id"], expires_at,
        grant.get("payment_id"),
    )
    return expires_at


async def handle_join_request(bot: Bot, chat_id: int, user_id: int, invite_link: str | None) -> dict | None:
    """Solicitud de ingreso con un enlace del bot: aprueba solo al dueño del enlace.
    Devuelve {"result": "approved" | "declined", "grant", "expires_at"} o None si el enlace no es del bot."""
    grant = await db.get_grant_by_link(invite_link) if invite_link else None
    if grant is None:
        return None
    if grant["user_id"] != user_id or grant["status"] != "issued":
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError as exc:
            log.debug("No se pudo rechazar la solicitud de %s: %s", user_id, exc)
        return {"result": "declined", "grant": grant, "expires_at": None}
    await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
    await close_grant(bot, grant, "used")
    expires_at = await _start_membership(grant)
    return {"result": "approved", "grant": grant, "expires_at": expires_at}


async def handle_join(bot: Bot, chat_id: int, user_id: int, invite_link: str | None) -> dict | None:
    """Alguien entró (enlace de 1 uso o ingreso directo): autodestruye el mensaje y empieza la suscripción."""
    grant = await db.get_grant_by_link(invite_link) if invite_link else None
    if grant is None or grant["status"] != "issued":
        active = await db.get_active_grants(user_id, chat_id)
        grant = active[0] if active else None
    if grant is None or not await close_grant(bot, grant, "used"):
        return None
    if grant["user_id"] != user_id:
        log.warning("Enlace del miembro %s usado por %s", grant["user_id"], user_id)
        return {"grant": grant, "expires_at": None}
    return {"grant": grant, "expires_at": await _start_membership(grant)}


async def reissue(bot: Bot, grant_id: int) -> dict:
    """Reemite un enlace que caducó sin usarse, con el mismo producto y plan."""
    grant = await db.get_grant(grant_id)
    if not grant or not grant.get("product_key"):
        raise RuntimeError(config.ta("payment_not_found"))
    return await deliver_access(bot, grant["user_id"], grant["payment_id"], grant["product_key"], grant["plan_months"] or 0)


async def revoke_user_access_link(bot: Bot, user_id: int) -> int:
    count = 0
    for grant in await db.get_active_grants(user_id):
        if await close_grant(bot, grant, "revoked"):
            count += 1
    return count


async def _remove_member(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Saca al miembro sin dejarlo vetado (podrá volver si renueva). Devuelve el error o None."""
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        return None
    except TelegramAPIError as exc:
        return exc.message


# ===========================================================================
# Barridos periódicos
# ===========================================================================
async def _sweep_expired_links(bot: Bot) -> None:
    for grant in await db.get_expired_grants():
        if not await close_grant(bot, grant, "expired"):
            continue
        lang = await db.get_lang(grant["user_id"])
        try:
            await bot.send_message(grant["user_id"], config.t(lang, "link_expired"))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        product = config.get_product(grant.get("product_key") or "") or config.product_by_chat(grant["chat_id"])
        contact = await Contact.from_db(grant["user_id"])
        kb = None
        if grant.get("product_key"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=config.ta("btn_reissue"), callback_data=f"rg:{grant['id']}")
            ]])
        await notify_topic(
            bot, contact,
            config.ta("vip_link_expired", grant_id=grant["id"],
                      product=html.escape(config.product_name_admin(product.key)) if product else grant["chat_id"]),
            reply_markup=kb,
        )


async def _sweep_reminders(bot: Bot) -> None:
    days = config.reminder_days()
    now = int(time.time())
    for membership in await db.get_memberships_to_remind(days * 86400, now):
        if not await db.mark_reminded(membership["id"]):
            continue
        user_id, product_key = membership["user_id"], membership["product"]
        lang = await db.get_lang(user_id)
        date = config.format_date(membership["expires_at"])
        days_left = max(1, -(-(membership["expires_at"] - now) // 86400))
        try:
            await bot.send_message(
                user_id,
                config.t(lang, "membership_reminder", product=config.product_name(lang, product_key), date=date,
                         days_left=days_left),
                reply_markup=renew_kb(lang, product_key),
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
        contact = await Contact.from_db(user_id)
        await notify_topic(
            bot, contact,
            config.ta("membership_reminder_topic", product=html.escape(config.product_name_admin(product_key)), date=date),
        )


async def _sweep_expired_memberships(bot: Bot) -> None:
    for membership in await db.get_expired_memberships():
        if not await db.close_membership(membership["id"], "expired"):
            continue
        user_id, chat_id, product_key = membership["user_id"], membership["chat_id"], membership["product"]
        for grant in await db.get_active_grants(user_id, chat_id):
            if not grant.get("product_key") or grant["product_key"] == product_key:
                await close_grant(bot, grant, "revoked")
        lang = await db.get_lang(user_id)
        if await db.get_active_memberships_in_chat(user_id, chat_id):
            # Le queda otra suscripción en el mismo chat (p. ej. espectador): no se expulsa, se ajusta su rol.
            product = config.get_product(product_key)
            remaining = await roles.effective_role(user_id, chat_id)
            expired_role = product.member_role if product else "none"
            old_role = max(expired_role, remaining, key=lambda r: roles.ROLE_RANK.get(r, 0))
            await sync_member_role(bot, chat_id, user_id, old_role=old_role)
            try:
                await bot.send_message(
                    user_id,
                    config.t(lang, "membership_expired", product=config.product_name(lang, product_key)),
                    reply_markup=renew_kb(lang, product_key),
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
            contact = await Contact.from_db(user_id)
            await notify_topic(
                bot, contact,
                config.ta("membership_expired_topic", product=html.escape(config.product_name_admin(product_key)),
                          status=config.ta("kick_skipped_other_plan")),
            )
            continue
        roles.forget(chat_id, user_id)
        error = await _remove_member(bot, chat_id, user_id)
        try:
            await bot.send_message(
                user_id,
                config.t(lang, "membership_expired", product=config.product_name(lang, product_key)),
                reply_markup=renew_kb(lang, product_key),
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        contact = await Contact.from_db(user_id)
        await notify_topic(
            bot, contact,
            config.ta(
                "membership_expired_topic", product=html.escape(config.product_name_admin(product_key)),
                status=config.ta("kick_ok") if error is None else config.ta("kick_failed", error=html.escape(error)),
            ),
        )


async def sweep(bot: Bot) -> None:
    for task in (_sweep_expired_links, _sweep_reminders, _sweep_expired_memberships):
        try:
            await task(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error en el barrido %s", task.__name__)
