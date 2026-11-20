"""Lado del miembro: idioma, bienvenida, secciones dinámicas, planes de suscripción (Stars y pagos externos),
donaciones, comprobantes y chat de soporte.

Este módulo no contiene textos, montos ni enlaces: todo sale de config.py, del /panel y de la base de datos.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
)

import config
import database as db
from handlers.admin_handlers import receipt_review_kb
from services.access import deliver_access
from services.menus import show_menu
from services.telegram_service import (
    SUPPORTED_CONTENT_TYPES,
    Contact,
    announce_tip,
    get_or_create_topic,
    log_sale,
    notify_topic,
    relay_to_topic,
    send_rich,
    support_end_kb,
    with_topic,
)

log = logging.getLogger(__name__)
router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

RECEIPT_TYPES = {ContentType.PHOTO, ContentType.DOCUMENT}
STARS_CURRENCY = "XTR"

# Callbacks de versiones anteriores que los miembros aún pueden tener en el chat → sección actual.
LEGACY_NAV = {
    "menu:home": "main", "tip:back": "main", "menu:hosts": "main", "none:hosts": "main", "tip:menu": "donate",
    "menu:event": "main", "pay:paypal_instructions": "paypal_hub", "nav:home": "main", "nav:confirm_vip": "paypal_hub",
    "nav:confirm": "paypal_hub",
    # Secciones de la versión anterior (Alpha Pig Bros) → secciones de BETA HOUSE
    "nav:vip": "main", "nav:vip_paypal": "paypal_hub", "nav:dir": "main", "nav:stars": "main",
    "nav:methods": "paypal_hub", "nav:platforms": "main",
}
DEFAULT_PRODUCT = "creator"   # producto por defecto en callbacks antiguos sin producto


# ===========================================================================
# Utilidades
# ===========================================================================
def detect_lang(language_code: str | None) -> str:
    code = (language_code or "").split("-", 1)[0].lower()
    return code if code in config.LANGS else config.DEFAULT_LANG


def language_kb() -> InlineKeyboardMarkup:
    lang = config.DEFAULT_LANG
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=config.t(lang, "btn_lang_es"), callback_data="lang:es"),
        InlineKeyboardButton(text=config.t(lang, "btn_lang_en"), callback_data="lang:en"),
    ]])


def welcome_vars(user) -> dict:
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    return {"mention": mention, "id": user.id, "name": html.escape(user.first_name or "")}


def _back(lang: str, target: str) -> list[InlineKeyboardButton]:
    key = "btn_home" if target == "main" else "btn_back"
    return [InlineKeyboardButton(text=config.t(lang, key), callback_data=f"nav:{target}")]


async def _replace(cb: CallbackQuery, bot: Bot, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edita el mensaje del menú; si no se puede (multimedia, mensaje antiguo…) envía uno nuevo."""
    if cb.message.text is not None:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    await send_rich(bot, cb.message.chat.id, text, reply_markup=kb)


async def open_menu(bot: Bot, chat_id: int, user, menu_id: str, cb: CallbackQuery | None = None) -> None:
    menu_id = LEGACY_NAV.get(f"nav:{menu_id}", menu_id)
    if config.get_menu(menu_id) is None:
        menu_id = "main"
    lang = await db.get_lang(user.id)
    if menu_id == "main":
        await db.set_receipt_mode(user.id, None)
    if menu_id == config.SUPPORT_MENU:
        await open_support(bot, user, notify=True)
    extra = welcome_vars(user) if menu_id == "main" else {}
    await show_menu(bot, chat_id, menu_id, lang, cb=cb, **extra)


async def open_support(bot: Bot, user, notify: bool) -> None:
    """Abre el chat de soporte: lo siguiente que escriba el miembro llega a su Tema."""
    opened = await db.set_support_open(user.id, True)
    await db.set_support_ack(user.id, True)
    if notify and opened:
        await notify_topic(bot, Contact.from_user(user), config.ta("support_opened"), reply_markup=support_end_kb(user.id))


# ===========================================================================
# Planes: elegir plan con Stars o para un pago externo
# ===========================================================================
def stars_plans_kb(lang: str, product_key: str, back: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=config.t(lang, "plan_btn_stars", plan=config.plan_label(lang, p.months), price=p.stars_price),
        callback_data=f"pl:s:{product_key}:{p.months}",
    )] for p in config.stars_plans(product_key)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, _back(lang, back)])


def ext_plans_kb(lang: str, product_key: str, back: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=config.t(lang, "plan_btn_ext", plan=config.plan_label(lang, p.months), price=p.ext_price),
        callback_data=f"pl:r:{product_key}:{p.months}",
    )] for p in config.ext_plans(product_key)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, _back(lang, back)])


def receipt_products_kb(lang: str, back: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=config.product_name(lang, p.key), callback_data=f"pr:{p.key}:{back}")]
            for p in config.active_products() if config.ext_plans(p.key)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, _back(lang, back)])


def _sellable(product_key: str) -> bool:
    product = config.get_product(product_key)
    return bool(product and product.is_active and product.chat_id)


async def show_stars_plans(cb: CallbackQuery, bot: Bot, lang: str, product_key: str, back: str) -> None:
    if not _sellable(product_key) or not config.stars_plans(product_key):
        await cb.answer(config.t(lang, "no_plans"), show_alert=True)
        return
    await cb.answer()
    name = config.product_name(lang, product_key)
    await _replace(cb, bot, config.t(lang, "choose_plan", product=name), stars_plans_kb(lang, product_key, back))


async def show_receipt_plans(cb: CallbackQuery, bot: Bot, lang: str, product_key: str, back: str) -> None:
    if not _sellable(product_key) or not config.ext_plans(product_key):
        await cb.answer(config.t(lang, "no_plans"), show_alert=True)
        return
    await cb.answer()
    name = config.product_name(lang, product_key)
    await _replace(cb, bot, config.t(lang, "choose_plan_receipt", product=name), ext_plans_kb(lang, product_key, back))


async def send_sub_invoice(bot: Bot, chat_id: int, lang: str, product_key: str, months: int) -> bool:
    plan = config.get_plan(product_key, months)
    if not _sellable(product_key) or not plan or not plan.is_active or plan.stars_price <= 0:
        return False
    name = config.product_name(lang, product_key)
    await bot.send_invoice(
        chat_id=chat_id,
        title=name[:32],
        description=config.t(lang, "stars_invoice_desc", plan=config.plan_label(lang, months), product=name)[:255],
        payload=config.sub_payload(product_key, months, plan.stars_price),
        currency=STARS_CURRENCY,
        prices=[LabeledPrice(label=config.t(lang, "stars_invoice_label"), amount=plan.stars_price)],
    )
    return True


async def send_tip_invoice(bot: Bot, chat_id: int, lang: str, amount: int) -> bool:
    if not config.TIPS_ENABLED or not await _tip_offered(amount):
        return False
    await bot.send_invoice(
        chat_id=chat_id,
        title=config.t(lang, "tip_invoice_title")[:32],
        description=config.t(lang, "tip_invoice_desc", amount=amount)[:255],
        payload=config.tip_payload(amount),
        currency=STARS_CURRENCY,
        prices=[LabeledPrice(label=config.t(lang, "tip_invoice_label"), amount=amount)],
    )
    return True


async def _tip_offered(amount: int) -> bool:
    """El monto está publicado en algún botón de donación activo (de cualquier sección)."""
    return any(b["target_value"] == str(amount) for b in await db.get_buttons_by_action("tip", active_only=True))


# ===========================================================================
# /start (con enlaces directos) → idioma → bienvenida
# ===========================================================================
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.full_name, user.username, initial_lang=detect_lang(user.language_code))
    await db.set_receipt_mode(user.id, None)
    try:
        await get_or_create_topic(bot, Contact.from_user(user))
    except Exception:
        log.exception("No se pudo crear/recuperar el tema del miembro %s", user.id)

    payload = (command.args or "").strip()
    if payload and config.DEEPLINK_RE.match(payload):
        await handle_deep_link(message, bot, payload)
        return
    await message.answer(config.t(config.DEFAULT_LANG, "choose_lang"), reply_markup=language_kb())


async def handle_deep_link(message: Message, bot: Bot, payload: str) -> None:
    """t.me/<bot>?start=… desde anuncios o publicaciones: abre directamente la sección o el pago."""
    user = message.from_user
    lang = await db.get_lang(user.id)
    chat_id = message.chat.id
    if payload == "sp":
        await open_menu(bot, chat_id, user, config.SUPPORT_MENU)
        return
    kind, value = payload.split("_", 1)
    if kind == "m":
        await open_menu(bot, chat_id, user, value)
    elif kind == "s":
        if _sellable(value) and config.stars_plans(value):
            name = config.product_name(lang, value)
            await message.answer(config.t(lang, "choose_plan", product=name), reply_markup=stars_plans_kb(lang, value, "main"))
        else:
            await message.answer(config.t(lang, "no_plans"))
    elif kind == "r":
        if _sellable(value) and config.ext_plans(value):
            name = config.product_name(lang, value)
            await message.answer(
                config.t(lang, "choose_plan_receipt", product=name), reply_markup=ext_plans_kb(lang, value, "main")
            )
        else:
            await message.answer(config.t(lang, "no_plans"))
    elif kind == "t" and value.isdigit():
        if not await send_tip_invoice(bot, chat_id, lang, int(value)):
            await message.answer(config.t(lang, "tip_invalid"))


@router.callback_query(F.data.startswith("lang:"))
async def set_language(cb: CallbackQuery, bot: Bot) -> None:
    lang = cb.data.split(":", 1)[1]
    if lang not in config.LANGS:
        lang = config.DEFAULT_LANG
    await cb.answer()
    user = cb.from_user
    await db.upsert_user(user.id, user.full_name, user.username, initial_lang=lang)
    await db.set_lang(user.id, lang)
    try:
        await cb.message.edit_text(config.t(lang, "lang_set"), reply_markup=None)
    except TelegramBadRequest:
        pass
    await show_menu(bot, cb.message.chat.id, "main", lang, **welcome_vars(user))


# ===========================================================================
# Navegación y acciones de los botones
# ===========================================================================
@router.callback_query(F.data.startswith("nav:"))
async def navigate(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    await open_menu(bot, cb.message.chat.id, cb.from_user, cb.data.split(":", 1)[1], cb=cb)


@router.callback_query(F.data.in_(set(LEGACY_NAV)))
async def navigate_legacy(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    await open_menu(bot, cb.message.chat.id, cb.from_user, LEGACY_NAV[cb.data], cb=cb)


@router.callback_query(F.data.regexp(r"^(a|btn|buy):"))
async def button_action(cb: CallbackQuery, bot: Bot) -> None:
    """Acciones de los botones configurables (también callbacks btn:/buy: de versiones anteriores)."""
    _, menu_id, button_id = (cb.data.split(":", 2) + ["", ""])[:3]
    row = await db.get_button(menu_id, button_id)
    user = cb.from_user
    lang = await db.get_lang(user.id)
    if not row or not row["is_active"]:
        await cb.answer()
        return
    action = config.LEGACY_ACTIONS.get(row["action_type"], row["action_type"])
    target = row["target_value"] or ""

    if action == "alert":
        await cb.answer(config.split_bilingual(target, lang)[: config.ALERT_LIMIT], show_alert=True)
    elif action == "header":  # divisor: aviso breve (toast), sin cambiar de pantalla
        await cb.answer(config.t(lang, "header_hint")[: config.ALERT_LIMIT], show_alert=False)
    elif action == "url":  # el destino es un ID de pago (p. ej. Binance Pay ID): se muestra en emergente
        value = config.resolve_button_url(target)
        await cb.answer(config.t(lang, "binance_info", value=value)[: config.ALERT_LIMIT], show_alert=True)
    elif action == "subscribe":
        await show_stars_plans(cb, bot, lang, target.split(":", 1)[0] or DEFAULT_PRODUCT, menu_id)
    elif action == "receipt":
        if target:
            await show_receipt_plans(cb, bot, lang, target, menu_id)
        else:
            await cb.answer()
            await db.set_receipt_mode(user.id, None)
            await _replace(cb, bot, config.t(lang, "choose_product_receipt"), receipt_products_kb(lang, menu_id))
    elif action == "tip":
        if target.isdigit() and await send_tip_invoice(bot, cb.message.chat.id, lang, int(target)):
            await cb.answer()
        else:
            await cb.answer(config.t(lang, "tip_invalid" if config.TIPS_ENABLED else "tips_disabled"), show_alert=True)
    elif action == "support":
        await cb.answer()
        await open_menu(bot, cb.message.chat.id, user, config.SUPPORT_MENU, cb=cb)
    elif action == "lang":
        await cb.answer()
        await _replace(cb, bot, config.t(lang, "choose_lang"), language_kb())
    else:
        await cb.answer()


@router.callback_query(F.data == "sp:open")
async def support_reopen(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    await open_menu(bot, cb.message.chat.id, cb.from_user, config.SUPPORT_MENU, cb=cb)


@router.callback_query(F.data.startswith("pl:"))
async def choose_plan(cb: CallbackQuery, bot: Bot) -> None:
    """pl:s:<producto>:<meses> → factura en Stars · pl:r:<producto>:<meses> → enviar comprobante."""
    parts = cb.data.split(":")
    lang = await db.get_lang(cb.from_user.id)
    if len(parts) != 4 or not parts[3].isdigit():
        await cb.answer()
        return
    mode, product_key, months = parts[1], parts[2], int(parts[3])
    plan = config.get_plan(product_key, months)
    if mode == "s":
        if await send_sub_invoice(bot, cb.message.chat.id, lang, product_key, months):
            await cb.answer()
        else:
            await cb.answer(config.t(lang, "no_plans"), show_alert=True)
        return
    if not _sellable(product_key) or not plan or not plan.is_active or not plan.ext_price:
        await cb.answer(config.t(lang, "no_plans"), show_alert=True)
        return
    await cb.answer()
    await db.set_receipt_mode(cb.from_user.id, product_key, months)
    text = config.t(
        lang, "receipt_instructions", product=config.product_name(lang, product_key),
        plan=config.plan_label(lang, months), price=html.escape(plan.ext_price),
    )
    await _replace(cb, bot, text, InlineKeyboardMarkup(inline_keyboard=[_back(lang, "main")]))


@router.callback_query(F.data.startswith("pr:"))
async def receipt_pick_product(cb: CallbackQuery, bot: Bot) -> None:
    _, product_key, back = (cb.data.split(":", 2) + ["main"])[:3]
    lang = await db.get_lang(cb.from_user.id)
    await show_receipt_plans(cb, bot, lang, product_key, back if config.get_menu(back) else "main")


@router.callback_query(F.data.startswith("rn:"))
async def renew(cb: CallbackQuery, bot: Bot) -> None:
    """Botón «Renovar» de los recordatorios: planes con Stars o, si no hay, pago externo."""
    product_key = cb.data.split(":", 1)[1]
    lang = await db.get_lang(cb.from_user.id)
    if config.stars_plans(product_key):
        await show_stars_plans(cb, bot, lang, product_key, "main")
    else:
        await show_receipt_plans(cb, bot, lang, product_key, "main")


# Callbacks de versiones anteriores
@router.callback_query(F.data.in_({"pay:stars", "pay:paypal_confirm", "menu:support"}))
async def legacy_actions(cb: CallbackQuery, bot: Bot) -> None:
    lang = await db.get_lang(cb.from_user.id)
    if cb.data == "pay:stars":
        await cb.answer()
        await open_menu(bot, cb.message.chat.id, cb.from_user, "main", cb=cb)
    elif cb.data == "pay:paypal_confirm":
        await cb.answer()
        await open_menu(bot, cb.message.chat.id, cb.from_user, "paypal_hub", cb=cb)
    else:
        await cb.answer()
        await open_menu(bot, cb.message.chat.id, cb.from_user, config.SUPPORT_MENU, cb=cb)


@router.callback_query(F.data.startswith("tip:pay:"))
async def tip_legacy(cb: CallbackQuery, bot: Bot) -> None:
    lang = await db.get_lang(cb.from_user.id)
    raw = cb.data.rsplit(":", 1)[1]
    if raw.isdigit() and await send_tip_invoice(bot, cb.message.chat.id, lang, int(raw)):
        await cb.answer()
    else:
        await cb.answer(config.t(lang, "tip_invalid"), show_alert=True)


# ===========================================================================
# Pagos con Telegram Stars
# ===========================================================================
@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Valida producto, plan y monto contra la configuración vigente antes de que Telegram cobre."""
    payload, total = query.invoice_payload, query.total_amount
    valid = False
    if query.currency == STARS_CURRENCY:
        if payload.startswith(config.TIP_PAYLOAD_PREFIX):
            amount = payload[len(config.TIP_PAYLOAD_PREFIX):]
            valid = amount.isdigit() and int(amount) == total and await _tip_offered(total)
        elif offer := config.parse_sub_payload(payload):
            product_key, months, price = offer
            plan = config.get_plan(product_key, months)
            valid = bool(
                _sellable(product_key) and plan and plan.is_active and plan.stars_price == total
                and price in (total, -1)
            )
    if valid:
        await query.answer(ok=True)
        return
    log.warning("Pre-checkout rechazado: user=%s payload=%s amount=%s", query.from_user.id, payload, total)
    lang = await db.get_lang(query.from_user.id)
    await query.answer(ok=False, error_message=config.t(lang, "invoice_invalid"))


async def _handle_stars_sub(
    message: Message, bot: Bot, sp: SuccessfulPayment, contact: Contact, product_key: str, months: int
) -> None:
    payment_id = await db.create_payment(
        contact.id, "stars", f"{sp.total_amount} {sp.currency}", status="approved",
        charge_id=sp.telegram_payment_charge_id, product=product_key, plan_months=months,
    )
    delivered = True
    try:
        await deliver_access(bot, contact.id, payment_id, product_key, months)
    except Exception:
        delivered = False
        log.exception("No se pudo entregar acceso %s a %s tras pago con Stars", product_key, contact.id)
        await message.answer(config.t(await db.get_lang(contact.id), "stars_fallback"))

    product, plan = html.escape(config.product_name_admin(product_key)), config.plan_label_admin(months)
    await notify_topic(
        bot,
        contact,
        config.ta(
            "stars_vip_topic", payment_id=payment_id, product=product, plan=plan, amount=sp.total_amount,
            charge_id=html.escape(sp.telegram_payment_charge_id),
            status=config.ta("stars_vip_delivered" if delivered else "stars_vip_failed"),
        ),
    )
    await log_sale(
        bot,
        config.SALES_LOG_CHANNEL_ID,
        config.ta(
            "sale_log", tag=product_key.upper(), method="STARS", product=product, mention=contact.mention,
            user_id=contact.id, plan=plan, amount=f"{sp.total_amount} XTR",
            status=config.ta("sale_status_auto" if delivered else "sale_status_failed"),
        ),
    )


async def _handle_tip(message: Message, bot: Bot, sp: SuccessfulPayment, contact: Contact) -> None:
    tip_id = await db.create_tip(
        contact.id, sp.total_amount, sp.telegram_payment_charge_id, sp.invoice_payload, sp.currency
    )
    if tip_id is None:
        return
    await message.answer(config.t(await db.get_lang(contact.id), "tip_thanks", amount=sp.total_amount))
    await announce_tip(bot, contact, tip_id, sp.total_amount, sp.telegram_payment_charge_id)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    user = message.from_user
    contact = Contact.from_user(user)
    await db.upsert_user(user.id, user.full_name, user.username)
    if await db.charge_id_exists(sp.telegram_payment_charge_id):
        return  # actualización duplicada

    offer = config.parse_sub_payload(sp.invoice_payload)
    if offer:
        await _handle_stars_sub(message, bot, sp, contact, offer[0], offer[1])
    elif sp.invoice_payload.startswith(config.TIP_PAYLOAD_PREFIX):
        await _handle_tip(message, bot, sp, contact)
    else:
        log.error("Pago con payload desconocido: %s (user %s)", sp.invoice_payload, user.id)
        await notify_topic(
            bot, contact,
            config.ta(
                "unknown_payment_topic", payload=html.escape(sp.invoice_payload), amount=sp.total_amount,
                charge_id=html.escape(sp.telegram_payment_charge_id),
            ),
        )


# ===========================================================================
# Comprobantes de pagos externos (PayPal y otros)
# ===========================================================================
async def _process_receipt(message: Message, bot: Bot, product_key: str, months: int | None) -> None:
    user = message.from_user
    contact = Contact.from_user(user)
    lang = await db.get_lang(user.id)
    product_key = product_key if config.get_product(product_key) else None
    plan = config.get_plan(product_key, months) if product_key and months is not None else None

    pending = await db.get_pending_payment(user.id)
    if pending:
        payment_id = pending["id"]
        if product_key and months is not None:
            await db.set_payment_plan(payment_id, product_key, months, plan.ext_price if plan else None)
    else:
        payment_id = await db.create_payment(
            user.id, "external", plan.ext_price if plan else None, product=product_key, plan_months=months,
            receipt_kind=product_key or "*",
        )
    payment = await db.get_payment(payment_id)
    unknown = config.ta("receipt_unknown")
    alert = config.ta(
        "receipt_alert",
        card=contact.card,
        product=html.escape(config.product_name_admin(payment["product"])) if payment["product"] else unknown,
        plan=config.plan_label_admin(payment["plan_months"]) if payment["plan_months"] is not None else unknown,
        price=html.escape(payment["amount"] or "—"),
        payment_id=payment_id,
    )

    async def deliver(topic_id: int) -> None:
        await bot.copy_message(
            chat_id=config.ADMIN_GROUP_ID, from_chat_id=message.chat.id, message_id=message.message_id,
            message_thread_id=topic_id,
        )
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID, message_thread_id=topic_id, text=alert,
            reply_markup=receipt_review_kb(payment),
        )

    await with_topic(bot, contact, deliver)
    await db.set_receipt_mode(user.id, None)
    await show_menu(bot, message.chat.id, "receipt_wait", lang)


# ===========================================================================
# Chat de soporte: lo que escriba el miembro llega a su Tema
# ===========================================================================
def _open_support_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=config.t(lang, "btn_open_support"), callback_data="sp:open")
    ]])


@router.message(F.content_type.in_(SUPPORTED_CONTENT_TYPES))
async def user_message(message: Message, bot: Bot) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.full_name, user.username, initial_lang=detect_lang(user.language_code))
    lang = await db.get_lang(user.id)

    receipt = await db.get_receipt_mode(user.id)
    if receipt and message.content_type in RECEIPT_TYPES:
        try:
            await _process_receipt(message, bot, *receipt)
        except Exception:
            log.exception("No se pudo procesar el comprobante de %s", user.id)
            await message.answer(config.t(lang, "relay_error"))
        return

    if not receipt and config.support_mode() == "button" and not await db.is_support_open(user.id):
        await message.answer(config.t(lang, "support_closed_hint"), reply_markup=_open_support_kb(lang))
        return

    try:
        await relay_to_topic(bot, Contact.from_user(user), message)
    except Exception:
        log.exception("No se pudo reenviar el mensaje de %s al foro", user.id)
        await message.answer(config.t(lang, "relay_error"))
        return
    if receipt:
        await message.answer(config.t(lang, "send_image"))
    elif await db.pop_support_ack(user.id):
        await show_menu(bot, message.chat.id, "support_wait", lang)
