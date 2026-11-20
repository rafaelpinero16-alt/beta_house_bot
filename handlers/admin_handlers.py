"""Equipo en el grupo de gestión y eventos de los chats vinculados:
aprobación de comprobantes (producto + plan), /aprobar, /revocar, /cerrar, /ban, /unban, respuestas desde los
Temas, finalizar conversaciones, solicitudes de ingreso, entradas a los chats y altas/bajas del bot como admin.
El /panel vive en handlers/panel/.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from services import catalog, roles
from services.access import (
    deliver_access,
    handle_join,
    handle_join_request,
    reissue,
    revoke_user_access_link,
    sync_member_role,
)
from services.moderation import ban_member, block_banned_join, is_banned, unban_member
from services.telegram_service import (
    SUPPORTED_CONTENT_TYPES,
    Contact,
    close_topic,
    is_admin_message,
    is_team_admin,
    log_sale,
    notify_topic,
    relay_to_user,
)

log = logging.getLogger(__name__)
router = Router(name="admin")
_IN_ADMIN_GROUP = F.message.chat.id == config.ADMIN_GROUP_ID


def _grid(builder: InlineKeyboardBuilder, items: int, columns: int = 2, tail: int = 1) -> None:
    rows = [columns] * (items // columns) + ([items % columns] if items % columns else [])
    builder.adjust(*rows, *([1] * tail))


# ===========================================================================
# Revisión de comprobantes: producto + plan
# ===========================================================================
def receipt_review_kb(payment: dict) -> InlineKeyboardMarkup:
    """Si el miembro eligió producto y plan: aprobación en un clic. Si no: elegir producto y luego plan."""
    pid = payment["id"]
    product_key, months = payment.get("product"), payment.get("plan_months")
    product = config.get_product(product_key) if product_key else None
    if product and product.chat_id and months is not None:
        b = InlineKeyboardBuilder()
        b.button(
            text=config.ta("btn_approve_plan", product=product.name_es, plan=config.plan_label_admin(months)),
            callback_data=f"pay:ok:{pid}:{product_key}:{months}",
        )
        b.button(text=config.ta("btn_change_plan"), callback_data=f"pay:prod:{pid}")
        b.button(text=config.ta("btn_reject"), callback_data=f"pay:reject:{pid}")
        b.adjust(1, 2)
        return b.as_markup()
    return _product_picker_kb(pid)


def _product_picker_kb(pid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    products = [p for p in config.all_products() if p.chat_id]
    for product in products:
        b.button(text=config.ta("btn_pick_product", product=product.name_es), callback_data=f"pay:plan:{pid}:{product.key}")
    b.button(text=config.ta("btn_reject"), callback_data=f"pay:reject:{pid}")
    _grid(b, len(products))
    return b.as_markup()


def _plan_picker_kb(pid: int, product_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for months in config.PLAN_MONTHS:
        plan = config.get_plan(product_key, months)
        label = config.plan_label_admin(months) + (f" · {plan.ext_price}" if plan and plan.ext_price else "")
        b.button(text=config.ta("btn_pick_plan", plan=label), callback_data=f"pay:ok:{pid}:{product_key}:{months}")
    b.button(text=config.ta("btn_back_products"), callback_data=f"pay:prod:{pid}")
    _grid(b, len(config.PLAN_MONTHS))
    return b.as_markup()


async def _finish_alert(cb: CallbackQuery, suffix: str) -> None:
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{suffix}", reply_markup=None)
    except TelegramBadRequest:
        await cb.message.edit_reply_markup(reply_markup=None)


async def _load_pending(cb: CallbackQuery, bot: Bot, pid_text: str) -> dict | None:
    if not await is_team_admin(bot, cb.from_user.id):
        await cb.answer(config.ta("only_admins"), show_alert=True)
        return None
    payment = await db.get_payment(int(pid_text)) if pid_text.isdigit() else None
    if not payment:
        await cb.answer(config.ta("payment_not_found"), show_alert=True)
        return None
    if payment["status"] != "pending":
        await cb.answer(config.ta("payment_already_done"), show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return None
    return payment


@router.callback_query(F.data.startswith("pay:prod:"), _IN_ADMIN_GROUP)
async def review_pick_product(cb: CallbackQuery, bot: Bot) -> None:
    payment = await _load_pending(cb, bot, cb.data.split(":")[2])
    if payment:
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=_product_picker_kb(payment["id"]))


@router.callback_query(F.data.startswith("pay:plan:"), _IN_ADMIN_GROUP)
async def review_pick_plan(cb: CallbackQuery, bot: Bot) -> None:
    _, _, pid, product_key = cb.data.split(":", 3)
    payment = await _load_pending(cb, bot, pid)
    if not payment:
        return
    product = config.get_product(product_key)
    if not product or not product.chat_id:
        await cb.answer(config.ta("product_unavailable"), show_alert=True)
        return
    await cb.answer(config.ta("pick_plan_toast", product=product.name_es))
    await cb.message.edit_reply_markup(reply_markup=_plan_picker_kb(payment["id"], product_key))


async def _approve(cb: CallbackQuery, bot: Bot, pid: str, product_key: str, months: int) -> None:
    payment = await _load_pending(cb, bot, pid)
    if not payment:
        return
    product = config.get_product(product_key)
    if not product or not product.chat_id or months not in config.PLAN_MONTHS:
        await cb.answer(config.ta("product_unavailable"), show_alert=True)
        return
    payment_id = payment["id"]
    if not await db.transition_payment(payment_id, "pending", "approved", cb.from_user.id):
        await cb.answer(config.ta("payment_already_done"), show_alert=True)
        return
    plan = config.get_plan(product_key, months)
    await db.set_payment_plan(payment_id, product_key, months, (plan.ext_price if plan and plan.ext_price else None))
    try:
        await deliver_access(bot, payment["user_id"], payment_id, product_key, months)
    except TelegramForbiddenError:
        await db.transition_payment(payment_id, "approved", "pending")
        await cb.answer(config.ta("user_blocked_access"), show_alert=True)
        return
    except Exception as exc:
        log.exception("Falló la entrega de acceso para el pago %s", payment_id)
        await db.transition_payment(payment_id, "approved", "pending")
        await cb.answer(config.ta("link_error", error=exc)[:190], show_alert=True)
        return

    admin = html.escape(cb.from_user.full_name)
    product_name, plan_label = html.escape(product.name_es), config.plan_label_admin(months)
    await cb.answer(config.ta("approved_toast"))
    await _finish_alert(cb, config.ta("approved_suffix", admin=admin, product=product_name, plan=plan_label))
    contact = await Contact.from_db(payment["user_id"])
    amount = (await db.get_payment(payment_id))["amount"]
    await log_sale(
        bot,
        config.SALES_LOG_CHANNEL_ID,
        config.ta(
            "sale_log", tag=product_key.upper(), method=(payment["method"] or "manual").upper(), product=product_name,
            mention=contact.mention, user_id=contact.id, plan=plan_label, amount=html.escape(amount or "—"),
            status=config.ta("sale_status_approved", admin=admin),
        ),
    )


@router.callback_query(F.data.startswith("pay:ok:"), _IN_ADMIN_GROUP)
async def review_approve(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    if len(parts) != 5 or not parts[4].isdigit():
        await cb.answer(config.ta("payment_not_found"), show_alert=True)
        return
    await _approve(cb, bot, parts[2], parts[3], int(parts[4]))


@router.callback_query(F.data.startswith("pay:approve:") | F.data.startswith("pp:approve:"), _IN_ADMIN_GROUP)
async def review_approve_legacy(cb: CallbackQuery, bot: Bot) -> None:
    """Alertas de versiones anteriores: elegir producto y plan."""
    payment = await _load_pending(cb, bot, cb.data.rsplit(":", 1)[1])
    if payment:
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=_product_picker_kb(payment["id"]))


@router.callback_query(F.data.startswith("pay:reject:") | F.data.startswith("pp:reject:"), _IN_ADMIN_GROUP)
async def review_reject(cb: CallbackQuery, bot: Bot) -> None:
    payment = await _load_pending(cb, bot, cb.data.rsplit(":", 1)[1])
    if not payment:
        return
    if not await db.transition_payment(payment["id"], "pending", "rejected", cb.from_user.id):
        await cb.answer(config.ta("payment_already_done"), show_alert=True)
        return
    try:
        lang = await db.get_lang(payment["user_id"])
        await bot.send_message(payment["user_id"], config.t(lang, "receipt_rejected"))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await cb.answer(config.ta("rejected_toast"))
    await _finish_alert(cb, config.ta("rejected_suffix", admin=html.escape(cb.from_user.full_name)))


@router.callback_query(F.data.startswith("rg:"), _IN_ADMIN_GROUP)
async def reissue_link(cb: CallbackQuery, bot: Bot) -> None:
    if not await is_team_admin(bot, cb.from_user.id):
        await cb.answer(config.ta("only_admins"), show_alert=True)
        return
    raw = cb.data.split(":", 1)[1]
    try:
        await reissue(bot, int(raw))
    except Exception as exc:
        await cb.answer(config.ta("link_error", error=exc)[:190], show_alert=True)
        return
    await cb.answer(config.ta("reissued"))
    await cb.message.edit_reply_markup(reply_markup=None)


# ===========================================================================
# Chat de soporte: finalizar conversación
# ===========================================================================
async def end_support(bot: Bot, user_id: int, admin_name: str) -> bool:
    if not await db.set_support_open(user_id, False):
        return False
    lang = await db.get_lang(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=config.t(lang, "btn_open_support"), callback_data="sp:open")
    ]])
    try:
        await bot.send_message(user_id, config.t(lang, "support_ended"), reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    contact = await Contact.from_db(user_id)
    await notify_topic(bot, contact, config.ta("support_end_done", admin=html.escape(admin_name)))
    await close_topic(bot, user_id)
    return True


@router.callback_query(F.data.startswith("sup:end:"), _IN_ADMIN_GROUP)
async def support_end_button(cb: CallbackQuery, bot: Bot) -> None:
    if not await is_team_admin(bot, cb.from_user.id):
        await cb.answer(config.ta("only_admins"), show_alert=True)
        return
    raw = cb.data.rsplit(":", 1)[1]
    if not raw.isdigit():
        await cb.answer()
        return
    done = await end_support(bot, int(raw), cb.from_user.full_name)
    await cb.answer(None if done else config.ta("support_end_already"), show_alert=not done)


# ===========================================================================
# Comandos dentro del Tema de un miembro
# ===========================================================================
async def _topic_user(message: Message, bot: Bot) -> int | None:
    if not message.is_topic_message or not message.message_thread_id:
        await message.reply(config.ta("cmd_only_in_topic"))
        return None
    if not await is_admin_message(bot, message):
        await message.reply(config.ta("cmd_only_admins"))
        return None
    user_id = await db.get_user_by_topic(message.message_thread_id)
    if not user_id:
        await message.reply(config.ta("topic_unlinked"))
    return user_id


def _admin_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _admin_name(message: Message) -> str:
    return message.from_user.full_name if message.from_user else "admin"


# Alias en español/inglés para /aprobar
PRODUCT_ALIASES = {
    "creador": "creator", "creadores": "creator", "creator": "creator", "c": "creator",
    "espectador": "viewer", "espectadores": "viewer", "viewer": "viewer", "v": "viewer",
}


def _default_product() -> str:
    return next((k for k, *_ in config.PRODUCT_SEEDS if config.get_product(k)), None) or next(
        (p.key for p in config.active_products()), "")


def _approve_usage() -> str:
    return config.ta(
        "cmd_approve_usage",
        products=", ".join(f"<code>{p.key}</code>" for p in config.all_products() if p.chat_id) or "—",
    )


@router.message(Command("aprobar"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_approve(message: Message, bot: Bot, command: CommandObject) -> None:
    """/aprobar [producto] [meses]: aprueba el comprobante pendiente o concede/renueva acceso manual."""
    user_id = await _topic_user(message, bot)
    if not user_id:
        return
    pending = await db.get_pending_payment(user_id)
    args = (command.args or "").split()
    product_key = args[0].lower() if args else ((pending or {}).get("product") or _default_product())
    product_key = PRODUCT_ALIASES.get(product_key, product_key)
    product = config.get_product(product_key)
    if not product or not product.chat_id or len(args) > 2 or (len(args) == 2 and not args[1].isdigit()):
        await message.reply(_approve_usage())
        return
    months = int(args[1]) if len(args) == 2 else (
        (pending or {}).get("plan_months") if (pending or {}).get("plan_months") is not None else 1
    )
    if months not in config.PLAN_MONTHS:
        await message.reply(_approve_usage())
        return

    if pending and await db.transition_payment(pending["id"], "pending", "approved", _admin_id(message)):
        payment_id = pending["id"]
        await db.set_payment_plan(payment_id, product_key, months)
        action = config.ta("cmd_approved_pending", payment_id=payment_id)
    else:
        payment_id = await db.create_payment(
            user_id, "manual", None, status="approved", resolved_by=_admin_id(message),
            product=product_key, plan_months=months,
        )
        action = config.ta("cmd_manual", payment_id=payment_id)
    try:
        await deliver_access(bot, user_id, payment_id, product_key, months)
    except TelegramForbiddenError:
        await message.reply(config.ta("cmd_user_blocked"))
        return
    except Exception as exc:
        log.exception("Falló /aprobar para %s", user_id)
        await message.reply(config.ta("cmd_link_error", error=html.escape(str(exc))))
        return
    await message.reply(
        config.ta("cmd_link_sent", action=action, product=html.escape(product.name_es), plan=config.plan_label_admin(months))
    )


@router.message(Command("rol"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_role(message: Message, bot: Bot) -> None:
    """/rol: recalcula y reaplica los permisos (creador / espectador) del miembro en los grupos con roles."""
    user_id = await _topic_user(message, bot)
    if not user_id:
        return
    results = await roles.apply_user_roles(bot, user_id)
    if not results:
        await message.reply(config.ta("cmd_role_none"))
        return
    contact = await Contact.from_db(user_id)
    lines = []
    for chat_id, role, result in results:
        outcome = (
            config.ta("role_result_ok") if result is None
            else config.ta("role_result_skip") if result == "skip" else html.escape(result)
        )
        lines.append(config.ta(
            "cmd_role_result", mention=contact.mention, chat=html.escape(config.chat_label(chat_id)),
            role=roles.role_tag(role), result=outcome,
        ))
    await message.reply("\n".join(lines))


@router.message(Command("canal"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_channel(message: Message, bot: Bot, command: CommandObject) -> None:
    """/canal -100… autoriza un canal del creador · /canal quitar -100… lo retira · /canal lista sus canales."""
    user_id = await _topic_user(message, bot)
    if not user_id:
        return
    contact = await Contact.from_db(user_id)
    args = (command.args or "").split()
    remove = bool(args) and args[0].lower() in ("quitar", "remove", "borrar", "del")
    if remove:
        args = args[1:]
    if not args:
        channels = await db.list_user_channels(user_id)
        text = (config.ta("channel_list", mention=contact.mention, channels=", ".join(f"<code>{c}</code>" for c in channels))
                if channels else config.ta("channel_list_empty", mention=contact.mention))
        await message.reply(text + "\n\n" + config.ta("cmd_channel_usage"))
        return
    raw = args[0]
    if not raw.lstrip("-").isdigit() or not raw.startswith("-100"):
        await message.reply(config.ta("cmd_channel_usage"))
        return
    channel_id = int(raw)
    if remove:
        await db.remove_allowed_channel(channel_id, user_id)
        await message.reply(config.ta("channel_removed", channel_id=channel_id))
        return
    owner = await db.add_allowed_channel(channel_id, user_id, message.from_user.id)
    if owner:
        await message.reply(config.ta("channel_taken", owner=owner))
        return
    await message.reply(config.ta("channel_added", channel_id=channel_id, mention=contact.mention))


@router.message(Command("revocar"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_revoke(message: Message, bot: Bot) -> None:
    user_id = await _topic_user(message, bot)
    if not user_id:
        return
    count = await revoke_user_access_link(bot, user_id)
    await message.reply(config.ta("cmd_revoked", count=count) if count else config.ta("cmd_nothing_to_revoke"))


@router.message(Command("cerrar"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_close(message: Message, bot: Bot) -> None:
    user_id = await _topic_user(message, bot)
    if user_id and not await end_support(bot, user_id, _admin_name(message)):
        await message.reply(config.ta("support_end_already"))


@router.message(Command("ban"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_ban(message: Message, bot: Bot, command: CommandObject) -> None:
    user_id = await _topic_user(message, bot)
    if not user_id:
        return
    if user_id in config.ADMIN_IDS:
        await message.reply(config.ta("ban_admin_forbidden"))
        return
    result = await ban_member(bot, user_id, (command.args or "").strip() or None, _admin_id(message), _admin_name(message))
    if result is None:
        await message.reply(config.ta("ban_already"))


@router.message(Command("unban"), F.chat.id == config.ADMIN_GROUP_ID)
async def cmd_unban(message: Message, bot: Bot) -> None:
    user_id = await _topic_user(message, bot)
    if user_id and not await unban_member(bot, user_id, _admin_name(message)):
        await message.reply(config.ta("ban_not_banned"))


# ===========================================================================
# Respuestas del equipo en el Tema -> chat privado del miembro
# ===========================================================================
@router.message(F.chat.id == config.ADMIN_GROUP_ID, F.is_topic_message, F.content_type.in_(SUPPORTED_CONTENT_TYPES))
async def topic_to_user(message: Message, bot: Bot) -> None:
    text = message.text or message.caption or ""
    if text.startswith(("#", "/")):
        return
    user_id = await db.get_user_by_topic(message.message_thread_id)
    if not user_id:
        return
    try:
        await relay_to_user(bot, user_id, message)
    except TelegramForbiddenError:
        await message.reply(config.ta("relay_blocked"))
    except TelegramBadRequest as exc:
        await message.reply(config.ta("relay_failed", error=html.escape(str(exc))))


# ===========================================================================
# Chats vinculados: solicitudes de ingreso, entradas y estado del bot
# ===========================================================================
def _is_product_chat(event) -> bool:
    return event.chat.id in config.product_chat_ids()


def _user_link(user) -> str:
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'


@router.chat_join_request(F.func(_is_product_chat))
async def on_join_request(request: ChatJoinRequest, bot: Bot) -> None:
    user = request.from_user
    label = html.escape(config.chat_label(request.chat.id))
    if is_banned(user.id):
        await bot.decline_chat_join_request(chat_id=request.chat.id, user_id=user.id)
        return
    link = request.invite_link.invite_link if request.invite_link else None
    outcome = await handle_join_request(bot, request.chat.id, user.id, link)
    if not outcome:
        return  # enlace ajeno al bot: lo gestionan los admins humanos
    owner = await Contact.from_db(outcome["grant"]["user_id"])
    if outcome["grant"].get("product_key"):
        label = html.escape(config.product_name_admin(outcome["grant"]["product_key"]))
    if outcome["result"] == "approved":
        note = config.ta("vip_joined", mention=owner.mention, product=label, grant_id=outcome["grant"]["id"])
    else:
        note = config.ta(
            "join_request_declined", intruder=_user_link(user), intruder_id=user.id, product=label, owner=owner.mention
        )
    await notify_topic(bot, owner, note)


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION), F.func(_is_product_chat))
async def on_product_join(event: ChatMemberUpdated, bot: Bot) -> None:
    member = event.new_chat_member.user
    label = html.escape(config.chat_label(event.chat.id))
    if is_banned(member.id):
        await block_banned_join(bot, event.chat.id, member.id)
        contact = await Contact.from_db(member.id)
        await notify_topic(bot, contact, config.ta("ban_rejoin_blocked", mention=contact.mention, product=label))
        return
    link = event.invite_link.invite_link if event.invite_link else None
    outcome = await handle_join(bot, event.chat.id, member.id, link)
    roles.forget(event.chat.id, member.id)
    if not outcome:
        # Entrada sin enlace pendiente (solicitud ya aprobada, reingreso o alta manual): solo se aplica su rol.
        if not member.is_bot and roles.is_role_chat(event.chat.id):
            paid = bool(await db.get_active_memberships_in_chat(member.id, event.chat.id))
            await sync_member_role(bot, event.chat.id, member.id, announce=paid)
        return
    grant = outcome["grant"]
    owner = await Contact.from_db(grant["user_id"])
    if grant.get("product_key"):
        label = html.escape(config.product_name_admin(grant["product_key"]))
    if not member.is_bot and roles.is_role_chat(event.chat.id):
        await sync_member_role(bot, event.chat.id, member.id, announce=grant["user_id"] == member.id)
    if grant["user_id"] == member.id:
        note = config.ta("vip_joined", mention=owner.mention, product=label, grant_id=grant["id"])
    else:
        note = config.ta(
            "vip_link_stolen", grant_id=grant["id"], product=label, owner=owner.mention,
            intruder=_user_link(member), intruder_id=member.id,
        )
    await notify_topic(bot, owner, note)


@router.my_chat_member()
async def on_bot_status(event: ChatMemberUpdated, bot: Bot) -> None:
    """El bot fue agregado, promovido, degradado o expulsado de un grupo/canal."""
    if event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        roles.invalidate_admins(event.chat.id)
        await catalog.on_bot_status_change(bot, event.chat, event.new_chat_member)


@router.callback_query(F.data.regexp(r"^(panel|pb|ban|cl|pp|pm|lk|tp|ps|su):"))
async def panel_forbidden(cb: CallbackQuery) -> None:
    """Botones del /panel pulsados por alguien que no está en ADMIN_IDS (o fuera del chat privado)."""
    await cb.answer(config.ta("only_admins"), show_alert=True)
