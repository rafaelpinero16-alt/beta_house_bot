"""Punto de entrada: instancia del bot, despachador, middlewares, verificación y tareas de fondo."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Message, Update

import config
import database as db
from handlers import admin_handlers, group_handlers, panel, user_handlers
from services import access, broadcast, catalog
from services.moderation import is_banned, load_banned

log = logging.getLogger("bot")


class AntiFloodMiddleware(BaseMiddleware):
    """Descarta ráfagas en chats privados (límite generoso para no afectar a usuarios reales)."""

    def __init__(self, limit: int = 20, window: float = 10.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(self, handler, event: Message, data):
        if event.chat.type == "private" and event.from_user:
            now = time.monotonic()
            hits = self._hits[event.from_user.id]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return None
            hits.append(now)
        return await handler(event, data)


async def _member(bot: Bot, chat_id: int, bot_id: int):
    chat = await bot.get_chat(chat_id)
    member = await bot.get_chat_member(chat_id, bot_id)
    return chat, member


async def _check_admin_group(bot: Bot, bot_id: int, errors: list[str]) -> None:
    label = f"Grupo de soporte ({config.ADMIN_GROUP_ID})"
    try:
        chat, member = await _member(bot, config.ADMIN_GROUP_ID, bot_id)
    except TelegramAPIError as exc:
        errors.append(f"{label}: inaccesible ({exc}). ¿El bot está dentro del grupo?")
        return
    if chat.type != ChatType.SUPERGROUP or not chat.is_forum:
        errors.append(f"{label}: debe ser un supergrupo con Temas (Topics) activados.")
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        errors.append(f"{label}: el bot debe ser administrador.")
    elif not getattr(member, "can_manage_topics", False):
        errors.append(f"{label}: falta el permiso 'Gestionar temas'.")


async def _check_log_chat(bot: Bot, bot_id: int, chat_id: int, name: str, errors: list[str]) -> None:
    label = f"{name} ({chat_id})"
    try:
        chat, member = await _member(bot, chat_id, bot_id)
    except TelegramAPIError as exc:
        errors.append(f"{label}: inaccesible ({exc}). ¿El bot está dentro?")
        return
    if chat.type == ChatType.CHANNEL:
        if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(member, "can_post_messages", False):
            errors.append(f"{label}: el bot debe ser administrador con permiso 'Publicar mensajes'.")
    elif member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER) and not getattr(
        member, "can_send_messages", False
    ):
        errors.append(f"{label}: el bot no puede enviar mensajes.")


class BanCheckMiddleware(BaseMiddleware):
    """Descarta en silencio TODAS las actualizaciones de un miembro vetado (mensajes, botones, pagos…).

    Excepción: las entradas/salidas de chats (chat_member) siguen llegando para poder volver a sacarlo
    si consigue entrar a un chat vinculado.
    """

    async def __call__(self, handler, event: Update, data):
        user = data.get("event_from_user")
        if (
            user is not None
            and user.id not in config.ADMIN_IDS
            and is_banned(user.id)
            and event.chat_member is None
            and event.my_chat_member is None
        ):
            return None
        return await handler(event, data)


async def verify_setup(bot: Bot) -> None:
    """Comprueba chats y permisos antes de recibir actualizaciones. Reporta todos los fallos a la vez."""
    me = await bot.get_me()
    errors: list[str] = []
    await _check_admin_group(bot, me.id, errors)
    await _check_log_chat(bot, me.id, config.SALES_LOG_CHANNEL_ID, "Canal de ventas", errors)
    if config.TIPS_ENABLED:
        await _check_log_chat(bot, me.id, config.TIPS_LOG_CHANNEL_ID, "Canal de donaciones", errors)
    if errors:
        raise RuntimeError("Verificación de Telegram fallida:\n  - " + "\n  - ".join(errors))

    # Productos: no bloquean el arranque (se gestionan desde el panel); se actualizan sus permisos.
    checked: set[int] = set()
    for product in config.all_products():
        if not product.chat_id or product.chat_id in checked:
            continue   # varios productos (creator/viewer) comparten chat: se verifica una vez
        checked.add(product.chat_id)
        ok, info = await catalog.check_product(bot, product.key)
        if not ok:
            log.warning("Chat %s (%s): %s", config.chat_label(product.chat_id), product.chat_id, config._plain(info))
    log.info(
        "Verificado @%s | gestión=%s | productos activos=%s | ventas=%s | donaciones=%s | admins=%s",
        me.username,
        config.ADMIN_GROUP_ID,
        ", ".join(p.key for p in config.active_products()) or "ninguno",
        config.SALES_LOG_CHANNEL_ID,
        config.TIPS_LOG_CHANNEL_ID if config.TIPS_ENABLED else "-",
        len(config.ADMIN_IDS),
    )


async def setup_commands(bot: Bot) -> None:
    """/start para todos; /panel visible solo en el chat privado de cada admin."""
    await bot.set_my_commands(
        [BotCommand(command="start", description=config.ta("cmd_desc_start"))], scope=BotCommandScopeDefault()
    )
    admin_cmds = [
        BotCommand(command="start", description=config.ta("cmd_desc_start")),
        BotCommand(command="panel", description=config.ta("cmd_desc_panel")),
    ]
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as exc:  # el admin aún no ha abierto el bot
            log.warning("No se pudo registrar /panel para %s: %s", admin_id, exc)


_LEGACY_KEYS = ("stars_price", "paypal_price_text", "vip_plan_days", "welcome_media",
                *(f"tip_amount_{i}" for i in range(1, 6)))


async def prepare_catalog() -> None:
    """Siembra y migra el catálogo (idempotente): productos, planes, secciones y botones.
    Los precios, la multimedia y las donaciones configurados en la versión anterior se conservan."""
    legacy = await db.pop_legacy_config(_LEGACY_KEYS)
    upgraded = await db.upgrade_legacy_buttons()
    created = await db.seed_products(config.PRODUCT_SEEDS, config.PLAN_MONTHS, config.PRODUCT_ROLES)
    products = len(created)
    # Planes iniciales: solo para productos recién creados y sin planes activos (nunca pisa lo editado en /panel).
    for key in created:
        if await db.has_active_plans(key):
            continue
        for months, stars, ext_price, active in config.PLAN_SEEDS.get(key, []):
            await db.update_plan(key, months, stars_price=stars, ext_price=ext_price, is_active=active)
    menus = await db.seed_menus(config.SYSTEM_MENUS)
    if legacy.get("welcome_media"):
        await db.update_menu("main", media=legacy["welcome_media"])
    legacy_tips = tuple(
        int(legacy[k]) for k in (f"tip_amount_{i}" for i in range(1, 6)) if k in legacy and legacy[k].isdigit()
        and int(legacy[k]) > 0
    )
    tips = legacy_tips or config.DEFAULT_TIP_AMOUNTS
    buttons = await db.seed_buttons(
        config.default_buttons(legacy_tips or None), config.DEFAULT_PAIRED_BUTTONS | config.tip_button_pairs(tips)
    )
    await catalog.reload_all()
    if legacy or upgraded or products or menus or buttons:
        log.info(
            "Catálogo preparado: %s ajustes migrados, %s botones actualizados, %s productos, %s secciones y %s botones nuevos",
            len(legacy), upgraded, products, menus, buttons,
        )


async def background(bot: Bot) -> None:
    """Tareas periódicas: enlaces caducados, recordatorios, vencimientos y anuncios programados."""
    while True:
        await access.sweep(bot)
        try:
            await broadcast.sweep(bot)
        except Exception:
            log.exception("Error al lanzar anuncios programados")
        await asyncio.sleep(config.SWEEP_INTERVAL_SECONDS)


async def main() -> None:
    logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    config.validate()
    logging.getLogger().setLevel(config.LOG_LEVEL)
    await db.init_db()
    await prepare_catalog()
    await load_banned()
    skipped = config.load_overrides(await db.get_all_config())
    if skipped:
        log.warning("Ajustes de /panel ignorados por inválidos u obsoletos: %s", ", ".join(skipped))
    log.info(
        "Catálogo: %s productos (%s activos) · %s secciones · %s ajustes personalizados · chats con roles: %s",
        len(config.all_products()), len(config.active_products()), len(config.all_menus()), config.custom_count(),
        ", ".join(str(c) for c in sorted(config.role_chat_ids())) or "ninguno",
    )

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(BanCheckMiddleware())
    dp.message.middleware(AntiFloodMiddleware())

    # Orden: /panel (admins en privado) → equipo (grupo de gestión y chats vinculados) → moderación por rol en
    # los grupos con roles (BETA HOUSE) → miembros (chat privado).
    dp.include_router(panel.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(group_handlers.router)
    dp.include_router(user_handlers.router)

    sweeper: asyncio.Task | None = None
    try:
        await verify_setup(bot)
        await bot.delete_webhook(drop_pending_updates=False)
        await setup_commands(bot)
        sweeper = asyncio.create_task(background(bot), name="background")
        log.info("Bot iniciado (polling).")
        # resolve_used_update_types() incluye chat_member, chat_join_request y my_chat_member.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if sweeper:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
        await db.close_db()
        await bot.session.close()


EXIT_CONFIG_ERROR = 78  # EX_CONFIG: systemd no reintenta en bucle (ver RestartPreventExitStatus)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        logging.critical("BOT_TOKEN rechazado por Telegram: revisa el token en .env.")
        sys.exit(EXIT_CONFIG_ERROR)
    except RuntimeError as exc:
        logging.critical("%s", exc)
        sys.exit(EXIT_CONFIG_ERROR)
