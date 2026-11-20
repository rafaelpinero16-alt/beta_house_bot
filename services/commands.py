"""Menú «/» de Telegram: qué comandos ve cada quien (se registran solos; no hace falta configurarlos en @BotFather).

  · Miembros (todos los chats privados): /start /menu /creador /espectador /pagar /mi_plan /soporte /donar /idioma
  · Admins (su chat privado con el bot): lo anterior + /panel /estado /sincronizar
  · Grupo de gestión: /aprobar /rol /canal /revocar /cerrar /ban /unban

Las descripciones salen en español o inglés según el idioma de la app de Telegram de cada persona.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

import config

log = logging.getLogger(__name__)
_admins_done: set[int] = set()


def _commands(rows: list[tuple[str, str, str]], lang: str) -> list[BotCommand]:
    return [BotCommand(command=cmd, description=(es if lang == "es" else en)[:256]) for cmd, es, en in rows]


async def _set(bot: Bot, rows: list[tuple[str, str, str]], scope) -> None:
    await bot.set_my_commands(_commands(rows, "es"), scope=scope)                       # por defecto: español
    await bot.set_my_commands(_commands(rows, "en"), scope=scope, language_code="en")   # app en inglés


async def ensure_admin_commands(bot: Bot, user_id: int, force: bool = False) -> bool:
    """Registra el menú de admin en el chat privado del admin (falla si aún no ha abierto el bot)."""
    if user_id not in config.ADMIN_IDS or (user_id in _admins_done and not force):
        return False
    try:
        await _set(bot, config.MEMBER_COMMANDS + config.ADMIN_PRIVATE_COMMANDS, BotCommandScopeChat(chat_id=user_id))
        _admins_done.add(user_id)
        return True
    except TelegramAPIError as exc:
        log.info("Menú de admin pendiente para %s (debe abrir el bot primero): %s", user_id, exc.message)
        return False


async def setup_commands(bot: Bot) -> None:
    # Grupos (salvo el de gestión): sin comandos, para no ensuciar BETA HOUSE.
    await bot.set_my_commands([], scope=BotCommandScopeDefault())
    await _set(bot, config.MEMBER_COMMANDS, BotCommandScopeAllPrivateChats())
    try:
        await _set(bot, config.TEAM_COMMANDS, BotCommandScopeChat(chat_id=config.ADMIN_GROUP_ID))
    except TelegramAPIError as exc:
        log.warning("No se pudieron registrar los comandos del grupo de gestión: %s", exc.message)
    for admin_id in config.ADMIN_IDS:
        await ensure_admin_commands(bot, admin_id, force=True)
