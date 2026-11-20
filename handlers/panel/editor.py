"""Editor genérico de ajustes (textos ES/EN, textos del equipo, enlaces del catálogo y ajustes numéricos):
categorías → elegir idioma → enviar valor → vista previa real → Guardar / Restaurar."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LabeledPrice, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import (
    NOT_COMMAND,
    PREVIEW_MAX,
    Sample,
    SettingEdit,
    admin_router,
    audit,
    flag,
    grid,
    refresh_setting_cache,
    replace,
    short,
    template_source,
)
from services.telegram_service import send_long

router = admin_router("panel_editor")
BUTTON_CATEGORIES = ("buttons", "buttons_team", "buttons_panel")


# ===========================================================================
# Listados: categorías de textos y de enlaces
# ===========================================================================
def text_categories_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    cats = [c for c in config.TEXT_CATEGORIES if c not in BUTTON_CATEGORIES]
    for cat in cats:
        b.button(text=config.TEXT_CATEGORIES[cat], callback_data=f"panel:tc:{cat}")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:texts")
    grid(b, len(cats), columns=1)
    return b.as_markup()


def _category_back(category: str) -> str:
    return "panel:group:buttons" if category in BUTTON_CATEGORIES else "panel:tcs"


def text_category_kb(category: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    count = 0
    for ckey, (cat, label) in config.TEXT_CATALOG.items():
        if cat != category:
            continue
        if ckey.startswith(config.ADMIN_TEXT_PREFIX):
            skey = f"adm_{ckey[len(config.ADMIN_TEXT_PREFIX):]}"
            b.button(text=f"{flag(skey)} {short(label, 50)}", callback_data=f"panel:edit:{skey}")
        else:
            custom = any(config.setting_is_custom(f"{ckey}_{lang}") for lang in config.LANGS)
            mark = config.ta("flag_custom" if custom else "flag_default")
            b.button(text=f"{mark} {label}", callback_data=f"panel:text:{ckey}")
        count += 1
    b.button(text=config.ta("btn_panel_back"), callback_data=_category_back(category))
    grid(b, count, columns=1 if category not in BUTTON_CATEGORIES else 2)
    return b.as_markup()


def link_categories_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cat, label in config.LINK_CATEGORIES.items():
        b.button(text=label, callback_data=f"panel:lc:{cat}")
    b.button(text=config.ta("btn_custom_links"), callback_data="lk:list")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    grid(b, len(config.LINK_CATEGORIES) + 1, columns=1)
    return b.as_markup()


@router.callback_query(F.data == "panel:tcs")
async def show_text_categories(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await replace(cb, config.ta("panel_texts_title"), text_categories_kb())


@router.callback_query(F.data.startswith("panel:tc:"))
async def show_text_category(cb: CallbackQuery, state: FSMContext) -> None:
    category = cb.data.split(":", 2)[2]
    if category not in config.TEXT_CATEGORIES:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await cb.answer()
    title = config.ta("panel_cat_title", group=config.ta("panel_group_texts"), category=config.TEXT_CATEGORIES[category])
    await replace(cb, title, text_category_kb(category))


@router.callback_query(F.data.startswith("panel:lc:"))
async def show_link_category(cb: CallbackQuery, state: FSMContext) -> None:
    category = cb.data.split(":", 2)[2]
    if category not in config.LINK_CATEGORIES:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await cb.answer()
    b = InlineKeyboardBuilder()
    keys = [k for k, s in config.SETTINGS.items() if s.group == "links" and s.category == category]
    for key in keys:
        b.button(text=f"{flag(key)} {config.SETTINGS[key].label}", callback_data=f"panel:edit:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:links")
    grid(b, len(keys), columns=1)
    title = config.ta("panel_cat_title", group=config.ta("panel_group_links"), category=config.LINK_CATEGORIES[category])
    await replace(cb, title, b.as_markup())


@router.callback_query(F.data.startswith("panel:text:"))
async def text_lang_picker(cb: CallbackQuery, state: FSMContext) -> None:
    text_key = cb.data.split(":", 2)[2]
    if text_key not in config.TEXT_CATALOG:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    await cb.answer()
    category, label = config.TEXT_CATALOG[text_key]
    b = InlineKeyboardBuilder()
    for lang in config.LANGS:
        key = f"{text_key}_{lang}"
        b.button(text=f"{flag(key)} {config.LANG_FLAGS.get(lang, lang)}", callback_data=f"panel:edit:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"panel:tc:{category}")
    b.adjust(len(config.LANGS), 1)
    await replace(cb, config.ta("panel_text_lang_title", label=html.escape(label)), b.as_markup())


# ===========================================================================
# Edición de un ajuste
# ===========================================================================
def default_back(key: str) -> str:
    setting = config.SETTINGS[key]
    if setting.group == "texts":
        if key.startswith("adm_"):
            return f"panel:tc:{setting.category}"
        return f"panel:text:{key.rsplit('_', 1)[0]}"
    if setting.group == "links":
        return f"panel:lc:{setting.category}"
    if setting.group == "subs":
        return "pp:settings"
    if setting.group == "support":
        return "su:home"
    return "panel:home"


def edit_kb(key: str, back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if config.setting_is_custom(key):
        b.button(text=config.ta("btn_panel_reset"), callback_data=f"panel:reset:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data=back)
    b.adjust(1)
    return b.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_panel_save"), callback_data="panel:save")
    b.button(text=config.ta("btn_panel_cancel"), callback_data="panel:cancel")
    b.adjust(2)
    return b.as_markup()


def edit_prompt(key: str) -> str:
    setting = config.SETTINGS[key]
    value = config.setting_value(key)
    current = short(html.escape(value), PREVIEW_MAX) or "—"
    hint = config.ta("panel_format_html" if setting.kind == "template" else "panel_format_plain")
    placeholders = ""
    if setting.placeholders:
        items = ", ".join(f"<code>{{{p}}}</code>" for p in sorted(setting.placeholders))
        placeholders = config.ta("panel_placeholders", items=items)
    updated = config.setting_updated_at(key)
    return config.ta(
        "panel_edit_prompt",
        label=html.escape(setting.label), help=html.escape(setting.help), placeholders=placeholders, flag=flag(key),
        updated_at=config.ta("panel_updated_at", timestamp=updated) if updated else config.ta("panel_never"),
        current=current, format_hint=hint,
    )


async def open_setting(cb: CallbackQuery, state: FSMContext, key: str, back: str | None = None) -> None:
    """Abre el editor de un ajuste. `back` = callback al que vuelve el botón Volver."""
    back = back or default_back(key)
    await state.clear()
    await state.set_state(SettingEdit.waiting_value)
    await state.update_data(key=key, back=back)
    await replace(cb, edit_prompt(key), edit_kb(key, back))


@router.callback_query(F.data.startswith("panel:edit:"))
async def panel_edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in config.SETTINGS:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await cb.answer()
    await open_setting(cb, state, key)


async def check_stars_amount(bot: Bot, amount: int) -> None:
    """Pregunta a Telegram si acepta el monto en Stars (lanza SettingError)."""
    try:
        await bot.create_invoice_link(
            title=config.SUBSCRIPTION_TITLE, description=config.SUBSCRIPTION_TITLE, payload="panel_price_check",
            currency="XTR", prices=[LabeledPrice(label=config.SUBSCRIPTION_TITLE, amount=amount)],
        )
    except TelegramBadRequest as exc:
        raise config.SettingError(config.ta("err_stars_rejected", error=html.escape(exc.message))) from None


async def preview_template(message: Message, bot: Bot, label: str, value: str) -> None:
    """Muestra el texto tal como lo verán. Lanza SettingError si Telegram rechaza el HTML."""
    user = message.from_user
    sample = Sample(
        mention=f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>', id=user.id,
        name=html.escape(user.first_name or ""), amount=100,
    )
    for name in ("mention", "id", "name"):
        sample[name.upper()] = sample[name]
    rendered = value.format_map(Sample({**config._dynamic_vars(), **sample}))
    await message.answer(config.ta("panel_preview_header", label=html.escape(label)))
    try:
        await send_long(bot, message.chat.id, rendered)
    except TelegramBadRequest as exc:
        raise config.SettingError(config.ta("err_html", error=html.escape(exc.message))) from None


@router.message(SettingEdit.waiting_value, NOT_COMMAND)
async def receive_value(message: Message, state: FSMContext, bot: Bot) -> None:
    key = (await state.get_data()).get("key")
    if key not in config.SETTINGS:
        await state.clear()
        await message.answer(config.ta("panel_session_expired"))
        return
    if not message.text:
        await message.answer(config.ta("panel_need_text"))
        return
    setting = config.SETTINGS[key]
    raw = template_source(message) if setting.kind == "template" else message.text
    try:
        value = config.normalize_setting(key, raw)
        if setting.kind == "template":
            await preview_template(message, bot, setting.label, value)
            await message.answer(config.ta("panel_preview_confirm"), reply_markup=confirm_kb())
        else:
            await message.answer(
                config.ta("panel_preview_value", label=html.escape(setting.label), value=html.escape(value)),
                reply_markup=confirm_kb(),
            )
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    except TelegramAPIError as exc:
        await message.answer(config.ta("panel_invalid", error=html.escape(exc.message)))
        return
    await state.update_data(value=value)
    await state.set_state(SettingEdit.confirming)


@router.message(SettingEdit.confirming, NOT_COMMAND)
async def value_while_confirming(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.set_state(SettingEdit.waiting_value)
    await receive_value(message, state, bot)


@router.callback_query(F.data == "panel:save")
async def save_value(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    key, value, back = data.get("key"), data.get("value"), data.get("back")
    if await state.get_state() != SettingEdit.confirming.state or key not in config.SETTINGS or value is None:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    label = config.SETTINGS[key].label
    old = config.setting_value(key)
    await db.set_config(key, value)
    await refresh_setting_cache(key)
    await state.set_state(SettingEdit.waiting_value)
    await state.update_data(value=None)
    await cb.answer()
    await cb.message.edit_text(config.ta("panel_saved", label=html.escape(label)), reply_markup=None)
    await audit(bot, label, old, value, cb.from_user.full_name)
    await cb.message.answer(edit_prompt(key), reply_markup=edit_kb(key, back or default_back(key)))


@router.callback_query(F.data == "panel:cancel")
async def cancel_value(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(config.ta("panel_cancelled"), reply_markup=None)
    key = data.get("key")
    if key in config.SETTINGS:
        await cb.message.answer(edit_prompt(key), reply_markup=edit_kb(key, data.get("back") or default_back(key)))


@router.callback_query(F.data.startswith("panel:reset:"))
async def reset_ask(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in config.SETTINGS or not config.setting_is_custom(key):
        await cb.answer(config.ta("panel_reset_none"), show_alert=True)
        return
    await cb.answer()
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"panel:reset_ok:{key}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"panel:edit:{key}")
    b.adjust(2)
    await replace(cb, config.ta("panel_reset_ask", label=html.escape(config.SETTINGS[key].label)), b.as_markup())


@router.callback_query(F.data.startswith("panel:reset_ok:"))
async def reset_do(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    key = cb.data.split(":", 2)[2]
    if key not in config.SETTINGS:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    label = config.SETTINGS[key].label
    old = config.setting_value(key)
    await db.delete_config(key)
    config.drop_override(key)
    await cb.answer(config.ta("panel_reset_done", label=label)[:200].replace("<b>", "").replace("</b>", ""))
    await audit(bot, label, old, config.setting_value(key), cb.from_user.full_name)
    back = (await state.get_data()).get("back")
    await open_setting(cb, state, key, back)
