"""/panel — administración del bot desde Telegram (solo ADMIN_IDS, en chat privado).

Un archivo por módulo:
  home.py       Inicio, /panel, /cancelar
  welcome.py    1 · Bienvenida y anuncios
  products.py   2 · Suscripciones y productos (vincular grupos/canales, planes, accesos)
  links.py      3 · Enlaces (catálogo por categorías + personalizados)
  buttons.py    4 · Botones (todas las secciones + nombres de botones del sistema, equipo y panel)
  texts.py      5 · Textos y multimedia (secciones, mensajes automáticos, mensajes del equipo)
  donations.py  6 · Donaciones (rangos, botones de donación, publicaciones)
  bans.py       7 · Baneos
  support.py    8 · Contactos y SUPPORT
  clones.py     9 · Clonación autorizada
  status.py     /estado (diagnóstico) y /sincronizar
  posts.py      Anuncios y publicaciones (usado por 1 y 6)
  editor.py     Editor genérico de textos/enlaces/ajustes
  common.py     Utilidades y estados compartidos
"""
from aiogram import Router

from handlers.panel import (
    bans,
    buttons,
    clones,
    donations,
    editor,
    home,
    links,
    posts,
    products,
    status,
    support,
    texts,
    welcome,
)

router = Router(name="panel")
for _module in (home, status, editor, welcome, products, links, buttons, texts, donations, posts, bans, support, clones):
    router.include_router(_module.router)

__all__ = ["router"]
