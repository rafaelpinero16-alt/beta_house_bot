"""Anuncios y publicaciones de donación (usado por los módulos 1 y 6): asistente de 4 pasos
(contenido → botones → destino → cuándo), lista, vista previa, envío inmediato o programado."""
from __future__ import annotations

import html
import time

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import database as db
from handlers.panel.common import NOT_COMMAND, PostWizard, admin_router, audit, back_kb, grid, replace, short, template_source
from services import broadcast
from services.telegram_service import media_from_message

router = admin_router("panel_posts")
KINDS = ("announce", "donation")
_BACK = {"announce": "panel:group:welcome", "donation": "panel:group:donations"}
_TEXT_MAX = 12000


def _target_label(target: str) -> str:
    if target == "users":
        return config.ta("post_target_label_users")
    if target == "chats":
        return config.ta("post_target_label_chats")
    if target.startswith("me:"):
        return config.ta("post_target_label_me")
    chat_id = target.partition(":")[2]
    product = config.product_by_chat(int(chat_id)) if chat_id.lstrip("-").isdigit() else None
    return html.escape(product.name_es) if product else chat_id


def _when_label(post: dict) -> str:
    label = broadcast.format_when(post["send_at"]) if post["status"] in ("scheduled",) else (post["last_sent_at"] or "—")
    return label + (config.ta("post_repeat", hours=post["repeat_hours"]) if post["repeat_hours"] else "")


async def list_view(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    posts = await db.list_posts(kind)
    items = "\n".join(
        config.ta("post_item", id=p["id"], status=config.ta(f"post_status_{p['status']}"),
                  target=_target_label(p["target"]), when=_when_label(p))
        for p in posts
    ) or config.ta("posts_empty")
    b = InlineKeyboardBuilder()
    for post in posts:
        b.button(text=config.ta("btn_post_open", status=config.ta(f"post_status_{post['status']}"), id=post["id"]),
                 callback_data=f"ps:o:{post['id']}")
    b.button(text=config.ta("btn_post_new"), callback_data=f"ps:new:{kind}")
    b.button(text=config.ta("btn_panel_back"), callback_data=_BACK[kind])
    grid(b, len(posts), columns=3, tail=2)
    return config.ta("posts_title", kind=config.ta(f"posts_kind_{kind}"), items=items), b.as_markup()


async def post_view(post_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    post = await db.get_post(post_id)
    if not post:
        return None
    text = config.ta(
        "post_detail", id=post_id, status=config.ta(f"post_status_{post['status']}"), target=_target_label(post["target"]),
        when=_when_label(post), sent=post["sent_count"], failed=post["fail_count"],
    )
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("btn_post_preview"), callback_data=f"ps:prev:{post_id}")
    if post["status"] in ("draft", "scheduled", "sent", "cancelled"):
        b.button(text=config.ta("btn_post_send_now"), callback_data=f"ps:send:{post_id}")
    if post["status"] == "scheduled":
        b.button(text=config.ta("btn_post_cancel"), callback_data=f"ps:cancel:{post_id}")
    b.button(text=config.ta("btn_post_delete"), callback_data=f"ps:del:{post_id}")
    b.button(text=config.ta("btn_panel_back"), callback_data=f"ps:list:{post['kind']}")
    b.adjust(2, 2, 1)
    return text, b.as_markup()


@router.callback_query(F.data.startswith("ps:list:"))
async def list_posts(cb: CallbackQuery, state: FSMContext) -> None:
    kind = cb.data.split(":", 2)[2]
    if kind not in KINDS:
        await cb.answer()
        return
    await state.clear()
    await cb.answer()
    await replace(cb, *await list_view(kind))


@router.callback_query(F.data.startswith("ps:new:"))
async def new_post(cb: CallbackQuery, state: FSMContext) -> None:
    kind = cb.data.split(":", 2)[2]
    if kind not in KINDS:
        await cb.answer()
        return
    await state.clear()
    await state.set_state(PostWizard.content)
    await state.update_data(kind=kind)
    await cb.answer()
    await replace(cb, config.ta("post_step_content"), back_kb(f"ps:list:{kind}"))


@router.message(PostWizard.content, NOT_COMMAND)
async def step_content(message: Message, state: FSMContext) -> None:
    media = media_from_message(message) or ""
    text = template_source(message).strip() if (message.text or message.caption) else ""
    if (not text and not media) or len(text) > _TEXT_MAX:
        await message.answer(config.ta("err_post_content"))
        return
    data = await state.get_data()
    await state.update_data(text=text, media=media)
    await state.set_state(PostWizard.buttons)
    hint = config.ta("post_auto_tips_hint") if data.get("kind") == "donation" else ""
    await message.answer(config.ta("post_step_buttons", auto_hint=hint), reply_markup=back_kb(f"ps:list:{data.get('kind')}"))


@router.message(PostWizard.buttons, NOT_COMMAND)
async def step_buttons(message: Message, state: FSMContext) -> None:
    try:
        buttons = broadcast.encode_buttons(message.text or "")
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    await state.update_data(buttons=buttons)
    await state.set_state(PostWizard.target)
    users = len(await db.broadcast_user_ids())
    b = InlineKeyboardBuilder()
    b.button(text=config.ta("post_target_users", count=users), callback_data="ps:tg:users")
    b.button(text=config.ta("post_target_chats"), callback_data="ps:tg:chats")
    chats = [p for p in config.active_products()][:10]
    for product in chats:
        b.button(text=config.ta("post_target_chat", title=short(product.name_es, 30)), callback_data=f"ps:tg:chat:{product.chat_id}")
    b.button(text=config.ta("post_target_me"), callback_data="ps:tg:me")
    b.adjust(1)
    await message.answer(config.ta("post_step_target"), reply_markup=b.as_markup())


@router.callback_query(PostWizard.target, F.data.startswith("ps:tg:"))
async def step_target(cb: CallbackQuery, state: FSMContext) -> None:
    target = cb.data.split(":", 2)[2]
    if target == "me":
        target = f"me:{cb.from_user.id}"
    await state.update_data(target=target)
    await state.set_state(PostWizard.when)
    await cb.answer()
    await replace(cb, config.ta("post_step_when", tz=config.TIMEZONE), None)


@router.message(PostWizard.when, NOT_COMMAND)
async def step_when(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        send_at, repeat = broadcast.parse_when(message.text or "")
    except config.SettingError as exc:
        await message.answer(config.ta("panel_invalid", error=str(exc)))
        return
    data = await state.get_data()
    status = "scheduled" if send_at else "sending"
    post_id = await db.create_post(
        data["kind"], data.get("text", ""), data.get("media", ""), data.get("buttons", "[]"), data["target"],
        send_at or int(time.time()), repeat, status, message.from_user.id,
    )
    await state.clear()
    await audit(bot, f"Publicación #{post_id}", "—", f"{data['target']} · {broadcast.format_when(send_at)}",
                message.from_user.full_name)
    if send_at:
        state_label = config.ta("post_state_scheduled", when=broadcast.format_when(send_at))
    else:
        state_label = config.ta("post_state_now")
        broadcast.start(bot, post_id)
    await message.answer(config.ta("post_saved", id=post_id, state=state_label))
    view = await post_view(post_id)
    if view:
        await message.answer(view[0], reply_markup=view[1])


@router.callback_query(F.data.regexp(r"^ps:(o|prev|send|cancel|del|delok):\d+$"))
async def post_actions(cb: CallbackQuery, bot: Bot) -> None:
    _, op, raw = cb.data.split(":")
    post_id = int(raw)
    post = await db.get_post(post_id)
    if not post:
        await cb.answer(config.ta("panel_session_expired"), show_alert=True)
        return
    if op == "o":
        await cb.answer()
        await replace(cb, *await post_view(post_id))
    elif op == "prev":
        await cb.answer()
        preview = dict(post, target=f"me:{cb.from_user.id}")
        await broadcast.deliver(bot, preview)
    elif op == "send":
        await db.set_post_status(post_id, "sending", int(time.time()))
        broadcast.start(bot, post_id)
        await cb.answer(config.ta("post_state_now"))
        await replace(cb, *await post_view(post_id))
    elif op == "cancel":
        await db.set_post_status(post_id, "cancelled")
        await cb.answer()
        await replace(cb, *await post_view(post_id))
    elif op == "del":
        b = InlineKeyboardBuilder()
        b.button(text=config.ta("btn_panel_reset_confirm"), callback_data=f"ps:delok:{post_id}")
        b.button(text=config.ta("btn_panel_back"), callback_data=f"ps:o:{post_id}")
        b.adjust(2)
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=b.as_markup())
    elif op == "delok":
        await db.delete_post(post_id)
        await cb.answer()
        await replace(cb, *await list_view(post["kind"]))
