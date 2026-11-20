"""Módulo 2 · Suscripciones y productos: vincular grupos y canales (el bot entra como admin con los permisos
necesarios), varias tarifas/roles sobre el mismo chat (BETA HOUSE: creador + espectador), planes de 1/3/6/12
meses y de por vida con precio en Stars y precio externo, rol de cada producto en el grupo, sincronización de
permisos, verificación y ajustes de acceso (modo, validez del enlace, recordatorio, permisos del espectador)."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel import editor
from handlers.panel.common import NOT_COMMAND, ProductEdit, admin_router, audit, back_kb, grid, replace, short, yes_no
from services import catalog, roles

router = admin_router("panel_products")
log = logging.getLogger(__name__)
_ROLE_CYCLE = {"creator": "viewer", "viewer": "none", "none": "creator"}
_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _sync_and_report(bot: Bot, admin_id: int, chat_id: int | None) -> None:
    """Sincroniza permisos en segundo plano y avisa al admin al terminar (puede tardar en grupos grandes)."""
    try:
        counts = await (roles.sync_chat(bot, chat_id) if chat_id else roles.sync_all(bot))
        await bot.send_message(admin_id, config.ta("roles_synced", **counts))
    except Exception:
        log.exception("Error al sincronizar permisos (chat %s)", chat_id)


def _plans_summary(product_key: str) -> str:
    active = config.plans(product_key)
    if not active:
        return config.ta("product_no_plans")
    return ", ".join(config.plan_label_admin(p.months) for p in active)


def _status(product: config.Product) -> str:
    return config.ta("product_status_on" if product.is_active and product.chat_id else "product_status_off")


def _mode_label() -> str:
    return config.ta(f"access_mode_{config.access_mode()}")


async def products_view() -> tuple[str, InlineKeyboardMarkup]:
    products = config.all_products()
    items = "\n".join(
        config.ta("product_item", status=_status(p).split(" ")[0], name=html.escape(p.name_es), key=p.key,
                  plans=_plans_summary(p.key))
        for p in products
    ) or config.ta("products_empty")
    b = InlineKeyboardBuilder()
    for product in products:
        b.button(text=f"{_status(product).split(' ')[0]} {short(product.name_es, 40)}", callback_data=f"pp:o:{product.key}")
    b.button(text=config.ta("btn_link_group"), callback_data="pp:addg")
    b.button(text=config.ta("btn_link_channel"), callback_data="pp:addc")
    b.button(text=config.ta("btn_detected_chats"), callback_data="pp:det")
    b.button(text=config.ta("btn_sub_settings"), callback_data="pp:settings")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(*([1] * len(products)), 2, 1, 1, 1)
    text = config.ta(
        "panel_products_title", items=items, mode=_mode_label(), ttl=config.ttl_label(), reminder=config.reminder_days()
    )
    return text, b.as_markup()


async def show(cb: CallbackQuery) -> None:
    await replace(cb, *await products_view())


async def product_view(key: str) -> tuple[str, InlineKeyboardMarkup] | None:
    product = config.get_product(key)
    if not product:
        return None
    lines = []
    for months in config.PLAN_MONTHS:
        plan = config.get_plan(key, months)
        lines.append(config.ta(
            "plan_line",
            status="🟢" if plan and plan.is_active else "🔴",
            plan=config.plan_label_admin(months),
            stars=f"{plan.stars_price} XTR" if plan and plan.stars_price else config.ta("plan_off"),
            ext=html.escape(plan.ext_price) if plan and plan.ext_price else config.ta("plan_off"),
        ))
    kind = config.ta("chat_kind_channel" if product.chat_type == "channel" else "chat_kind_group")
    text = config.ta(
        "product_detail", name_es=html.escape(product.name_es), name_en=html.escape(product.name_en), key=key,
        chat_id=product.chat_id or "—", kind=kind, status=_status(product), invite=yes_no(product.can_invite),
        restrict=yes_no(product.can_restrict), members=await db.count_active_members(key), plans="\n".join(lines),
        role=config.ta(f"role_{product.member_role}" if product.member_role in config.MEMBER_ROLES else "role_none"),
    )
    b = InlineKeyboardBuilder()
    for months in config.PLAN_MONTHS:
        plan = config.get_plan(key, months)
        b.button(
            text=config.ta("btn_plan", status="🟢" if plan and plan.is_active else "🔴", plan=config.plan_label_admin(months)),
            callback_data=f"pp:pl:{key}:{months}",
        )
    b.button(text=config.ta("btn_product_toggle_off" if product.is_active else "btn_product_toggle_on"),
             callback_data=f"pp:tog:{key}")
    b.button(text=config.ta("btn_product_check"), callback_data=f"pp:chk:{key}")
    b.button(text=config.ta("btn_product_rename_es"), callback_data=f"pp:ne:{key}")
    b.button(text=config.ta("btn_product_rename_en"), callback_data=f"pp:nn:{key}")
    b.button(text=config.ta("btn_product_role"), callback_data=f"pp:role:{key}")
    b.button(text=config.ta("btn_sync_roles"), callback_data=f"pp:sync:{key}")
    b.button(text=config.ta("btn_product_unlink"), callback_data=f"pp:un:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data="pp:list")
    b.adjust(3, 2, 2, 2, 2, 1, 1)
    return text, b.as_markup()


def plan_view(key: str, months: int) -> tuple[str, InlineKeyboardMarkup]:
    plan = config.get_plan(key, months)
    active = bool(plan and plan.is_active)
    text = config.ta(
        "plan_detail", product=html.escape(config.product_name_admin(key)), plan=config.plan_label_admin(months),
        status=config.ta("pb_state_on" if active else "pb_state_off"),
        stars=f"{plan.stars_price} XTR" if plan and plan.stars_price else config.ta("plan_off"),
        ext=html.escape(plan.ext_price) if plan and plan.ext_price else config.ta("plan_off"),
    )
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_plan_toggle_off" if active else "btn_plan_toggle_on"), callback_data=f"pp:pt:{key}:{months}")
    b.button(text=config.ta("btn_plan_stars"), callback_data=f"pp:ps:{key}:{months}")
    b.button(text=config.ta("btn_plan_ext"), callback_data=f"pp:pe:{key}:{months}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"pp:o:{key}")
    b.adjust(1, 2, 1)
    return text, b.as_markup()


def settings_view() -> tuple[str, InlineKeyboardMarkup]:
    text = config.ta(
        "sub_settings_title", mode=_mode_label(), ttl=config.ttl_label(), reminder=config.reminder_days(),
        viewer_mode=config.ta(f"viewer_mode_{config.viewer_mode()}"),
    )
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_access_mode"), callback_data="pp:mode")
    b.button(text=config.ta("btn_viewer_mode"), callback_data="pp:vmode")
    b.button(text=config.SETTINGS["invite_ttl_minutes"].label, callback_data="pp:set:invite_ttl_minutes")
    b.button(text=config.SETTINGS["reminder_days"].label, callback_data="pp:set:reminder_days")
    b.button(text=config.ta("btn_reminder_text"), callback_data="panel:text:membership_reminder")
    b.button(text=config.ta("btn_expired_text"), callback_data="panel:text:membership_expired")
    b.button(text=config.ta("btn_access_text"), callback_data="panel:text:access_granted")
    b.button(text=config.ta("btn_panel_back"), callback_data="pp:list")
    b.adjust(1)
    return text, b.as_markup()


async def _show_product(cb: CallbackQuery, key: str) -> None:
    view = await product_view(key)
    if view:
        await replace(cb, *view)
    else:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)


# ===========================================================================
# Handlers
# ===========================================================================
@router.callback_query(F.data == "pp:list")
async def list_products(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show(cb)


@router.callback_query(F.data.in_({"pp:addg", "pp:addc"}))
async def add_chat_hint(cb: CallbackQuery, bot: Bot) -> None:
    kind_key = "channel" if cb.data == "pp:addc" else "group"
    kind = config.ta("chat_kind_channel" if kind_key == "channel" else "chat_kind_group")
    me = await bot.get_me()
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_open_add_link", kind=kind), url=catalog.add_bot_url(me.username, kind_key))
    b.button(text=config.ta("btn_detected_chats"), callback_data="pp:det")
    b.button(text=config.ta("btn_panel_back"), callback_data="pp:list")
    b.adjust(1)
    await cb.answer()
    await replace(cb, config.ta("link_add_hint", kind=kind), b.as_markup())


@router.callback_query(F.data == "pp:det")
async def detected_chats(cb: CallbackQuery) -> None:
    chats = await db.list_admin_chats()
    lines, b = [], InlineKeyboardBuilder()
    count = 0
    for chat in chats:
        products = config.products_by_chat(chat["chat_id"])
        linked = config.ta("detected_linked", key=", ".join(p.key for p in products)) if products else ""
        lines.append(config.ta(
            "detected_item", title=html.escape(chat["title"] or ""), chat_id=chat["chat_id"],
            invite=yes_no(chat["can_invite"]), restrict=yes_no(chat["can_restrict"]), linked=linked,
        ))
        title = short(chat["title"] or str(chat["chat_id"]), 32)
        if not products:
            b.button(text=f"➕ {title}", callback_data=f"pp:link:{chat['chat_id']}")
        else:  # otra tarifa/rol sobre el mismo chat
            b.button(text=config.ta("btn_link_more", title=title), callback_data=f"pp:link2:{chat['chat_id']}")
        count += 1
    b.button(text=config.ta("btn_panel_back"), callback_data="pp:list")
    grid(b, count, columns=1)
    await cb.answer()
    await replace(cb, config.ta("detected_title", items="\n".join(lines) or config.ta("detected_empty")), b.as_markup())


@router.callback_query(F.data.startswith("pp:link:") | F.data.startswith("pp:link2:"))
async def link_chat(cb: CallbackQuery, bot: Bot) -> None:
    raw = cb.data.split(":", 2)[2]
    if not raw.lstrip("-").isdigit():
        await cb.answer()
        return
    try:
        key, existed = await catalog.link_chat(bot, int(raw), extra=cb.data.startswith("pp:link2:"))
    except TelegramAPIError as exc:
        await cb.answer(html.escape(exc.message)[:190], show_alert=True)
        return
    product = config.get_product(key)
    if existed:
        await cb.answer(config.ta("chat_already_linked", key=key).replace("<code>", "").replace("</code>", ""), show_alert=True)
    else:
        await cb.answer()
        await audit(bot, f"Producto {key}", "—", product.name_es if product else key, cb.from_user.full_name)
        await cb.message.answer(config.ta("chat_linked", title=html.escape(product.name_es if product else key), key=key))
    await _show_product(cb, key)


@router.callback_query(F.data == "pp:ignore")
async def ignore_chat(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "pp:settings")
async def sub_settings(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, *settings_view())


@router.callback_query(F.data.startswith("pp:set:"))
async def edit_sub_setting(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in ("invite_ttl_minutes", "reminder_days"):
        await cb.answer()
        return
    await cb.answer()
    await editor.open_setting(cb, state, key, back="pp:settings")


@router.callback_query(F.data == "pp:mode")
async def toggle_access_mode(cb: CallbackQuery, bot: Bot) -> None:
    old = config.access_mode()
    new = "link" if old == "request" else "request"
    await db.set_config("access_mode", new)
    config.apply_override("access_mode", new)
    await audit(bot, config.SETTINGS["access_mode"].label, old, new, cb.from_user.full_name)
    await cb.answer(config.ta(f"access_mode_{new}"))
    await replace(cb, *settings_view())


@router.callback_query(F.data == "pp:vmode")
async def toggle_viewer_mode(cb: CallbackQuery, bot: Bot) -> None:
    old = config.viewer_mode()
    new = "text" if old == "readonly" else "readonly"
    await db.set_config("viewer_mode", new)
    config.apply_override("viewer_mode", new)
    await audit(bot, config.SETTINGS["viewer_mode"].label, old, new, cb.from_user.full_name)
    await cb.answer(config.ta(f"viewer_mode_{new}"))
    await replace(cb, *settings_view())
    _background(_sync_and_report(bot, cb.from_user.id, None))


@router.callback_query(F.data.regexp(r"^pp:(o|tog|chk|ne|nn|un|unok|pl|pt|ps|pe|role|sync):"))
async def product_actions(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = cb.data.split(":")
    op, key = parts[1], parts[2]
    months = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    product = config.get_product(key)
    if not product or (op in ("pl", "pt", "ps", "pe") and months not in config.PLAN_MONTHS):
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    admin = cb.from_user.full_name

    if op == "o":
        await cb.answer()
        await _show_product(cb, key)
    elif op == "tog":
        await db.update_product(key, is_active=0 if product.is_active else 1)
        await catalog.reload_products()
        await audit(bot, f"Producto {key} · ventas", str(product.is_active), str(not product.is_active), admin)
        await cb.answer()
        await _show_product(cb, key)
    elif op == "chk":
        ok, info = await catalog.check_product(bot, key)
        message = config.ta("product_check_ok", title=html.escape(info)) if ok else config.ta(
            "product_check_fail", title=html.escape(product.name_es), error=info)
        await cb.answer(message.replace("<b>", "").replace("</b>", "")[:200], show_alert=True)
        await _show_product(cb, key)
    elif op in ("ne", "nn"):
        field = "name_es" if op == "ne" else "name_en"
        await state.set_state(ProductEdit.waiting)
        await state.update_data(key=key, field=field)
        await cb.answer()
        await replace(cb, config.ta("product_prompt_name", lang="ES" if op == "ne" else "EN"), back_kb(f"pp:o:{key}"))
    elif op == "un":
        b = InlineKeyboardBuilder()
        b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"pp:unok:{key}")
        b.button(text=config.ta("btn_panel_back"), callback_data=f"pp:o:{key}")
        b.adjust(2)
        await cb.answer()
        await replace(cb, config.ta("product_unlink_ask", name=html.escape(product.name_es)), b.as_markup())
    elif op == "unok":
        await db.delete_product(key)
        await db.disable_buttons_targeting(("subscribe", "receipt"), key)
        await catalog.reload_products()
        await audit(bot, f"Producto {key}", product.name_es, "—", admin)
        await cb.answer(config.ta("product_unlinked"), show_alert=True)
        await show(cb)
    elif op == "role":
        new_role = _ROLE_CYCLE.get(product.member_role, "creator")
        await db.update_product(key, member_role=new_role)
        await catalog.reload_products()
        await audit(bot, f"Producto {key} · rol", product.member_role, new_role, admin)
        await cb.answer(config.ta(f"role_{new_role}")[:190])
        await _show_product(cb, key)
        if product.chat_id and new_role != "none":
            _background(_sync_and_report(bot, cb.from_user.id, product.chat_id))
    elif op == "sync":
        if not product.chat_id or not roles.is_role_chat(product.chat_id):
            await cb.answer(config.ta("role_none"), show_alert=True)
            return
        await cb.answer(config.ta("btn_sync_roles"))
        _background(_sync_and_report(bot, cb.from_user.id, product.chat_id))
    elif op == "pl":
        await cb.answer()
        await replace(cb, *plan_view(key, months))
    elif op == "pt":
        plan = config.get_plan(key, months)
        new_state = 0 if plan and plan.is_active else 1
        await db.update_plan(key, months, is_active=new_state)
        await catalog.reload_products()
        await audit(bot, f"Plan {key}/{months} · estado", str(int(bool(plan and plan.is_active))), str(new_state), admin)
        await cb.answer()
        await replace(cb, *plan_view(key, months))
    elif op in ("ps", "pe"):
        field = "stars_price" if op == "ps" else "ext_price"
        await state.set_state(ProductEdit.waiting)
        await state.update_data(key=key, months=months, field=field)
        await cb.answer()
        prompt = config.ta("plan_prompt_stars", max=config.STARS_PRICE_MAX) if op == "ps" else config.ta("plan_prompt_ext")
        await replace(cb, prompt, back_kb(f"pp:pl:{key}:{months}"))


@router.message(ProductEdit.waiting, NOT_COMMAND)
async def receive_product_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    key, field, months = data.get("key"), data.get("field"), data.get("months")
    product = config.get_product(key or "")
    if not product or not message.text:
        await message.answer(config.ta("panel_need_text"))
        return
    raw = message.text.strip()
    admin = message.from_user.full_name
    try:
        if field in ("name_es", "name_en"):
            if not 1 <= len(raw) <= 64 or set("<>&{}") & set(raw):
                raise config.SettingError(config.ta("err_pb_name"))
            await db.update_product(key, **{field: raw})
            await audit(bot, f"Producto {key} · {field}", getattr(product, field), raw, admin)
        elif field == "stars_price":
            if not raw.isdigit() or int(raw) > config.STARS_PRICE_MAX:
                raise config.SettingError(config.ta("err_int", min=0, max=config.STARS_PRICE_MAX))
            if int(raw) > 0:
                await editor.check_stars_amount(bot, int(raw))
            old = config.get_plan(key, months)
            await db.update_plan(key, months, stars_price=int(raw))
            await audit(bot, f"Plan {key}/{months} · Stars", str(old.stars_price if old else 0), raw, admin)
        elif field == "ext_price":
            value = "" if raw == "0" else config.check_price_text(raw)
            old = config.get_plan(key, months)
            await db.update_plan(key, months, ext_price=value)
            await audit(bot, f"Plan {key}/{months} · precio externo", old.ext_price if old else "", value or "—", admin)
        else:
            raise config.SettingError(config.ta("panel_session_expired"))
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    await catalog.reload_products()
    await state.clear()
    await message.answer(config.ta("product_saved"))
    view = plan_view(key, months) if months is not None else await product_view(key)
    if view:
        await message.answer(view[0], reply_markup=view[1])
