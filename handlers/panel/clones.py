"""Módulo 9 · Clonación autorizada: registrar clones del bot (token validado con Telegram) y descargar su .env.
Cada clon es un despliegue independiente con su propia base de datos y su propio /panel."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, CloneWizard, admin_router, back_kb, grid, log, replace

router = admin_router("panel_clones")


async def show(cb: CallbackQuery) -> None:
    await replace(cb, *await _clones_view())


_CLONE_STEPS: list[tuple[State, str, str]] = [
    (CloneWizard.token, "clone_prompt_token", "bot_token"),
    (CloneWizard.admin_group, "clone_prompt_admin_group", "admin_group_id"),
    (CloneWizard.vip, "clone_prompt_vip", "vip_chat_id"),
    (CloneWizard.sales, "clone_prompt_sales", "sales_log_id"),
    (CloneWizard.tips, "clone_prompt_tips", "tips_log_id"),
    (CloneWizard.admins, "clone_prompt_admins", "admin_ids"),
]


def _mask_token(token: str) -> str:
    return f"{token[:12]}…{token[-4:]}" if len(token) > 20 else "…"


def _clone_status(row: dict) -> str:
    return config.ta("clone_status_active" if row["status"] == "active" else "clone_status_revoked")


async def _clones_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.list_clones()
    items = "\n".join(
        config.ta(
            "clone_item", status=_clone_status(r), username=html.escape(r["bot_username"] or str(r["bot_id"])),
            admin_group=r["admin_group_id"], date=(r["created_at"] or "")[:10],
        )
        for r in rows
    ) or config.ta("clones_empty")
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(
            text=config.ta("btn_clone_open", status=_clone_status(r), username=r["bot_username"] or r["bot_id"]),
            callback_data=f"cl:o:{r['id']}",
        )
    b.button(text=config.ta("btn_clone_add"), callback_data="cl:add")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:home")
    grid(b, len(rows), tail=2)
    return config.ta("clones_title", count=len(rows), items=items), b.as_markup()


def clone_env(row: dict) -> str:
    """Archivo .env de un clon: identidad propia + núcleo con valores de fábrica (se personaliza en su /panel)."""
    tips_enabled = "true" if row["tips_log_id"] else "false"
    return "\n".join([
        f"# Clon autorizado de este bot · @{row['bot_username']} · {row['created_at']}",
        "# Despliégalo como un servicio nuevo con el mismo código y su propio Volume (DB_PATH).",
        f"BOT_TOKEN={row['bot_token']}",
        f"ADMIN_GROUP_ID={row['admin_group_id']}",
        f"BETA_HOUSE_CHAT_ID={row['vip_chat_id']}",
        f"SALES_LOG_CHANNEL_ID={row['sales_log_id']}",
        f"TIPS_LOG_CHANNEL_ID={row['tips_log_id'] or ''}",
        f"TIPS_ENABLED={tips_enabled}",
        f"ADMIN_IDS={row['admin_ids']}",
        f"CREATOR_STARS_PRICE={config.PLAN_SEEDS['creator'][0][1]}",
        f"CREATOR_PAYPAL_PRICE={config.PLAN_SEEDS['creator'][0][2]}",
        f"VIEWER_STARS_PRICE={config.PLAN_SEEDS['viewer'][0][1]}",
        f"VIEWER_PAYPAL_PRICE={config.PLAN_SEEDS['viewer'][0][2]}",
        f"VIEWER_MODE={config.viewer_mode()}",
        f"SUBSCRIPTION_TITLE={config.SUBSCRIPTION_TITLE}",
        f"INVITE_TTL_SECONDS={config.invite_ttl_seconds()}",
        "DB_PATH=/data/bot_data.db",
        "LOG_LEVEL=INFO",
        "",
    ])


async def _clone_detail(clone_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    row = await db.get_clone(clone_id)
    if not row:
        return None
    text = config.ta(
        "clone_detail", username=html.escape(row["bot_username"] or str(row["bot_id"])), status=_clone_status(row),
        token=_mask_token(row["bot_token"]), admin_group=row["admin_group_id"], vip=row["vip_chat_id"],
        sales=row["sales_log_id"], tips=row["tips_log_id"] or "—", admins=html.escape(row["admin_ids"]),
        date=(row["created_at"] or "")[:16],
    )
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_clone_env"), callback_data=f"cl:env:{clone_id}")
    active = row["status"] == "active"
    b.button(text=config.ta("btn_clone_revoke" if active else "btn_clone_activate"), callback_data=f"cl:st:{clone_id}")
    b.button(text=config.ta("btn_clone_delete"), callback_data=f"cl:del:{clone_id}")
    b.button(text=config.ta("btn_panel_back"), callback_data="panel:group:clones")
    b.adjust(1)
    return text, b.as_markup()


async def _send_clone_env(message: Message, row: dict) -> None:
    await message.answer_document(
        BufferedInputFile(clone_env(row).encode(), filename=f"clone_{row['bot_username'] or row['bot_id']}.env"),
        caption=config.ta("clone_env_caption", username=html.escape(row["bot_username"] or str(row["bot_id"]))),
        protect_content=True,
    )


@router.callback_query(F.data.startswith("cl:"))
async def clones_router(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    op = parts[1]
    if op == "add":
        await state.clear()
        await state.set_state(_CLONE_STEPS[0][0])
        await cb.answer()
        await replace(cb, _clone_step_text(0), back_kb("panel:group:clones"))
        return
    if op == "save":
        await _clone_save(cb, state)
        return
    clone_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    row = await db.get_clone(clone_id)
    if not row:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    await state.clear()
    if op == "o":
        await cb.answer()
        await replace(cb, *(await _clone_detail(clone_id)))
    elif op == "env":
        await cb.answer()
        await _send_clone_env(cb.message, row)
    elif op == "st":
        new_status = "revoked" if row["status"] == "active" else "active"
        await db.set_clone_status(clone_id, new_status)
        await cb.answer(config.ta("clone_revoked" if new_status == "revoked" else "clone_activated"), show_alert=True)
        await replace(cb, *(await _clone_detail(clone_id)))
    elif op == "del":
        b = InlineKeyboardBuilder()
        b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"cl:delok:{clone_id}")
        b.button(text=config.ta("btn_panel_back"), callback_data=f"cl:o:{clone_id}")
        b.adjust(2)
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=b.as_markup())
    elif op == "delok":
        await db.delete_clone(clone_id)
        await cb.answer(config.ta("clone_deleted"))
        await show(cb)


def _clone_step_text(index: int) -> str:
    return config.ta(
        "clone_step", step=index + 1, total=len(_CLONE_STEPS), prompt=config.ta(_CLONE_STEPS[index][1])
    )


async def _clone_parse(bot: Bot, field: str, raw: str) -> tuple[object, dict]:
    """Valida un paso del asistente. Devuelve (valor, datos extra) o lanza SettingError."""
    raw = raw.strip()
    if field == "bot_token":
        if not config.is_bot_token(raw):
            raise config.SettingError(config.ta("err_clone_token", error="formato"))
        if raw == config.BOT_TOKEN:
            raise config.SettingError(config.ta("err_clone_token_self"))
        probe = Bot(token=raw)
        try:
            me = await probe.get_me()
        except Exception as exc:  # token rechazado, red, etc.
            raise config.SettingError(config.ta("err_clone_token", error=html.escape(str(exc)[:150]))) from None
        finally:
            await probe.session.close()
        if me.id == bot.id:
            raise config.SettingError(config.ta("err_clone_token_self"))
        if await db.clone_exists(me.id):
            raise config.SettingError(config.ta("err_clone_token_dup"))
        return raw, {"bot_id": me.id, "bot_username": me.username or str(me.id)}
    if field == "tips_log_id" and raw == "0":
        return 0, {}
    if field.endswith("_id") and field != "admin_ids":
        try:
            value = int(raw)
        except ValueError:
            raise config.SettingError(config.ta("err_clone_chat")) from None
        if not config.is_supergroup_or_channel(value):
            raise config.SettingError(config.ta("err_clone_chat"))
        return value, {}
    ids = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not ids or not all(p.isdigit() and int(p) > 0 for p in ids):
        raise config.SettingError(config.ta("err_clone_admins"))
    return ",".join(ids), {}


@router.message(StateFilter(*[step for step, _, _ in _CLONE_STEPS]), NOT_COMMAND)
async def clone_wizard_step(message: Message, state: FSMContext, bot: Bot) -> None:
    current = await state.get_state()
    index = next(i for i, (step, _, _) in enumerate(_CLONE_STEPS) if step.state == current)
    field = _CLONE_STEPS[index][2]
    try:
        value, extra = await _clone_parse(bot, field, message.text or "")
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    if field == "bot_token":
        try:
            await message.delete()  # no dejar el token a la vista en el chat
        except TelegramBadRequest:
            pass
    await state.update_data(**{field: value}, **extra)

    if index + 1 < len(_CLONE_STEPS):
        await state.set_state(_CLONE_STEPS[index + 1][0])
        await message.answer(_clone_step_text(index + 1), reply_markup=back_kb("panel:group:clones"))
        return

    data = await state.get_data()
    await state.set_state(CloneWizard.confirm)
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_clone_save"), callback_data="cl:save")
    b.button(text=config.ta("btn_panel_cancel"), callback_data="panel:group:clones")
    b.adjust(1)
    await message.answer(
        config.ta(
            "clone_summary", username=html.escape(data["bot_username"]), token=_mask_token(data["bot_token"]),
            admin_group=data["admin_group_id"], vip=data["vip_chat_id"], sales=data["sales_log_id"],
            tips=data["tips_log_id"] or "—", admins=data["admin_ids"],
        ),
        reply_markup=b.as_markup(),
    )


async def _clone_save(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if await state.get_state() != CloneWizard.confirm.state or "bot_id" not in data:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    if await db.clone_exists(data["bot_id"]):
        await cb.answer(config.ta("err_clone_token_dup"), show_alert=True)
        return
    clone_id = await db.create_clone(data, cb.from_user.id)
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(
        config.ta("clone_saved", username=html.escape(data["bot_username"])), reply_markup=None
    )
    row = await db.get_clone(clone_id)
    await _send_clone_env(cb.message, row)
    log.info("Clon @%s autorizado por %s", data["bot_username"], cb.from_user.id)
