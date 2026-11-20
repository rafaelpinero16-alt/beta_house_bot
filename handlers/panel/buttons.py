"""Módulo 4 · Botones: todos los botones de cada sección (nombre ES/EN, acción, destino, ON/OFF, orden, fila)
y los nombres de los botones del sistema, del grupo de gestión y del propio panel."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, ButtonEdit, admin_router, audit, back_kb, grid, replace, short
from handlers.panel.editor import check_stars_amount
from handlers.panel.texts import menus_kb
from services import catalog

router = admin_router("panel_buttons")
_NAME_MAX = 64


async def show(cb: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_buttons_menus"), callback_data="pb:menus")
    b.button(text=config.ta("btn_buttons_system"), callback_data="panel:tc:buttons")
    b.button(text=config.ta("btn_buttons_team"), callback_data="panel:tc:buttons_team")
    b.button(text=config.ta("btn_buttons_panel"), callback_data="panel:tc:buttons_panel")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    b.adjust(1)
    await replace(cb, config.ta("panel_buttons_title"), b.as_markup())


# ===========================================================================
# Presentación
# ===========================================================================
def _default_button(menu_id: str, button_id: str) -> tuple | None:
    return next((b for b in config.DEFAULT_BUTTONS.get(menu_id, []) if b[0] == button_id), None)


def _action(row: dict) -> str:
    return config.LEGACY_ACTIONS.get(row["action_type"], row["action_type"])


def target_label(row: dict) -> str:
    action, target = _action(row), row["target_value"] or ""
    if action == "menu":
        menu = config.get_menu(target)
        return html.escape(menu.title) if menu else f"<code>{html.escape(target)}</code>"
    if action == "url":
        if target.startswith("link:"):
            key = target[5:]
            return f"🔗 {html.escape(config.link_label(key))} (<code>{html.escape(config.link(key) or '—')}</code>)"
        return f"<code>{html.escape(target or '—')}</code>"
    if action in ("subscribe", "receipt"):
        if not target:
            return config.ta("pb_target_any")
        return html.escape(config.product_name_admin(target))
    if action == "tip":
        return f"{html.escape(target)} ⭐"
    if action == "alert":
        return html.escape(short(target, 150)) or "—"
    return config.ta("pb_target_none")


async def menu_buttons_view(menu_id: str) -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.get_buttons(menu_id)
    b = InlineKeyboardBuilder()
    for row in rows:
        dot = "🟢" if row["is_active"] else "🔴"
        name = config.render_button_name(row["name_es"], amount=row["target_value"] if row["action_type"] == "tip" else "")
        b.button(text=f"{dot} {short(name, 40)}", callback_data=f"pb:b:{menu_id}:{row['button_id']}")
    extra = 1
    if len(rows) < config.MAX_BUTTONS_PER_MENU:
        b.button(text=config.ta("btn_pb_add"), callback_data=f"pb:add:{menu_id}")
        extra = 2
    b.button(text=config.ta("btn_panel_back"), callback_data="pb:menus")
    b.adjust(*([1] * (len(rows) + extra)))
    menu = config.get_menu(menu_id)
    text = config.ta(
        "pb_menu_title", menu=html.escape(menu.title if menu else menu_id), count=len(rows),
        max=config.MAX_BUTTONS_PER_MENU,
    )
    return text, b.as_markup()


async def button_detail(menu_id: str, button_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    row = await db.get_button(menu_id, button_id)
    if not row:
        return None
    base = f"{menu_id}:{button_id}"
    action = _action(row)
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_pb_toggle_off" if row["is_active"] else "btn_pb_toggle_on"), callback_data=f"pb:t:{base}")
    b.button(text=config.ta("btn_pb_up"), callback_data=f"pb:up:{base}")
    b.button(text=config.ta("btn_pb_name_es"), callback_data=f"pb:ne:{base}")
    b.button(text=config.ta("btn_pb_name_en"), callback_data=f"pb:nn:{base}")
    b.button(text=config.ta("btn_pb_change_action"), callback_data=f"pb:ac:{base}")
    if action in config.ACTIONS_WITH_TARGET:
        b.button(text=config.ta("btn_pb_target"), callback_data=f"pb:tg:{base}")
    b.button(text=config.ta("btn_pb_row_split" if row["same_row"] else "btn_pb_row_join"), callback_data=f"pb:row:{base}")
    if _default_button(menu_id, button_id) is None:
        b.button(text=config.ta("btn_pb_delete"), callback_data=f"pb:del:{base}")
    else:
        b.button(text=config.ta("btn_pb_restore"), callback_data=f"pb:rs:{base}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"pb:m:{menu_id}")
    b.adjust(2, 2, 2 if action in config.ACTIONS_WITH_TARGET else 1, 2 if action in config.ACTIONS_WITH_TARGET else 1, 1)
    text = config.ta(
        "pb_detail", menu=menu_id, button_id=button_id,
        name_es=html.escape(row["name_es"]), name_en=html.escape(row["name_en"]),
        state=config.ta("pb_state_on" if row["is_active"] else "pb_state_off"),
        row=config.ta("pb_row_joined" if row["same_row"] else "pb_row_own"),
        action=config.ta(f"action_{action}"), target=target_label(row),
    )
    return text, b.as_markup()


async def _show_button(cb: CallbackQuery, menu_id: str, button_id: str) -> None:
    detail = await button_detail(menu_id, button_id)
    if detail:
        await replace(cb, *detail)
    else:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)


def _actions_kb(callback_prefix: str, back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for action in config.BUTTON_ACTIONS:
        b.button(text=config.ta(f"action_{action}"), callback_data=f"{callback_prefix}:{action}")
    b.button(text=config.ta("btn_panel_back"), callback_data=back)
    b.adjust(1)
    return b.as_markup()


def _target_picker(row: dict) -> tuple[str, InlineKeyboardMarkup] | None:
    """Selector de destino por listas (sección, enlace, producto). None = el destino se escribe."""
    menu_id, button_id, action = row["menu_id"], row["button_id"], _action(row)
    base = f"{menu_id}:{button_id}"
    b = InlineKeyboardBuilder()
    if action == "menu":
        options = [m for m in config.all_menus() if m.menu_id != menu_id]
        for menu in options:
            b.button(text=short(menu.title, 40), callback_data=f"pb:tm:{base}:{menu.menu_id}")
        prompt, count = config.ta("pb_choose_menu"), len(options)
    elif action == "url":
        keys = config.all_link_keys()
        for key in keys:
            b.button(text=short(config.link_label(key), 30), callback_data=f"pb:tl:{base}:{key}")
        b.button(text=config.ta("btn_pb_type_url"), callback_data=f"pb:tw:{base}")
        prompt, count = config.ta("pb_choose_link"), len(keys) + 1
    elif action in ("subscribe", "receipt"):
        products = [p for p in config.all_products() if p.chat_id]
        for product in products:
            b.button(text=short(product.name_es, 40), callback_data=f"pb:tp:{base}:{product.key}")
        count = len(products)
        if action == "receipt":
            b.button(text=config.ta("pb_any_product"), callback_data=f"pb:tp:{base}:*")
            count += 1
        prompt = config.ta("pb_choose_product")
    else:
        return None
    b.button(text=config.ta("btn_panel_back"), callback_data=f"pb:b:{base}")
    grid(b, count, columns=1 if action != "url" else 2)
    return prompt, b.as_markup()


async def _ask_target(cb: CallbackQuery, state: FSMContext, row: dict) -> None:
    picker = _target_picker(row)
    if picker:
        await replace(cb, *picker)
        return
    action = _action(row)
    prompt = (
        config.ta("pb_prompt_alert", max=config.ALERT_LIMIT) if action == "alert"
        else config.ta("pb_prompt_tip", max=config.STARS_PRICE_MAX)
    )
    await state.set_state(ButtonEdit.waiting)
    await state.update_data(menu_id=row["menu_id"], button_id=row["button_id"], field="target_value")
    await replace(cb, prompt, back_kb(f"pb:b:{row['menu_id']}:{row['button_id']}"))


async def _set_target(bot: Bot, admin: str, row: dict, value: str) -> None:
    await db.update_button(row["menu_id"], row["button_id"], target_value=value)
    if _action(row) == "menu":  # una sección creada por el admin «cuelga» de la sección que la abre
        target = config.get_menu(value)
        if target and not target.is_system and target.parent == "main" and row["menu_id"] != value:
            await db.update_menu(value, parent=row["menu_id"])
            await catalog.reload_menus()
    await audit(bot, f"Botón {row['menu_id']}/{row['button_id']} · destino", row["target_value"] or "", value, admin)


# ===========================================================================
# Handlers
# ===========================================================================
@router.callback_query(F.data == "pb:menus")
async def list_sections(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, config.ta("pb_menus_title", max=config.MAX_BUTTONS_PER_MENU),
                  menus_kb(prefix="pb:m", back="panel:group:buttons", with_new=False))


@router.callback_query(F.data.startswith("pb:"))
async def buttons_router(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = cb.data.split(":")
    op = parts[1]
    menu_id = parts[2] if len(parts) > 2 else ""
    if config.get_menu(menu_id) is None:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    admin = cb.from_user.full_name

    if op == "m":
        await cb.answer()
        await replace(cb, *await menu_buttons_view(menu_id))
        return
    if op == "add":
        if len(await db.get_buttons(menu_id)) >= config.MAX_BUTTONS_PER_MENU:
            await cb.answer(config.ta("pb_full", max=config.MAX_BUTTONS_PER_MENU), show_alert=True)
            return
        await cb.answer()
        menu = config.get_menu(menu_id)
        await replace(cb, config.ta("pb_add_choose", menu=html.escape(menu.title)),
                      _actions_kb(f"pb:na:{menu_id}", f"pb:m:{menu_id}"))
        return
    if op == "na":
        action = parts[3] if len(parts) > 3 else ""
        if action not in config.BUTTON_ACTIONS or len(await db.get_buttons(menu_id)) >= config.MAX_BUTTONS_PER_MENU:
            await cb.answer(config.ta("panel_session_expired"), show_alert=True)
            return
        names = config.NEW_BUTTON_NAMES
        if action in ("back", "home", "lang", "support"):
            names = (config.ta(f"action_{action}"), config.ta(f"action_{action}"))
        new_id = await db.add_button(menu_id, config.CUSTOM_BUTTON_PREFIX, *names, action, "")
        await audit(bot, f"Botón nuevo {menu_id}/{new_id}", "—", action, admin)
        await cb.answer(config.ta("pb_added"), show_alert=True)
        row = await db.get_button(menu_id, new_id)
        if action in config.ACTIONS_WITH_TARGET:
            await _ask_target(cb, state, row)
        else:
            await _show_button(cb, menu_id, new_id)
        return

    button_id = parts[3] if len(parts) > 3 else ""
    row = await db.get_button(menu_id, button_id)
    if not row:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    label = f"Botón {menu_id}/{button_id}"
    extra = parts[4] if len(parts) > 4 else ""

    if op == "b":
        await cb.answer()
        await _show_button(cb, menu_id, button_id)
    elif op == "t":
        new_state = 0 if row["is_active"] else 1
        await db.update_button(menu_id, button_id, is_active=new_state)
        await cb.answer(config.ta("pb_state_on" if new_state else "pb_state_off"))
        await audit(bot, f"{label} · estado", str(row["is_active"]), str(new_state), admin)
        await _show_button(cb, menu_id, button_id)
    elif op == "up":
        await db.move_button_up(menu_id, button_id)
        await cb.answer()
        await _show_button(cb, menu_id, button_id)
    elif op == "row":
        await db.update_button(menu_id, button_id, same_row=0 if row["same_row"] else 1)
        await cb.answer()
        await _show_button(cb, menu_id, button_id)
    elif op == "del" and _default_button(menu_id, button_id) is None:
        await db.delete_button(menu_id, button_id)
        await cb.answer(config.ta("pb_deleted"))
        await audit(bot, label, row["name_es"], "—", admin)
        await replace(cb, *await menu_buttons_view(menu_id))
    elif op == "rs" and (default := _default_button(menu_id, button_id)):
        _, name_es, name_en, action, target, active = default
        await db.update_button(
            menu_id, button_id, name_es=name_es, name_en=name_en, action_type=action, target_value=target,
            is_active=active, same_row=int((menu_id, button_id) in config.DEFAULT_PAIRED_BUTTONS),
        )
        await cb.answer(config.ta("pb_restored"))
        await audit(bot, label, row["name_es"], name_es, admin)
        await _show_button(cb, menu_id, button_id)
    elif op in ("ne", "nn"):
        field = "name_es" if op == "ne" else "name_en"
        await state.set_state(ButtonEdit.waiting)
        await state.update_data(menu_id=menu_id, button_id=button_id, field=field)
        await cb.answer()
        await replace(cb, config.ta("pb_prompt_name", lang="ES" if op == "ne" else "EN"), back_kb(f"pb:b:{menu_id}:{button_id}"))
    elif op == "ac":
        await cb.answer()
        await replace(cb, config.ta("pb_choose_action"), _actions_kb(f"pb:sa:{menu_id}:{button_id}", f"pb:b:{menu_id}:{button_id}"))
    elif op == "sa" and extra in config.BUTTON_ACTIONS:
        await db.update_button(menu_id, button_id, action_type=extra, target_value="")
        await audit(bot, f"{label} · acción", row["action_type"], extra, admin)
        await cb.answer()
        new_row = await db.get_button(menu_id, button_id)
        if extra in config.ACTIONS_WITH_TARGET:
            await _ask_target(cb, state, new_row)
        else:
            await _show_button(cb, menu_id, button_id)
    elif op == "tg":
        await cb.answer()
        await _ask_target(cb, state, row)
    elif op == "tm" and config.get_menu(extra):
        await _set_target(bot, admin, row, extra)
        await cb.answer(config.ta("pb_saved"))
        await _show_button(cb, menu_id, button_id)
    elif op == "tl" and extra in config.all_link_keys():
        await _set_target(bot, admin, row, f"link:{extra}")
        await cb.answer(config.ta("pb_saved"))
        await _show_button(cb, menu_id, button_id)
    elif op == "tp" and (extra == "*" or config.get_product(extra)):
        await _set_target(bot, admin, row, "" if extra == "*" else extra)
        await cb.answer(config.ta("pb_saved"))
        await _show_button(cb, menu_id, button_id)
    elif op == "tw":
        await state.set_state(ButtonEdit.waiting)
        await state.update_data(menu_id=menu_id, button_id=button_id, field="target_value")
        await cb.answer()
        await replace(cb, config.ta("pb_prompt_url"), back_kb(f"pb:b:{menu_id}:{button_id}"))
    else:
        await cb.answer()


def validate_button_value(row: dict, field: str, value: str) -> str:
    """Valida el nombre o el destino escrito de un botón. Lanza SettingError."""
    value = (value or "").strip()
    if field in ("name_es", "name_en"):
        if not 1 <= len(value) <= _NAME_MAX or set("<>&") & set(value):
            raise config.SettingError(config.ta("err_pb_name"))
        config.check_template(value, config.BUTTON_NAME_PLACEHOLDERS)
        return value
    action = _action(row)
    if action == "url":
        return config.check_url(value)
    if action == "alert":
        parts = value.split(" || ")
        if len(parts) > 2 or not all(1 <= len(p.strip()) <= config.ALERT_LIMIT for p in parts):
            raise config.SettingError(config.ta("err_pb_alert", max=config.ALERT_LIMIT))
        return value
    if action == "tip":
        if not value.isdigit() or not 1 <= int(value) <= config.STARS_PRICE_MAX:
            raise config.SettingError(config.ta("err_pb_tip", max=config.STARS_PRICE_MAX))
        return str(int(value))
    raise config.SettingError(config.ta("panel_session_expired"))


@router.message(ButtonEdit.waiting, NOT_COMMAND)
async def receive_button_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    menu_id, button_id, field = data.get("menu_id"), data.get("button_id"), data.get("field")
    row = await db.get_button(menu_id, button_id) if menu_id and button_id else None
    if not row:
        await state.clear()
        await message.answer(config.ta("panel_session_expired"))
        return
    if not message.text:
        await message.answer(config.ta("panel_need_text"))
        return
    try:
        value = validate_button_value(row, field, message.text)
        if field == "target_value" and _action(row) == "tip":
            await check_stars_amount(bot, int(value))
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    if field == "target_value":
        await _set_target(bot, message.from_user.full_name, row, value)
    else:
        await db.update_button(menu_id, button_id, **{field: value})
        await audit(bot, f"Botón {menu_id}/{button_id} · {field}", row[field] or "", value, message.from_user.full_name)
    await state.clear()
    await message.answer(config.ta("pb_saved"))
    detail = await button_detail(menu_id, button_id)
    if detail:
        await message.answer(detail[0], reply_markup=detail[1])
