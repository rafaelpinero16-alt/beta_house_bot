"""PANEL DE CONTROL del bot: variables de entorno, productos, precios, enlaces, textos y ajustes editables.

Regla de oro: los handlers y servicios no contienen textos, montos ni enlaces.

Dos capas de configuración:
  1. Valores por defecto  -> .env (IDs, credenciales, precios iniciales) y los diccionarios de este archivo.
  2. Ajustes en caliente  -> /panel en Telegram, guardados en la tabla `bot_config`. Tienen prioridad sobre
     la capa 1 y se aplican al instante, sin redesplegar. "Restaurar" vuelve al valor por defecto.
"""
from __future__ import annotations

import calendar
import os
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


_CONFIG_ERRORS: list[str] = []  # errores de formato detectados al leer .env (se reportan en validate())


def _raw(name: str) -> str:
    """Valor crudo sin comentarios en línea, espacios ni comillas envolventes."""
    raw = os.getenv(name, "").split(" #", 1)[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def _int(name: str, default: int = 0) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name} debe ser un número entero (valor actual: {raw!r}).")
        return default


def _str(name: str, default: str = "") -> str:
    return _raw(name) or default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "si", "sí", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _CONFIG_ERRORS.append(f"{name} debe ser true/false (valor actual: {raw!r}).")
    return default


def _int_list(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    """Acepta '1,2,3', '1;2;3', '{1, 2}', '(1, 2, 3)' o '[1, 2]'."""
    raw = _raw(name).strip("{}[]() ")
    if not raw:
        return default
    values: list[int] = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except ValueError:
            _CONFIG_ERRORS.append(f"{name} contiene un valor no numérico: {token!r}.")
    return tuple(values) if values else default


# ===========================================================================
# Telegram (obligatorios: se validan al arrancar)
# ===========================================================================
BOT_TOKEN = _str("BOT_TOKEN")
ADMIN_GROUP_ID = _int("ADMIN_GROUP_ID")              # Supergrupo de gestión con Temas
VIP_CHAT_ID = _int("VIP_CHAT_ID")                    # Compatibilidad con despliegues/clones anteriores
BETA_HOUSE_CHAT_ID = _int("BETA_HOUSE_CHAT_ID") or VIP_CHAT_ID  # Supergrupo VIP de BETA HOUSE
SALES_LOG_CHANNEL_ID = _int("SALES_LOG_CHANNEL_ID")  # Registro de ventas (creadores + espectadores)
TIPS_LOG_CHANNEL_ID = _int("TIPS_LOG_CHANNEL_ID")    # Contabilidad separada de donaciones
ADMIN_IDS: frozenset[int] = frozenset(_int_list("ADMIN_IDS"))  # Admins: aprueban pagos y usan /panel


# ===========================================================================
# Productos (tarifas/roles con suscripción) — CATÁLOGO DINÁMICO
# ---------------------------------------------------------------------------
# En BETA HOUSE dos productos apuntan al MISMO supergrupo: «creator» y «viewer». Cada uno tiene sus planes,
# precios (Stars y PayPal) y su ROL dentro del grupo:
#   creator → puede publicar texto, multimedia y enlaces (promociones).
#   viewer  → solo lectura: el bot le bloquea escritura, multimedia y enlaces (sí entra a los videochats).
# Se pueden vincular más grupos/canales o tarifas desde /panel → 🛒 Suscripciones y productos.
# ===========================================================================
PRODUCT_SEEDS: list[tuple[str, int, str, str]] = [
    # (clave, chat_id, nombre ES, nombre EN)
    ("creator", BETA_HOUSE_CHAT_ID, "BETA HOUSE · CREADOR VIP", "BETA HOUSE · VIP CREATOR"),
    ("viewer", BETA_HOUSE_CHAT_ID, "BETA HOUSE · ESPECTADOR VIP", "BETA HOUSE · VIP VIEWER"),
]
MEMBER_ROLES: tuple[str, ...] = ("creator", "viewer", "none")
PRODUCT_ROLES: dict[str, str] = {"creator": "creator", "viewer": "viewer"}   # rol de fábrica de cada semilla
# Planes iniciales (solo al crear el producto por primera vez): (meses, precio Stars, precio PayPal, activo).
# Los demás planes (3, 6, 12 meses y de por vida) quedan creados y apagados, listos para editar en el /panel.
PLAN_SEEDS: dict[str, list[tuple[int, int, str, int]]] = {
    "creator": [(1, _int("CREATOR_STARS_PRICE", 1050), _str("CREATOR_PAYPAL_PRICE", "15 $ USD"), 1)],
    "viewer": [(1, _int("VIEWER_STARS_PRICE", 350), _str("VIEWER_PAYPAL_PRICE", "5 $ USD"), 1)],
}
VIEWER_MODES: tuple[str, ...] = ("readonly", "text")
# readonly → el espectador no puede escribir ni enviar multimedia ni enlaces (recomendado).
# text     → puede escribir texto, pero no multimedia; el bot borra sus mensajes con enlaces.
PLAN_MONTHS: tuple[int, ...] = (1, 3, 6, 12, 0)   # 0 = de por vida
ACCESS_MODES: tuple[str, ...] = ("request", "link")
# request → enlace con solicitud de ingreso: el bot SOLO aprueba al miembro que pagó (recomendado).
# link    → enlace de 1 solo uso (member_limit=1).


@dataclass(frozen=True, slots=True)
class Product:
    key: str
    chat_id: int
    chat_type: str
    name_es: str
    name_en: str
    is_active: bool
    can_invite: bool = False
    can_restrict: bool = False
    member_role: str = "none"   # creator | viewer | none


@dataclass(frozen=True, slots=True)
class Plan:
    product: str
    months: int          # 0 = de por vida
    stars_price: int     # 0 = no se vende con Stars
    ext_price: str       # '' = no se ofrece con PayPal/otros métodos
    is_active: bool


# Cachés en memoria (se cargan desde la base de datos al arrancar y tras cada cambio en el /panel)
_products: dict[str, Product] = {}
_plans: dict[str, list[Plan]] = {}


def set_products(products: list[Product], plans: list[Plan]) -> None:
    _products.clear()
    _products.update({p.key: p for p in products})
    _plans.clear()
    for plan in sorted(plans, key=lambda p: (p.months == 0, p.months)):
        _plans.setdefault(plan.product, []).append(plan)


def all_products() -> list[Product]:
    return list(_products.values())


def active_products() -> list[Product]:
    """Productos activos con chat vinculado: el bot puede generar enlaces y expulsar al vencer."""
    return [p for p in _products.values() if p.is_active and p.chat_id]


def get_product(key: str) -> Product | None:
    return _products.get(key)


def product_by_chat(chat_id: int) -> Product | None:
    """Compatibilidad: primer producto del chat. Un chat puede tener varios (usa products_by_chat)."""
    return next((p for p in _products.values() if p.chat_id == chat_id), None)


def products_by_chat(chat_id: int) -> list[Product]:
    return [p for p in _products.values() if p.chat_id == chat_id]


def role_chat_ids() -> set[int]:
    """Chats donde el bot gestiona permisos por rol (algún producto con rol creator/viewer)."""
    return {p.chat_id for p in _products.values() if p.chat_id and p.member_role in ("creator", "viewer")}


def chat_label(chat_id: int) -> str:
    """Nombre de un chat para el equipo: los productos vinculados a él (p. ej. «Creador / Espectador»)."""
    names = [p.name_es for p in products_by_chat(chat_id)]
    return " / ".join(names) if names else str(chat_id)


def product_chat_ids() -> set[int]:
    return {p.chat_id for p in _products.values() if p.chat_id}


def plans(product_key: str, active_only: bool = True) -> list[Plan]:
    return [p for p in _plans.get(product_key, []) if p.is_active or not active_only]


def get_plan(product_key: str, months: int) -> Plan | None:
    return next((p for p in _plans.get(product_key, []) if p.months == months), None)


def stars_plans(product_key: str) -> list[Plan]:
    return [p for p in plans(product_key) if p.stars_price > 0]


def ext_plans(product_key: str) -> list[Plan]:
    return [p for p in plans(product_key) if p.ext_price]


# ===========================================================================
# Donaciones, pagos y tiempos
# ===========================================================================
DEFAULT_STARS_PRICE = _int("STARS_PRICE", 350)               # semilla: VIP 1 mes en Stars
DEFAULT_PAYPAL_PRICE_TEXT = _str("PAYPAL_PRICE_TEXT", "7 $")  # semilla: VIP 1 mes por PayPal
DEFAULT_REMINDER_DAYS = max(3, _int("REMINDER_DAYS", 3))
DEFAULT_TIP_AMOUNTS: tuple[int, ...] = tuple(
    a for a in (
        _int(f"TIP_AMOUNT_{i}", d) for i, d in enumerate((150, 250, 500, 750, 1000), start=1)
    ) if a > 0
)
TIPS_ENABLED = _bool("TIPS_ENABLED", True)
SUBSCRIPTION_TITLE = _str("SUBSCRIPTION_TITLE", "BETA HOUSE VIP")
STARS_PRICE_MIN, STARS_PRICE_MAX = 1, 100_000
INVITE_TTL_MIN_MINUTES, INVITE_TTL_MAX_MINUTES = 5, 47 * 60   # el bot solo puede borrar sus mensajes 48 h
DEFAULT_INVITE_TTL_MINUTES = max(
    INVITE_TTL_MIN_MINUTES, min(_int("INVITE_TTL_SECONDS", 86400) // 60, INVITE_TTL_MAX_MINUTES)
)
SWEEP_INTERVAL_SECONDS = _int("SWEEP_INTERVAL_SECONDS", 60)
TIMEZONE = _str("TIMEZONE", "America/Bogota")                 # para programar anuncios
BROADCAST_DELAY_SECONDS = 0.05                                 # ~20 mensajes/s (límite seguro de Telegram)

# Payloads de facturas
SUB_PAYLOAD_PREFIX = "sp:"          # sp:<producto>:<meses>:<precio>
TIP_PAYLOAD_PREFIX = "tip_stars_"
LEGACY_VIP_PAYLOAD = "vip_pass_stars"
LEGACY_SUB_PREFIX = "sub:"          # sub:<producto>:<precio>:<días> (versión anterior)


def tip_payload(amount: int) -> str:
    return f"{TIP_PAYLOAD_PREFIX}{amount}"


def sub_payload(product: str, months: int, price: int) -> str:
    return f"{SUB_PAYLOAD_PREFIX}{product}:{months}:{price}"


def parse_sub_payload(payload: str) -> tuple[str, int, int] | None:
    """(producto, meses, precio) de una factura de suscripción, incluidos formatos anteriores."""
    try:
        if payload.startswith(SUB_PAYLOAD_PREFIX):
            product, months, price = payload[len(SUB_PAYLOAD_PREFIX):].split(":")
            return product, int(months), int(price)
        if payload.startswith(LEGACY_SUB_PREFIX):
            product, price, days = payload[len(LEGACY_SUB_PREFIX):].split(":")
            return product, (0 if int(days) == 0 else max(1, round(int(days) / 30))), int(price)
    except ValueError:
        return None
    if payload == LEGACY_VIP_PAYLOAD:
        return "creator", 1, -1  # precio -1 = aceptar el monto cobrado (factura antigua ya pagada)
    return None


def add_months(epoch: int, months: int) -> int:
    """Suma meses de calendario (31-ene + 1 mes = 28/29-feb)."""
    start = datetime.fromtimestamp(epoch, timezone.utc)
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return int(start.replace(year=year, month=month, day=day).timestamp())


# ===========================================================================
# Infraestructura
# ===========================================================================
DB_PATH = _str("DB_PATH", "bot_data.db")
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()
TELEGRAM_TEXT_LIMIT = 4096
ALERT_LIMIT = 200                      # límite de Telegram para mensajes emergentes
MAX_BUTTONS_PER_MENU = 12             # botones configurables por menú (+ el de volver)

# ===========================================================================
# ENLACES — 7 categorías (todas editables desde /panel → 🔗 Enlaces)
#   clave: (categoría, etiqueta, valor por defecto, opcional)
# Opcional = puede quedar vacío; el botón que lo usa se oculta solo.
# ===========================================================================
_PAYPAL = _str("PAYPAL_LINK", "https://paypal.me/JaviiTorresDIGITAL")
LINK_CATEGORIES: dict[str, str] = {
    "groups": "🏠 Grupo BETA HOUSE",
    "pay_creator": "🔥 Pagos · Creadores",
    "pay_viewer": "🎟️ Pagos · Espectadores",
    "pay_general": "💳 Pagos · PayPal general",
    "pay_donate": "⭐ Pagos · Donaciones",
    "support": "💬 Contacto directo con los creadores",
}
LINK_CATALOG: dict[str, tuple[str, str, str, bool]] = {
    "rules_link": ("groups", "BETA HOUSE · reglas", "", True),
    "community_link": ("groups", "BETA HOUSE · grupo/canal público", "", True),
    "paypal_creator_link": ("pay_creator", "PayPal (creadores)", _str("PAYPAL_CREATOR_LINK", _PAYPAL), False),
    "paypal_viewer_link": ("pay_viewer", "PayPal (espectadores)", _str("PAYPAL_VIEWER_LINK", _PAYPAL), False),
    "paypal_link": ("pay_general", "PayPal (acceso directo del menú)", _PAYPAL, False),
    "binance_link": ("pay_general", "Binance (URL o Pay ID)", "", True),
    "paypal_donate_link": ("pay_donate", "PayPal (donaciones)", _PAYPAL, False),
    "contact_telegram_link": ("support", "Telegram de los creadores", _str("CONTACT_TELEGRAM_LINK", "https://t.me/Tjavier_96"), False),
    "contact_x_link": ("support", "X de los creadores", _str("CONTACT_X_LINK", "https://x.com/Javiitorrees96"), True),
}

# ===========================================================================
# Textos para el MIEMBRO (bilingües: TODO cambia según el idioma elegido)
# ---------------------------------------------------------------------------
# Variables disponibles en todos los textos (se rellenan con el valor vigente):
#   {creator_stars} {creator_paypal} precio mensual de Creador · {viewer_stars} {viewer_paypal} precio mensual de
#   Espectador · {stars_price} {paypal_price} (= Creador, compatibilidad) · {vip_days} días de un mes
#   {ttl_min} minutos de validez del enlace
# Específicas: {mention} {id} {name} (bienvenida), {amount} (donaciones), {product} {plan} (accesos).
# Los textos de EDITABLE_TEXT_KEYS se pueden cambiar desde /panel → 📝 Textos.
# ===========================================================================
TEXTS: dict[str, dict[str, str]] = {
    "es": {
        # ---- Idioma ----
        "choose_lang": "🌐 <b>Selecciona tu idioma</b>\n🌐 <b>Choose your language</b>",
        "btn_lang_es": "🇪🇸 ESPAÑOL",
        "btn_lang_en": "🇺🇸 ENGLISH",
        "lang_set": "✅ Español",
        "btn_back": "⬅️ VOLVER",
        "btn_home": "🏠 MENÚ PRINCIPAL",
        "header_hint": "👇 Elige Creador o Espectador en los botones de abajo",
        # ---- Bienvenida (EDITABLE desde /panel → 📝 Textos y multimedia: welcome) ----
        "welcome": (
            "👋 {mention} · <code>{id}</code>\n\n"
            "Bienvenido a <b>🏠 BETA HOUSE VIP</b>, el grupo privado donde <b>creadores de contenido</b> y "
            "<b>espectadores</b> se conectan de forma directa, sin intermediarios.\n\n"
            "🔥 <b>Si eres CREADOR</b>\n"
            "▪ Promoción cruzada de tus plataformas en <b>Beta House</b>, <b>PigBros</b> y <b>The Bunker</b>.\n"
            "▪ Publica tus promos con enlaces, textos, fotos y videos dentro del grupo.\n"
            "▪ Prioridad en los <b>videochats en vivo</b> para mostrarte ante toda la comunidad.\n\n"
            "🎟️ <b>Si eres ESPECTADOR</b>\n"
            "▪ Acceso exclusivo al grupo VIP y a todo lo que publican los creadores verificados.\n"
            "▪ Contacto directo con los creadores que están dentro del grupo.\n"
            "▪ Participación en las <b>videollamadas grupales</b> con todos.\n\n"
            "📽 <b>Videochats:</b> se abren dentro del grupo; los creadores tienen prioridad para presentarse y "
            "los espectadores pueden unirse, ver e interactuar.\n\n"
            "Al confirmar tu pago, este bot te da acceso al grupo y activa automáticamente los permisos de tu rol.\n\n"
            "👇 Usa los botones de abajo para suscribirte o resolver cualquier duda:"
        ),
        # ---- Creadores (EDITABLES: creator_info, creator_paypal) ----
        "creator_info": (
            "🔥 <b>SUSCRIPCIÓN PARA CREADORES · BETA HOUSE VIP</b>\n\n"
            "Como creador podrás <b>publicar tus promociones</b> dentro del grupo: enlaces a tus plataformas "
            "(OnlyFans, JustForFans, Cam4, canales de Telegram…), textos, fotos y videos.\n\n"
            "✅ <b>Beneficios</b>\n"
            "▪ Promoción cruzada en Beta House, PigBros y The Bunker.\n"
            "▪ Permisos de publicación activados automáticamente al entrar.\n"
            "▪ Prioridad en los videochats en vivo.\n"
            "▪ Contacto directo con los espectadores del grupo.\n\n"
            "📋 <b>Requisitos de contenido</b>\n"
            "▪ Solo promociona plataformas y contenido propios.\n"
            "▪ Respeta las reglas del grupo y la frecuencia de publicación indicada por los administradores.\n"
            "▪ Prohibido el spam, el contenido de terceros sin permiso y cualquier material ilegal.\n\n"
            "💰 <b>Precio:</b> desde <b>{creator_paypal}</b> / mes por PayPal o <b>{creator_stars} ⭐</b> con "
            "Telegram Stars (planes de varios meses con descuento según disponibilidad).\n\n"
            "1️⃣ Paga con <b>Telegram Stars</b> y recibe tu acceso al instante, o\n"
            "2️⃣ Paga con <b>PayPal</b> y luego pulsa <b>«CONFIRMAR PAGO PAYPAL»</b> para enviar tu captura."
        ),
        "creator_paypal": (
            "💎 <b>CREADORES · PAGO CON PAYPAL</b>\n\n"
            "1️⃣ Envía el monto de tu plan (libre de comisiones) con el botón «PAGAR CON PAYPAL». "
            "Plan mensual: <b>{creator_paypal}</b>.\n\n"
            "2️⃣ Vuelve a este chat y pulsa <b>«YA PAGUÉ: ENVIAR COMPROBANTE»</b>, elige el plan que pagaste y "
            "envía la <b>captura de pantalla</b> del pago.\n\n"
            "3️⃣ Cuando confirmemos tu pago recibirás aquí tu enlace personal a BETA HOUSE con los "
            "<b>permisos de creador</b> ya activados. 🔐"
        ),
        # ---- Espectadores (EDITABLES: viewer_info, viewer_paypal) ----
        "viewer_info": (
            "🎟️ <b>SUSCRIPCIÓN PARA ESPECTADORES · BETA HOUSE VIP</b>\n\n"
            "Como espectador tendrás acceso al grupo VIP para conocer a los creadores, ver sus promociones y "
            "contactarlos directamente.\n\n"
            "📽 <b>¿Cómo funcionan los videochats?</b>\n"
            "▪ Los videochats se abren dentro del grupo; entra desde el botón de videochat en la parte superior.\n"
            "▪ Puedes ver e interactuar con los creadores y el resto de participantes.\n"
            "▪ Los administradores dan la palabra o la cámara durante la sesión.\n\n"
            "🤝 <b>Normas de respeto</b>\n"
            "▪ Trata con respeto a creadores y espectadores; nada de acoso ni insistencia.\n"
            "▪ No grabes ni compartas fuera del grupo el contenido de los videochats.\n"
            "▪ En el grupo tu cuenta es de <b>solo lectura</b>: el chat está reservado a las promociones de los "
            "creadores. Para hablar con un creador, escríbele por privado.\n\n"
            "💰 <b>Precio:</b> desde <b>{viewer_paypal}</b> / mes por PayPal o <b>{viewer_stars} ⭐</b> con "
            "Telegram Stars.\n\n"
            "Elige cómo quieres pagar con los botones de abajo."
        ),
        "viewer_paypal": (
            "💎 <b>ESPECTADORES · PAGO CON PAYPAL</b>\n\n"
            "1️⃣ Envía el monto de tu plan (libre de comisiones) con el botón «PAGAR CON PAYPAL». "
            "Plan mensual: <b>{viewer_paypal}</b>.\n\n"
            "2️⃣ Vuelve a este chat, pulsa <b>«YA PAGUÉ: ENVIAR COMPROBANTE»</b>, elige el plan y envía la "
            "<b>captura de pantalla</b> del pago.\n\n"
            "3️⃣ Cuando confirmemos tu pago recibirás aquí tu enlace personal a BETA HOUSE. 🔐"
        ),
        # ---- PayPal general (EDITABLE: paypal_hub) ----
        "paypal_hub": (
            "💳 <b>SUSCRIPCIÓN CON PAYPAL Y CONFIRMACIÓN DE PAGOS</b>\n\n"
            "1️⃣ Paga con el botón «IR A PAYPAL» el monto exacto del plan que elegiste "
            "(Creador: <b>{creator_paypal}</b> · Espectador: <b>{viewer_paypal}</b> por mes).\n\n"
            "2️⃣ Pulsa <b>«CONFIRMAR PAGO PAYPAL»</b>, indica si pagaste como <b>Creador</b> o como "
            "<b>Espectador</b>, elige el plan y envía la captura de tu pago.\n\n"
            "3️⃣ Verificamos tu pago y te enviamos aquí tu acceso con los permisos de tu rol. ⏳"
        ),
        # ---- Planes y facturas ----
        "stars_invoice_desc": "Plan {plan} · {product}. Acceso con enlace personal que solo tú puedes usar.",
        "stars_invoice_label": "Suscripción",
        "choose_plan": "⭐ <b>{product}</b>\n\nElige tu plan de suscripción. Pagas con Telegram Stars y recibes tu acceso al instante:",
        "choose_plan_receipt": "📸 <b>{product}</b>\n\nElige el plan que pagaste (o vas a pagar) con PayPal:",
        "choose_product_receipt": "📸 <b>CONFIRMA TU PAGO</b>\n\n¿Pagaste como <b>Creador</b> o como <b>Espectador</b>?",
        "no_plans": "Este plan no está disponible en este momento.",
        "plan_btn_stars": "⭐ {plan} · {price} XTR",
        "plan_btn_ext": "💳 {plan} · {price}",
        "plan_month": "1 mes",
        "plan_months": "{n} meses",
        "plan_lifetime": "De por vida",
        "plan_until": "{plan} (hasta el {date})",
        "product_unavailable": "Esta suscripción no está disponible en este momento.",
        "receipt_instructions": (
            "📸 <b>CONFIRMA TU PAGO</b>\n\n"
            "Suscripción: <b>{product}</b>\nPlan: <b>{plan}</b>\nPrecio: <b>{price}</b>\n\n"
            "Envía ahora en este chat la <b>captura de pantalla</b> de tu pago procesado con éxito.\n\n"
            "Verificaremos tu pago y te enviaremos aquí tu acceso personal. ⏳"
        ),
        "binance_info": "🟡 ID de pago: {value}",
        # ---- Donaciones (EDITABLES: donations, tip_thanks) ----
        "donations": (
            "⭐ <b>DONACIONES</b>\n\n"
            "Si quieres apoyar a BETA HOUSE y a todo el equipo que mantiene vivo este espacio, puedes enviar una "
            "donación voluntaria. ¡Cada aporte nos ayuda a seguir creciendo! 💜\n\n"
            "👇 Elige cuántas ⭐ Estrellas quieres donar o hazlo por PayPal:"
        ),
        "btn_tip_option": "⭐ {amount}",
        "tip_invoice_title": "Donación a BETA HOUSE",
        "tip_invoice_desc": "Donación voluntaria de {amount} Estrellas para BETA HOUSE. ¡Gracias!",
        "tip_invoice_label": "Donación",
        "tip_invalid": "Esta donación no está disponible ahora.",
        "tips_disabled": "Las donaciones están desactivadas en este momento.",
        "tip_thanks": "💜 <b>¡Gracias por tu donación!</b>\n\nRecibimos tus <b>{amount} ⭐</b>. Tu apoyo mantiene vivo BETA HOUSE.",
        # ---- Contacto directo (EDITABLES: support_info, support_wait) ----
        "support_info": (
            "💬 <b>CONTACTA DIRECTAMENTE CON LOS CREADORES</b>\n\n"
            "A partir de ahora, cualquier mensaje que envíes en este chat (texto, fotos, audios, videos…) llegará "
            "directamente al equipo de BETA HOUSE. Te responderemos lo antes posible.\n\n"
            "👇 Escribe tu mensaje cuando quieras."
        ),
        "support_wait": (
            "✅ Recibimos tu mensaje. Si no obtienes respuesta en las próximas horas, también puedes "
            "contactarnos con los siguientes botones:"
        ),
        "support_closed_hint": "💬 Para escribirnos directamente pulsa el botón de abajo.",
        "btn_open_support": "💬 CONTACTAR CON LOS CREADORES",
        "support_ended": (
            "🔚 La conversación con el equipo terminó. ¡Gracias por escribirnos!\n\n"
            "Si necesitas algo más, pulsa el botón para abrir un chat nuevo."
        ),
        # ---- Pagos con Stars ----
        "invoice_invalid": "Factura no válida o desactualizada. Usa /start de nuevo.",
        "stars_fallback": "✅ Pago recibido con éxito. Nuestro equipo te enviará tu acceso en breve.",
        # ---- Comprobantes ----
        "send_image": "📸 Para validar tu pago envía la captura como <b>foto o archivo</b>.",
        "receipt_received": (
            "✅ <b>¡Comprobante recibido!</b>\n\nEstamos verificando tu pago. En cuanto se confirme recibirás aquí "
            "tu enlace de acceso personal. ⏳"
        ),
        "receipt_rejected": (
            "⚠️ No pudimos validar tu comprobante de pago. Revisa que la captura sea correcta o escríbenos aquí "
            "si tienes dudas."
        ),
        # ---- Accesos ----
        "access_granted": (
            "✅ <b>¡PAGO CONFIRMADO!</b>\n\n"
            "Tu acceso a <b>{product}</b> está listo.\n🗓 Plan: <b>{plan}</b>\n\n"
            "Pulsa el botón para entrar. 🔐 Es un enlace <b>personal</b>, válido por {ttl_min} min, y este "
            "mensaje se <b>autodestruirá</b> en cuanto lo uses. Al entrar, el bot activará los permisos de tu rol."
        ),
        "btn_join": "🚪 ENTRAR A {product}",
        "access_renewed": (
            "✅ <b>¡RENOVACIÓN CONFIRMADA!</b>\n\n"
            "Tu suscripción a <b>{product}</b> se amplió.\n🗓 Ahora vence: <b>{date}</b>\n\n"
            "Sigues dentro: no necesitas un enlace nuevo. 🔥"
        ),
        "access_activated": (
            "✅ <b>¡PAGO CONFIRMADO!</b>\n\n"
            "Tu suscripción a <b>{product}</b> está activa.\n🗓 Plan: <b>{plan}</b>\n\n"
            "Ya estás dentro del grupo, así que no necesitas un enlace nuevo: tus permisos se actualizaron. 🔥"
        ),
        "link_expired": "⌛ Tu enlace de acceso expiró sin usarse. Escríbenos aquí y te enviaremos uno nuevo.",
        "membership_expired": (
            "⌛ Tu suscripción a <b>{product}</b> terminó y tu acceso se cerró.\n\n"
            "¡Gracias por ser parte de BETA HOUSE! Renuévala cuando quieras con el botón de abajo."
        ),
        "membership_reminder": (
            "⏰ Tu suscripción a <b>{product}</b> vence el <b>{date}</b> (en {days_left} día(s)).\n\n"
            "Renuévala ahora para no perder tu acceso. 🔥"
        ),
        "btn_renew": "🔄 RENOVAR SUSCRIPCIÓN",
        # ---- Roles dentro del grupo (EDITABLES: role_creator_on, role_viewer_on, viewer_blocked) ----
        "role_creator_on": (
            "🔥 <b>¡Permisos de CREADOR activados!</b>\n\n"
            "Ya puedes publicar en BETA HOUSE tus promociones con enlaces, textos, fotos y videos. "
            "Recuerda respetar las reglas del grupo."
        ),
        "role_viewer_on": (
            "🎟️ <b>¡Bienvenido a BETA HOUSE como ESPECTADOR!</b>\n\n"
            "Tu cuenta en el grupo es de <b>solo lectura</b>: puedes ver todas las promociones, contactar a los "
            "creadores por privado y unirte a los videochats."
        ),
        "viewer_blocked": (
            "🔒 Tu suscripción de <b>espectador</b> no permite publicar en BETA HOUSE (mensajes, multimedia ni "
            "enlaces). Si eres creador, suscríbete como <b>Creador</b> desde el menú del bot."
        ),
        # ---- Errores ----
        "relay_error": "⚠️ No pudimos entregar tu mensaje. Inténtalo de nuevo en unos minutos.",
    },
    "en": {
        # ---- Language ----
        "choose_lang": "🌐 <b>Selecciona tu idioma</b>\n🌐 <b>Choose your language</b>",
        "btn_lang_es": "🇪🇸 ESPAÑOL",
        "btn_lang_en": "🇺🇸 ENGLISH",
        "lang_set": "✅ English",
        "btn_back": "⬅️ BACK",
        "btn_home": "🏠 MAIN MENU",
        "header_hint": "👇 Choose Creator or Viewer in the buttons below",
        # ---- Welcome ----
        "welcome": (
            "👋 {mention} · <code>{id}</code>\n\n"
            "Welcome to <b>🏠 BETA HOUSE VIP</b>, the private group where <b>content creators</b> and "
            "<b>viewers</b> connect directly, with no middlemen.\n\n"
            "🔥 <b>If you are a CREATOR</b>\n"
            "▪ Cross-promotion of your platforms in <b>Beta House</b>, <b>PigBros</b> and <b>The Bunker</b>.\n"
            "▪ Post your promos with links, text, photos and videos inside the group.\n"
            "▪ Priority in the <b>live video chats</b> to show yourself to the whole community.\n\n"
            "🎟️ <b>If you are a VIEWER</b>\n"
            "▪ Exclusive access to the VIP group and everything verified creators post.\n"
            "▪ Direct contact with the creators inside the group.\n"
            "▪ Join the <b>group video calls</b> with everyone.\n\n"
            "📽 <b>Video chats:</b> they open inside the group; creators get priority to present themselves and "
            "viewers can join, watch and interact.\n\n"
            "Once your payment is confirmed, this bot gives you access to the group and automatically enables "
            "your role's permissions.\n\n"
            "👇 Use the buttons below to subscribe or get help:"
        ),
        # ---- Creators ----
        "creator_info": (
            "🔥 <b>CREATOR SUBSCRIPTION · BETA HOUSE VIP</b>\n\n"
            "As a creator you can <b>post your promotions</b> inside the group: links to your platforms "
            "(OnlyFans, JustForFans, Cam4, Telegram channels…), text, photos and videos.\n\n"
            "✅ <b>Benefits</b>\n"
            "▪ Cross-promotion in Beta House, PigBros and The Bunker.\n"
            "▪ Posting permissions enabled automatically when you join.\n"
            "▪ Priority in live video chats.\n"
            "▪ Direct contact with the group's viewers.\n\n"
            "📋 <b>Content requirements</b>\n"
            "▪ Only promote your own platforms and content.\n"
            "▪ Follow the group rules and the posting frequency set by the admins.\n"
            "▪ No spam, no third-party content without permission and no illegal material.\n\n"
            "💰 <b>Price:</b> from <b>{creator_paypal}</b> / month via PayPal or <b>{creator_stars} ⭐</b> with "
            "Telegram Stars (multi-month plans with discounts when available).\n\n"
            "1️⃣ Pay with <b>Telegram Stars</b> and get instant access, or\n"
            "2️⃣ Pay with <b>PayPal</b> and then tap <b>«CONFIRM PAYPAL PAYMENT»</b> to send your screenshot."
        ),
        "creator_paypal": (
            "💎 <b>CREATORS · PAY WITH PAYPAL</b>\n\n"
            "1️⃣ Send your plan amount (fee-free) with the «PAY WITH PAYPAL» button. "
            "Monthly plan: <b>{creator_paypal}</b>.\n\n"
            "2️⃣ Come back to this chat, tap <b>«I PAID: SEND RECEIPT»</b>, choose the plan you paid and send "
            "the payment <b>screenshot</b>.\n\n"
            "3️⃣ Once we confirm your payment you'll receive your personal link to BETA HOUSE here, with "
            "<b>creator permissions</b> already enabled. 🔐"
        ),
        # ---- Viewers ----
        "viewer_info": (
            "🎟️ <b>VIEWER SUBSCRIPTION · BETA HOUSE VIP</b>\n\n"
            "As a viewer you get access to the VIP group to meet the creators, see their promotions and contact "
            "them directly.\n\n"
            "📽 <b>How do the video chats work?</b>\n"
            "▪ Video chats open inside the group; join from the video chat button at the top.\n"
            "▪ You can watch and interact with the creators and other participants.\n"
            "▪ Admins give the floor or camera during the session.\n\n"
            "🤝 <b>Rules of respect</b>\n"
            "▪ Treat creators and viewers with respect; no harassment or insistence.\n"
            "▪ Do not record or share video chat content outside the group.\n"
            "▪ In the group your account is <b>read-only</b>: the chat is reserved for creators' promotions. "
            "To talk to a creator, message them privately.\n\n"
            "💰 <b>Price:</b> from <b>{viewer_paypal}</b> / month via PayPal or <b>{viewer_stars} ⭐</b> with "
            "Telegram Stars.\n\n"
            "Choose how you want to pay with the buttons below."
        ),
        "viewer_paypal": (
            "💎 <b>VIEWERS · PAY WITH PAYPAL</b>\n\n"
            "1️⃣ Send your plan amount (fee-free) with the «PAY WITH PAYPAL» button. "
            "Monthly plan: <b>{viewer_paypal}</b>.\n\n"
            "2️⃣ Come back to this chat, tap <b>«I PAID: SEND RECEIPT»</b>, choose the plan and send the payment "
            "<b>screenshot</b>.\n\n"
            "3️⃣ Once we confirm your payment you'll receive your personal link to BETA HOUSE here. 🔐"
        ),
        # ---- General PayPal ----
        "paypal_hub": (
            "💳 <b>PAYPAL SUBSCRIPTION & PAYMENT CONFIRMATION</b>\n\n"
            "1️⃣ Use «GO TO PAYPAL» to pay the exact amount of the plan you chose "
            "(Creator: <b>{creator_paypal}</b> · Viewer: <b>{viewer_paypal}</b> per month).\n\n"
            "2️⃣ Tap <b>«CONFIRM PAYPAL PAYMENT»</b>, tell us whether you paid as a <b>Creator</b> or a "
            "<b>Viewer</b>, choose the plan and send your payment screenshot.\n\n"
            "3️⃣ We verify your payment and send you your access here with your role's permissions. ⏳"
        ),
        # ---- Plans and invoices ----
        "stars_invoice_desc": "{plan} plan · {product}. Access with a personal link only you can use.",
        "stars_invoice_label": "Subscription",
        "choose_plan": "⭐ <b>{product}</b>\n\nChoose your subscription plan. Pay with Telegram Stars and get instant access:",
        "choose_plan_receipt": "📸 <b>{product}</b>\n\nChoose the plan you paid (or will pay) via PayPal:",
        "choose_product_receipt": "📸 <b>CONFIRM YOUR PAYMENT</b>\n\nDid you pay as a <b>Creator</b> or as a <b>Viewer</b>?",
        "no_plans": "This plan is not available right now.",
        "plan_btn_stars": "⭐ {plan} · {price} XTR",
        "plan_btn_ext": "💳 {plan} · {price}",
        "plan_month": "1 month",
        "plan_months": "{n} months",
        "plan_lifetime": "Lifetime",
        "plan_until": "{plan} (until {date})",
        "product_unavailable": "This subscription is not available right now.",
        "receipt_instructions": (
            "📸 <b>CONFIRM YOUR PAYMENT</b>\n\n"
            "Subscription: <b>{product}</b>\nPlan: <b>{plan}</b>\nPrice: <b>{price}</b>\n\n"
            "Now send the <b>screenshot</b> of your successful payment in this chat.\n\n"
            "We'll verify your payment and send your personal access here. ⏳"
        ),
        "binance_info": "🟡 Payment ID: {value}",
        # ---- Donations ----
        "donations": (
            "⭐ <b>DONATIONS</b>\n\n"
            "If you'd like to support BETA HOUSE and the whole team that keeps this space alive, you can send a "
            "voluntary donation. Every contribution helps us keep growing! 💜\n\n"
            "👇 Choose how many ⭐ Stars you want to donate or use PayPal:"
        ),
        "btn_tip_option": "⭐ {amount}",
        "tip_invoice_title": "Donation to BETA HOUSE",
        "tip_invoice_desc": "Voluntary donation of {amount} Stars to BETA HOUSE. Thank you!",
        "tip_invoice_label": "Donation",
        "tip_invalid": "This donation is not available right now.",
        "tips_disabled": "Donations are disabled right now.",
        "tip_thanks": "💜 <b>Thank you for your donation!</b>\n\nWe received your <b>{amount} ⭐</b>. Your support keeps BETA HOUSE alive.",
        # ---- Direct contact ----
        "support_info": (
            "💬 <b>CONTACT THE CREATORS DIRECTLY</b>\n\n"
            "From now on, any message you send in this chat (text, photos, audio, videos…) goes straight to the "
            "BETA HOUSE team. We'll reply as soon as possible.\n\n"
            "👇 Write your message whenever you like."
        ),
        "support_wait": (
            "✅ We got your message. If you don't hear back in the next few hours, you can also reach us with "
            "the buttons below:"
        ),
        "support_closed_hint": "💬 To write to us directly, tap the button below.",
        "btn_open_support": "💬 CONTACT THE CREATORS",
        "support_ended": (
            "🔚 The conversation with the team has ended. Thanks for writing!\n\n"
            "If you need anything else, tap the button to open a new chat."
        ),
        # ---- Stars payments ----
        "invoice_invalid": "Invalid or outdated invoice. Please use /start again.",
        "stars_fallback": "✅ Payment received. Our team will send you your access shortly.",
        # ---- Receipts ----
        "send_image": "📸 To validate your payment send the screenshot as a <b>photo or file</b>.",
        "receipt_received": (
            "✅ <b>Receipt received!</b>\n\nWe're verifying your payment. As soon as it's confirmed you'll get "
            "your personal access link here. ⏳"
        ),
        "receipt_rejected": (
            "⚠️ We couldn't validate your payment receipt. Check that the screenshot is correct or write to us "
            "here if you have questions."
        ),
        # ---- Access ----
        "access_granted": (
            "✅ <b>PAYMENT CONFIRMED!</b>\n\n"
            "Your access to <b>{product}</b> is ready.\n🗓 Plan: <b>{plan}</b>\n\n"
            "Tap the button to join. 🔐 It's a <b>personal</b> link, valid for {ttl_min} min, and this message "
            "will <b>self-destruct</b> once you use it. When you join, the bot will enable your role's permissions."
        ),
        "btn_join": "🚪 JOIN {product}",
        "access_renewed": (
            "✅ <b>RENEWAL CONFIRMED!</b>\n\n"
            "Your subscription to <b>{product}</b> was extended.\n🗓 New expiry: <b>{date}</b>\n\n"
            "You're still in: no new link needed. 🔥"
        ),
        "access_activated": (
            "✅ <b>PAYMENT CONFIRMED!</b>\n\n"
            "Your subscription to <b>{product}</b> is active.\n🗓 Plan: <b>{plan}</b>\n\n"
            "You're already in the group, so no new link is needed: your permissions were updated. 🔥"
        ),
        "link_expired": "⌛ Your access link expired unused. Write to us here and we'll send you a new one.",
        "membership_expired": (
            "⌛ Your subscription to <b>{product}</b> has ended and your access was closed.\n\n"
            "Thanks for being part of BETA HOUSE! Renew anytime with the button below."
        ),
        "membership_reminder": (
            "⏰ Your subscription to <b>{product}</b> expires on <b>{date}</b> (in {days_left} day(s)).\n\n"
            "Renew now so you don't lose access. 🔥"
        ),
        "btn_renew": "🔄 RENEW SUBSCRIPTION",
        # ---- Roles inside the group ----
        "role_creator_on": (
            "🔥 <b>CREATOR permissions enabled!</b>\n\n"
            "You can now post your promotions in BETA HOUSE with links, text, photos and videos. "
            "Please follow the group rules."
        ),
        "role_viewer_on": (
            "🎟️ <b>Welcome to BETA HOUSE as a VIEWER!</b>\n\n"
            "Your account in the group is <b>read-only</b>: you can see every promotion, contact creators "
            "privately and join the video chats."
        ),
        "viewer_blocked": (
            "🔒 Your <b>viewer</b> subscription doesn't allow posting in BETA HOUSE (messages, media or links). "
            "If you are a creator, subscribe as a <b>Creator</b> from the bot menu."
        ),
        # ---- Errors ----
        "relay_error": "⚠️ We couldn't deliver your message. Please try again in a few minutes.",
    },
}

LANGS: tuple[str, ...] = tuple(TEXTS)
DEFAULT_LANG = "es"

# ===========================================================================
# Textos para el EQUIPO (grupo de gestión, canales de registro y /panel)
# ===========================================================================
ADMIN_TEXTS: dict[str, str] = {
    # Ficha del tema
    "contact_card": "Nombre: {mention}\nUsername: {username}\nID: <code>{user_id}</code>",
    "no_username": "sin username",
    # Nombres internos de producto y planes
    # Alerta de comprobante
    "btn_pick_product": "✅ {product}",
    "btn_pick_plan": "🗓 {plan}",
    "btn_reject": "❌ Rechazar",
    "btn_back_products": "⬅️ Volver",
    "pick_plan_toast": "Elige el plan para {product}.",
    "product_unavailable": "Ese producto no tiene chat configurado en .env.",
    "only_admins": "Solo los administradores pueden usar esta acción.",
    "payment_not_found": "Pago no encontrado.",
    "payment_already_done": "Este pago ya fue procesado.",
    "user_blocked_access": "El miembro bloqueó al bot: no se pudo entregar el acceso.",
    "link_error": "Error al generar el enlace: {error}",
    "approved_toast": "✅ Aprobado y enlace enviado.",
    "approved_suffix": "✅ <b>Aprobado</b> por {admin}: {product} · {plan} — enlace de 1 uso enviado.",
    "rejected_toast": "Pago rechazado.",
    "rejected_suffix": "❌ <b>Rechazado</b> por {admin}",
    # Comandos en temas
    "cmd_only_in_topic": "Usa este comando dentro del tema de un miembro.",
    "cmd_only_admins": "Solo los administradores pueden usar este comando.",
    "topic_unlinked": "Este tema no está vinculado a ningún miembro.",
    "cmd_approved_pending": "Pago #{payment_id} aprobado",
    "cmd_manual": "Acceso manual concedido (#{payment_id})",
    "cmd_reissued": "Enlace reemitido sin cambiar el vencimiento",
    "cmd_link_sent": "✅ {action}: {product} · {plan}. Enlace de 1 uso enviado.",
    "cmd_user_blocked": "⚠️ El miembro bloqueó al bot; no se pudo entregar el acceso.",
    "cmd_link_error": "⚠️ No se pudo generar el enlace: {error}",
    "cmd_revoked": "🗑 Enlaces anulados: {count}.",
    "cmd_nothing_to_revoke": "No hay enlaces vigentes para este miembro.",
    # Puente de soporte
    "relay_blocked": "⚠️ El miembro bloqueó al bot; no se pudo entregar el mensaje.",
    "relay_failed": "⚠️ No se pudo entregar el mensaje: {error}",
    # Accesos y membresías
    "vip_joined": "🎟 {mention} entró a <b>{product}</b>. Enlace #{grant_id} consumido y mensaje autodestruido.",
    "vip_link_stolen": (
        "🚨 El enlace #{grant_id} ({product}) de {owner} fue usado por OTRA cuenta: {intruder} "
        "(<code>{intruder_id}</code>). Revisa y expulsa si corresponde."
    ),
    "vip_link_expired": "⌛ El enlace #{grant_id} ({product}) expiró sin usarse. Usa <code>/aprobar</code> para reemitirlo.",
    "membership_expired_topic": "⌛ Terminó la suscripción a <b>{product}</b>. Miembro retirado del chat: {status}",
    "kick_ok": "sí ✅",
    "kick_skipped_other_plan": "no expulsado: mantiene otra suscripción activa en el grupo (rol ajustado)",
    "kick_failed": "no ⚠️ ({error}) — retíralo manualmente",
    # Ventas con Stars
    "stars_vip_topic": (
        "⭐ <b>Pago con Telegram Stars confirmado</b> (#{payment_id})\n"
        "{product} · {plan}\nMonto: {amount} XTR\nCharge ID: <code>{charge_id}</code>\n{status}"
    ),
    "stars_vip_delivered": "✅ Enlace de 1 uso entregado.",
    "stars_vip_failed": "⚠️ Falló la entrega: usa <code>/aprobar</code>.",
    "sale_log": (
        "#VENTA #{tag} #{method}\n💰 <b>NUEVA SUSCRIPCIÓN · {product}</b>\n\n"
        "👤 {mention}\n🆔 <code>{user_id}</code>\n🗓 {plan}\n💳 {amount}\n{status}"
    ),
    "sale_status_auto": "✅ Acceso enviado automáticamente.",
    "sale_status_failed": "⚠️ Acceso pendiente de envío manual.",
    "sale_status_approved": "✅ Aprobado por {admin}",
    # Donaciones
    "tip_topic": (
        "🎉 El miembro envió una donación de <b>{amount} XTR</b> (#T{tip_id})\n"
        "Total donado por este miembro: <b>{user_total} XTR</b>\n"
        "Charge ID: <code>{charge_id}</code>"
    ),
    "tip_log": (
        "#DONACION #STARS\n⭐ <b>NUEVA DONACIÓN</b>\n\n"
        "👤 {mention}\n🆔 <code>{user_id}</code>\n⭐ <b>{amount} XTR</b>\n"
        "📊 Donaciones acumuladas (todas): <b>{grand_total} XTR</b>"
    ),
    # Pagos inesperados
    "unknown_payment_topic": (
        "⚠️ Pago con Stars con payload desconocido <code>{payload}</code> ({amount} XTR). "
        "Charge ID: <code>{charge_id}</code>. Revisa manualmente."
    ),
    # Menú de comandos
    "cmd_desc_start": "Menú principal / Main menu",
    # ---- /panel ----
    "panel_group_title": "<b>{group}</b>\n<i>Elige qué quieres editar:</i>",
    "panel_text_lang_title": "<b>📝 {label}</b>\n<i>Elige el idioma que quieres editar:</i>",
    "flag_custom": "✏️",
    "flag_default": "⚙️",
    "btn_panel_close": "✖️ Cerrar",
    "btn_panel_back": "⬅️ Volver",
    "btn_panel_reset": "♻️ Restaurar por defecto",
    "btn_panel_reset_confirm": "⚠️ Sí, restaurar",
    "btn_panel_save": "✅ Guardar",
    "btn_panel_cancel": "❌ Cancelar",
    "panel_closed": "Panel cerrado.",
    "panel_edit_prompt": (
        "✏️ <b>Editando: {label}</b>\n<i>{help}</i>\n\n"
        "{placeholders}"
        "<b>Valor actual</b> {flag} (actualizado: {updated_at}):\n<pre>{current}</pre>\n\n"
        "👉 Envíame el <b>nuevo valor</b>. {format_hint}\n"
        "Escribe /cancelar para salir sin cambios."
    ),
    "panel_placeholders": "Variables disponibles: {items}\n\n",
    "panel_format_html": (
        "Dos formas:\n• Escríbelo con el formato de Telegram (negrita, cursiva…).\n"
        "• O toca el bloque de arriba para copiar el HTML, edítalo y pégalo tal cual.\n"
        "Si el texto es muy largo se enviará en varios mensajes automáticamente."
    ),
    "panel_format_plain": "Texto simple, sin formato.",
    "panel_format_media": "Envía una <b>foto, video, GIF o sticker</b>.",
    "panel_media_current": "{kind}",
    "panel_never": "nunca",
    "panel_updated_at": "{timestamp} UTC",
    "panel_preview_header": "👁 <b>Vista previa — {label}</b>\n━━━━━━━━━━━━━━━━━━━━",
    "panel_preview_value": "👁 <b>Vista previa — {label}</b>\n\nNuevo valor: <code>{value}</code>\n\n¿Guardar el cambio?",
    "panel_preview_confirm": "¿Guardar este cambio? Se aplicará al instante para todos los miembros.",
    "panel_saved": "✅ <b>{label}</b> guardado y aplicado.",
    "panel_reset_ask": "⚠️ ¿Restaurar <b>{label}</b> a su valor por defecto? Se perderá el valor personalizado.",
    "panel_reset_done": "♻️ <b>{label}</b> restaurado al valor por defecto.",
    "panel_reset_none": "Este ajuste ya usa el valor por defecto.",
    "panel_cancelled": "Edición cancelada. No se guardó nada.",
    "panel_session_expired": "La edición expiró (¿reinicio del bot?). Abre /panel de nuevo.",
    "panel_need_text": "Envía el nuevo valor como <b>mensaje de texto</b>.",
    "panel_need_media": "Envía una <b>foto, video, GIF o sticker</b> (no un archivo ni texto).",
    "panel_invalid": "❌ <b>Valor no válido</b>\n{error}\n\nEnvía otro valor o /cancelar.",
    "panel_audit_log": (
        "#CONFIG\n⚙️ <b>Ajuste modificado desde /panel</b>\n"
        "🔧 {label}\n↩️ Antes: <code>{old}</code>\n➡️ Ahora: <code>{new}</code>\n👤 {admin}"
    ),
    # Errores de validación del panel
    "err_empty": "El valor no puede estar vacío.",
    "err_too_long": "Demasiado largo: {length} caracteres (máximo {max}).",
    "err_braces": "Llaves {{ }} mal cerradas. Para mostrar una llave literal escribe {{{{ o }}}}.",
    "err_unknown_var": "Variable no permitida: {{{var}}}. Usa solo: {allowed}.",
    "err_html": "Telegram rechazó el formato del texto: {error}",
    "err_int": "Debe ser un número entero entre {min} y {max}.",
    "err_price_text": "Usa entre 1 y {max} caracteres, sin saltos de línea ni los símbolos < > & {{ }}.",
    "err_url": "Debe ser una URL que empiece por http:// o https:// y sin espacios (máx. {max} caracteres).",
    "err_url_or_id": "Envía una URL (https://…) o un ID de pago sin espacios (máx. {max} caracteres).",
    "err_stars_rejected": "Telegram no acepta ese precio en Stars: {error}",
    # ---- Aprobación: plan ----
    # ---- Recordatorios ----
    "membership_reminder_topic": "⏰ Recordatorio enviado: <b>{product}</b> vence el {date}.",
    # ---- Baneos ----
    "cmd_ban_usage": "Uso en el tema del miembro: <code>/ban motivo opcional</code> · <code>/unban</code>",
    "ban_no_reason": "sin motivo",
    "ban_admin_forbidden": "No se puede banear a un administrador del bot.",
    "ban_already": "Ese miembro ya está baneado.",
    "ban_not_banned": "Ese miembro no está baneado.",
    "ban_done": "🚫 <code>{user_id}</code> baneado. Enlaces revocados: {links}. Retirado de {chats} chat(s).",
    "ban_topic": "🚫 <b>Miembro baneado</b> por {admin}\nMotivo: {reason}\nEnlaces revocados: {links} · Retirado de {chats} chat(s)",
    "unban_done": "✅ <code>{user_id}</code> desbaneado. Puede volver a usar el bot (sus suscripciones canceladas no se reactivan).",
    "unban_topic": "✅ Miembro desbaneado por {admin}.",
    "ban_rejoin_blocked": "🚫 {mention} (baneado) intentó entrar a <b>{product}</b> y fue retirado.",
    # ---- /panel: secciones ----
    "panel_cat_title": "<b>{group} › {category}</b>\n<i>Elige qué quieres editar:</i>",
    "panel_admin_lang": "👥 Equipo (ES)",
    # ---- /panel: botones ----
    "pb_row_joined": "↔️ comparte fila con el anterior",
    "pb_row_own": "⬇️ fila propia",
    "btn_pb_row_join": "↔️ Unir a la fila anterior",
    "btn_pb_row_split": "⬇️ Poner en fila propia",
    "pb_state_on": "🟢 activo",
    "pb_state_off": "🔴 apagado",
    "btn_pb_toggle_on": "🟢 Encender",
    "btn_pb_toggle_off": "🔴 Apagar",
    "btn_pb_name_es": "✏️ Nombre ES",
    "btn_pb_name_en": "✏️ Nombre EN",
    "btn_pb_target": "🎯 Acción / destino",
    "btn_pb_up": "⬆️ Subir",
    "btn_pb_delete": "🗑 Eliminar",
    "btn_pb_restore": "♻️ Restaurar original",
    "pb_prompt_name": (
        "✏️ Envía el nuevo nombre <b>{lang}</b> (máx. 64 caracteres, texto simple).\n"
        "Variables: <code>{{creator_paypal}}</code> <code>{{creator_stars}}</code> <code>{{viewer_paypal}}</code> "
        "<code>{{viewer_stars}}</code>\n/cancelar para salir."
    ),
    "pb_prompt_alert": (
        "🎯 Envía el texto emergente (máx. {max} caracteres). Para un texto distinto por idioma: "
        "<code>texto ES || text EN</code>\n/cancelar para salir."
    ),
    "pb_saved": "✅ Botón actualizado.",
    "pb_deleted": "🗑 Botón eliminado.",
    "pb_restored": "♻️ Botón restaurado a su versión original.",
    "err_pb_name": "El nombre debe tener entre 1 y 64 caracteres, sin < > &.",
    "err_pb_alert": "Cada texto emergente debe tener entre 1 y {max} caracteres.",
    # ---- /panel: baneos ----
    "bans_title": "<b>🚫 Baneos</b> ({count})\n{items}\n\n<i>Pulsa un ID para desbanearlo o ➕ para banear.</i>",
    "bans_item": "• <code>{user_id}</code> {name} — {reason} · {date}",
    "bans_empty": "Nadie baneado.",
    "btn_ban_add": "➕ Banear por ID",
    "btn_unban": "✅ {user_id}",
    "err_ban_id": "ID no válido. Debe ser un número positivo.",
    # ---- /panel: clonación ----
    "clones_title": (
        "<b>🧬 Clonación autorizada</b> ({count})\n{items}\n\n"
        "Cada clon es una instancia independiente (su propio despliegue, base de datos y /panel) con toda la "
        "lógica de este bot. Aquí autorizas y registras los clones y descargas su archivo de configuración."
    ),
    "clone_item": "• {status} <b>@{username}</b> — grupo {admin_group} · {date}",
    "clones_empty": "Sin clones registrados.",
    "btn_clone_add": "➕ Autorizar nuevo clon",
    "btn_clone_open": "{status} @{username}",
    "clone_status_active": "🟢",
    "clone_status_revoked": "⛔",
    "clone_step": "🧬 <b>Nuevo clon · paso {step}/{total}</b>\n{prompt}\n\n/cancelar para salir.",
    "clone_prompt_token": "Envía el <b>BOT_TOKEN</b> del nuevo bot (créalo en @BotFather).",
    "clone_prompt_admin_group": "Envía el ID del <b>grupo de gestión con Temas</b> del clon (empieza por -100).",
    "clone_prompt_vip": "Envía el ID del <b>grupo BETA HOUSE</b> del clon (empieza por -100).",
    "clone_prompt_sales": "Envía el ID del <b>canal de ventas</b> del clon (-100…).",
    "clone_prompt_tips": "Envía el ID del <b>canal de donaciones</b> del clon (-100…), o <code>0</code> para desactivar donaciones.",
    "clone_prompt_admins": "Envía los <b>IDs de los administradores</b> del clon separados por coma.",
    "clone_summary": (
        "🧬 <b>Resumen del clon</b>\nBot: @{username}\nToken: <code>{token}</code>\nGrupo de gestión: <code>{admin_group}</code>\n"
        "VIP: <code>{vip}</code>\nVentas: <code>{sales}</code>\nDonaciones: <code>{tips}</code>\nAdmins: <code>{admins}</code>"
    ),
    "btn_clone_save": "✅ Autorizar y guardar",
    "clone_saved": "✅ Clon @{username} autorizado. Te envío su archivo <code>.env</code> para desplegarlo.",
    "clone_detail": (
        "🧬 <b>@{username}</b> · {status}\nToken: <code>{token}</code>\nGrupo de gestión: <code>{admin_group}</code>\n"
        "VIP: <code>{vip}</code> · Ventas: <code>{sales}</code> · Donaciones: <code>{tips}</code>\n"
        "Admins: <code>{admins}</code>\nAutorizado: {date}"
    ),
    "btn_clone_env": "📄 Descargar .env",
    "btn_clone_revoke": "⛔ Revocar autorización",
    "btn_clone_activate": "🟢 Reactivar",
    "btn_clone_delete": "🗑 Eliminar registro",
    "clone_env_caption": (
        "📄 Configuración del clon @{username}. Despliégalo como un servicio nuevo con este código y un Volume propio. "
        "Contiene el token: no lo reenvíes."
    ),
    "clone_revoked": "⛔ Autorización revocada. Detén el servicio del clon para que deje de funcionar.",
    "clone_activated": "🟢 Clon reactivado.",
    "clone_deleted": "🗑 Registro del clon eliminado.",
    "err_clone_token": "Telegram rechazó ese token: {error}",
    "err_clone_token_self": "Ese es el token de ESTE bot. El clon necesita su propio bot.",
    "err_clone_token_dup": "Ese bot ya está registrado como clon.",
    "err_clone_chat": "ID no válido: debe empezar por -100.",
    "err_clone_admins": "Envía uno o más IDs numéricos positivos separados por coma.",
    # ===== Ficha del Tema, planes y aprobación =====
    "topic_header": (
        "👤 <b>Nuevo contacto</b>\n{card}\n\n"
        "Responde en este Tema y el bot enviará tu mensaje al miembro.\n"
        "• Mensaje que empiece con <code>#</code> = nota interna (no se envía).\n"
        "• <code>/aprobar [producto] [meses]</code> = acceso manual (productos: {products}, o creador / espectador; meses 1, 3, 6, 12 o 0 = de por vida).\n"
        "• <code>/rol</code> = reaplica sus permisos en el grupo (🔥 creador publica · 🎟️ espectador solo lectura).\n"
        "• <code>/revocar</code> = anula el enlace de acceso vigente.\n"
        "• <code>/cerrar</code> = finaliza la conversación.\n"
        "• <code>/ban motivo</code> · <code>/unban</code> = vetar / levantar el veto."
    ),
    "support_opened": "💬 El miembro abrió un chat de soporte.",
    "btn_support_end": "🔚 Finalizar conversación",
    "support_end_done": "🔚 Conversación finalizada por {admin}. El miembro deberá pulsar el botón de soporte para volver a escribir.",
    "support_end_already": "La conversación ya estaba finalizada.",
    "plan_month": "1 mes",
    "plan_months": "{n} meses",
    "plan_lifetime": "♾ De por vida",
    "receipt_alert": (
        "🚨 <b>COMPROBANTE DE PAGO — PENDIENTE</b>\n\n{card}\n\n"
        "🛒 Producto: <b>{product}</b>\n🗓 Plan: <b>{plan}</b>\n💳 Precio: <b>{price}</b>\n"
        "🧾 Pago #{payment_id} — verifica el pago antes de aprobar."
    ),
    "receipt_unknown": "— (elige al aprobar)",
    "btn_approve_plan": "✅ Aprobar · {product} · {plan}",
    "btn_change_plan": "🔀 Cambiar canal / plan",
    "cmd_approve_usage": (
        "Uso: <code>/aprobar [producto] [meses]</code> (p. ej. <code>/aprobar creador 3</code>)\n"
        "Productos activos: {products} · alias: creador, espectador\n"
        "Meses: 1, 3, 6, 12 o 0 (de por vida)"
    ),
    "join_request_declined": (
        "🚫 {intruder} (<code>{intruder_id}</code>) intentó entrar a <b>{product}</b> con el enlace de {owner}. "
        "Solicitud rechazada."
    ),
    "access_renewed_topic": "🔄 Renovación aplicada: <b>{product}</b> ahora vence el {date}.",
    "btn_reissue": "🔁 Reemitir enlace",
    "reissued": "🔁 Enlace reemitido.",
    # ===== Vinculación de grupos y canales =====
    "chat_added_dm": (
        "🔗 <b>Me agregaron como administrador</b>\n«{title}» ({kind}) · <code>{chat_id}</code>\n\n"
        "Invitar usuarios: {invite} · Expulsar miembros: {restrict}\n\n"
        "¿Quieres que gestione suscripciones aquí?"
    ),
    "chat_removed_dm": "⚠️ Ya no soy administrador en «{title}» (<code>{chat_id}</code>). {note}",
    "chat_removed_product_note": "El producto <b>{product}</b> quedó pausado.",
    "chat_kind_channel": "canal",
    "chat_kind_group": "grupo",
    "perm_yes": "✅",
    "perm_no": "❌",
    "btn_link_chat": "✅ Vincular como producto",
    "btn_ignore": "Ignorar",
    "chat_linked": "✅ «{title}» vinculado como producto <code>{key}</code>. Configura sus planes y precios.",
    "chat_already_linked": "Ese chat ya está vinculado como producto <code>{key}</code>.",
    "chat_missing_perms": (
        "⚠️ Faltan permisos: dame «Invitar usuarios» y «Bloquear/expulsar usuarios» (restringir) en «{title}»."
    ),
    # ===== /panel: inicio y módulos =====
    "panel_home": (
        "<pre>▓▒░ PANEL DE CONTROL ░▒▓</pre>\n"
        "🛒 Productos activos: <b>{products}</b>\n"
        "⭐ Rangos de donación: <b>{tips}</b>\n"
        "📣 Anuncios programados: <b>{posts}</b>\n"
        "🚫 Baneados: <b>{banned}</b> · 🧬 Clones: <b>{clones}</b>\n"
        "💬 Chat de soporte: <b>{support_mode}</b>\n\n"
        "<i>Elige un módulo:</i>"
    ),
    "panel_group_welcome": "👋 Bienvenida y anuncios",
    "panel_group_products": "🛒 Suscripciones y productos",
    "panel_group_links": "🔗 Enlaces",
    "panel_group_buttons": "🔘 Botones",
    "panel_group_texts": "📝 Textos y multimedia",
    "panel_group_donations": "⭐ Donaciones",
    "panel_group_bans": "🚫 Baneos",
    "panel_group_support": "💬 Contactos y SUPPORT",
    "panel_group_clones": "🧬 Clonación",
    "panel_none": "ninguno",
    "panel_welcome_title": (
        "<b>👋 Bienvenida y anuncios</b>\n\nMultimedia de bienvenida: <b>{media}</b>\n"
        "<i>Edita el mensaje de bienvenida (texto, multimedia y botones) o crea anuncios para tus usuarios y chats.</i>"
    ),
    "btn_welcome_text": "✏️ Texto de bienvenida",
    "btn_welcome_media": "🖼 Multimedia de bienvenida",
    "btn_welcome_buttons": "🔘 Botones de bienvenida",
    "btn_language_texts": "🌐 Selector de idioma",
    "btn_posts": "📣 Anuncios",
    "btn_donation_posts": "📣 Publicaciones de donación",
    # ===== /panel: textos y multimedia =====
    "panel_texts_title": "<b>📝 Textos y multimedia</b>\n<i>Elige qué quieres editar:</i>",
    "btn_texts_menus": "📄 Secciones (texto + imagen de cada botón)",
    "btn_texts_auto": "🤖 Mensajes automáticos y de pago",
    "btn_texts_team": "👥 Mensajes del grupo de gestión",
    "panel_menus_title": "<b>📄 Secciones</b>\nCada sección es el mensaje que se abre al pulsar un botón.\n<i>Elige una:</i>",
    "menu_detail": (
        "<b>📄 {title}</b> · <code>{menu_id}</code>\nAbre desde: {parent}\nMultimedia: <b>{media}</b>\n"
        "Botones: <b>{buttons}</b>\n\n<b>Texto ES</b>:\n<pre>{text_es}</pre>"
    ),
    "btn_menu_text_es": "✏️ Texto ES",
    "btn_menu_text_en": "✏️ Texto EN",
    "btn_menu_media_set": "🖼 Poner imagen / video",
    "btn_menu_media_del": "🗑 Quitar multimedia",
    "btn_menu_buttons": "🔘 Botones de esta sección",
    "btn_menu_rename": "🏷 Renombrar sección",
    "btn_menu_delete": "🗑 Eliminar sección",
    "btn_menu_new": "➕ Nueva sección",
    "menu_prompt_text": (
        "✏️ Envía el texto <b>{lang}</b> de la sección «{title}». Puedes usar el formato de Telegram o pegar HTML.\n"
        "Variables: {variables}\n/cancelar para salir."
    ),
    "menu_prompt_media": "🖼 Envía la foto, video, GIF o sticker para «{title}».\n/cancelar para salir.",
    "menu_prompt_new": "➕ Envía el nombre de la nueva sección (se usa en el panel y como texto inicial).\n/cancelar para salir.",
    "menu_prompt_rename": "🏷 Envía el nuevo nombre de la sección.\n/cancelar para salir.",
    "menu_created": "✅ Sección «{title}» creada. Edita su texto, multimedia y botones, y ábrela desde un botón con la acción «Abrir sección».",
    "menu_deleted": "🗑 Sección eliminada. Los botones que la abrían quedaron apagados.",
    "menu_saved": "✅ Sección actualizada.",
    "menu_media_removed": "🗑 Multimedia quitada.",
    "menu_system": "sección del sistema",
    "menu_root": "inicio",
    "err_menu_name": "El nombre debe tener entre 1 y 40 caracteres, sin < > &.",
    # ===== /panel: botones =====
    "panel_buttons_title": "<b>🔘 Botones</b>\n<i>Elige dónde están los botones que quieres cambiar:</i>",
    "btn_buttons_menus": "📄 Botones de las secciones (miembros)",
    "btn_buttons_system": "⚙️ Botones del sistema (volver, idioma, acceso…)",
    "btn_buttons_team": "👥 Botones del grupo de gestión",
    "btn_buttons_panel": "🛠 Botones de este panel",
    "pb_menus_title": "<b>🔘 Secciones</b>\nHasta {max} botones por sección. El botón VOLVER se añade solo.\n<i>Elige una:</i>",
    "pb_menu_title": "<b>🔘 {menu}</b>\n{count}/{max} botones · 🟢 activo · 🔴 apagado\n<i>Elige un botón para editarlo:</i>",
    "pb_detail": (
        "<b>🔘 Botón</b> <code>{menu}/{button_id}</code>\n"
        "🇪🇸 {name_es}\n🇺🇸 {name_en}\n"
        "Estado: {state}\nFila: {row}\nAcción: {action}\nDestino: {target}"
    ),
    "action_menu": "📄 Abrir sección (texto, multimedia y más botones)",
    "action_url": "🔗 Abrir enlace",
    "action_alert": "💬 Ventana emergente",
    "action_subscribe": "⭐ Cobrar suscripción en Stars (da acceso)",
    "action_receipt": "📸 Confirmar pago externo (PayPal y otros)",
    "action_tip": "☕ Donación en Stars",
    "action_support": "💬 Abrir chat de soporte (DM)",
    "action_back": "⬅️ Volver",
    "action_home": "🏠 Menú principal",
    "action_lang": "🌐 Cambiar idioma",
    "action_header": "📌 Encabezado / Divisor (solo texto)",
    "btn_pb_add": "➕ Añadir botón",
    "btn_pb_change_action": "🔄 Cambiar acción",
    "pb_add_choose": "¿Qué debe hacer el nuevo botón de <b>{menu}</b>?",
    "pb_choose_action": "Elige la nueva acción del botón:",
    "pb_choose_menu": "¿Qué sección debe abrir?",
    "pb_choose_link": "¿Qué enlace debe abrir? (o escribe una URL nueva)",
    "pb_choose_product": "¿De qué producto?",
    "pb_any_product": "🛒 Que el miembro elija",
    "btn_pb_type_url": "✏️ Escribir URL",
    "pb_prompt_url": "🎯 Envía la URL (<code>https://…</code> o <code>tg://…</code>).\n/cancelar para salir.",
    "pb_prompt_tip": "☕ Envía la cantidad de Stars de esta donación (1–{max}).\n/cancelar para salir.",
    "pb_added": "✅ Botón añadido (apagado). Revisa sus nombres y enciéndelo.",
    "pb_target_none": "—",
    "pb_target_any": "el miembro elige",
    "err_pb_url": "URL no válida (debe empezar por https://, http:// o tg://).",
    "err_pb_tip": "Debe ser un número entre 1 y {max}.",
    "pb_full": "Esta sección ya tiene {max} botones.",
    # ===== /panel: suscripciones y productos =====
    "panel_products_title": (
        "<b>🛒 Suscripciones y productos</b>\n{items}\n\n"
        "Acceso: <b>{mode}</b> · Validez del enlace: <b>{ttl}</b> · Recordatorio: <b>{reminder} días antes</b>\n\n"
        "<i>Para vincular un grupo o canal pulsa «Vincular» y elige el chat: el bot entra como admin con los "
        "permisos necesarios y te avisará por aquí.</i>"
    ),
    "product_item": "• {status} <b>{name}</b> <code>{key}</code> — {plans}",
    "products_empty": "Ningún producto vinculado todavía.",
    "product_no_plans": "sin planes activos",
    "btn_link_group": "➕ Vincular grupo",
    "btn_link_channel": "➕ Vincular canal",
    "btn_detected_chats": "📡 Chats detectados",
    "btn_sub_settings": "⚙️ Ajustes de suscripción",
    "detected_title": "<b>📡 Chats donde el bot es admin</b>\n{items}\n<i>Pulsa uno para vincularlo como producto.</i>",
    "detected_item": "• {title} · <code>{chat_id}</code> · invitar {invite} · expulsar {restrict}{linked}",
    "detected_linked": " · productos: <code>{key}</code>",
    "btn_link_more": "➕ Otra tarifa/rol en {title}",
    "detected_empty": "No he detectado chats. Usa «Vincular grupo/canal» o agrégame como admin manualmente.",
    "product_detail": (
        "<b>🛒 {name_es}</b> <code>{key}</code>\n🇺🇸 {name_en}\nChat: <code>{chat_id}</code> ({kind})\n"
        "Estado: {status} · Permisos: invitar {invite} · expulsar {restrict}\n"
        "🎭 Rol en el grupo: <b>{role}</b>\n"
        "Miembros con suscripción activa: <b>{members}</b>\n\n<b>Planes</b>\n{plans}"
    ),
    "plan_line": "{status} {plan}: ⭐ {stars} · 💳 {ext}",
    "plan_off": "—",
    "product_status_on": "🟢 activo",
    "product_status_off": "🔴 pausado",
    "btn_product_toggle_on": "🟢 Activar ventas",
    "btn_product_toggle_off": "⏸ Pausar ventas",
    "btn_product_rename_es": "🏷 Nombre ES",
    "btn_product_rename_en": "🏷 Nombre EN",
    "btn_product_check": "🔍 Verificar permisos",
    "btn_product_unlink": "🗑 Desvincular",
    "btn_product_role": "🎭 Cambiar rol",
    "btn_sync_roles": "🔄 Sincronizar permisos",
    "role_creator": "🔥 Creador (publica texto, multimedia y enlaces)",
    "role_viewer": "🎟️ Espectador (solo lectura)",
    "role_none": "— Sin gestión de permisos",
    "role_tag_creator": "🔥 CREADOR",
    "role_tag_viewer": "🎟️ ESPECTADOR",
    "role_tag_none": "MIEMBRO",
    "roles_synced": "🔄 Permisos sincronizados: {ok} correctos · {fail} con error · {skipped} omitidos (admins / fuera del grupo).",
    "role_applied_topic": "🎭 Rol aplicado en <b>{chat}</b>: <b>{role}</b>.",
    "role_apply_failed_topic": "⚠️ No pude aplicar el rol <b>{role}</b> en <b>{chat}</b>: {error}",
    "role_changed_topic": "🎭 Cambio de rol en <b>{chat}</b>: {old} → <b>{new}</b>.",
    "chat_defaults_block": (
        "⚠️ Los permisos generales del grupo «{title}» no permiten enviar {missing}. Los creadores tampoco "
        "podrán hacerlo: actívalos en Ajustes del grupo → Permisos (el bot restringe a los espectadores uno a uno)."
    ),
    "chat_missing_delete": "⚠️ Falta el permiso «Eliminar mensajes» en «{title}»: no podré borrar publicaciones de espectadores.",
    "perm_messages": "mensajes",
    "perm_media": "multimedia",
    "perm_links": "vistas previas de enlaces",
    "cmd_role_result": "🎭 Rol de {mention} en {chat}: <b>{role}</b> ({result})",
    "cmd_role_none": "Este miembro no tiene suscripciones activas en chats con roles.",
    "role_result_ok": "aplicado",
    "role_result_skip": "omitido: admin o fuera del grupo",
    "viewer_mode_readonly": "solo lectura",
    "viewer_mode_text": "solo texto (sin multimedia ni enlaces)",
    "btn_viewer_mode": "🎟️ Permisos del espectador",
    "btn_plan": "{status} {plan}",
    "plan_detail": (
        "<b>🗓 {product} · {plan}</b>\nEstado: {status}\n⭐ Precio en Stars: <b>{stars}</b>\n"
        "💳 Precio PayPal/otros (texto): <b>{ext}</b>\n\n<i>Stars 0 = no se vende con Stars. "
        "Sin precio externo = no se ofrece por PayPal/otros.</i>"
    ),
    "btn_plan_stars": "⭐ Precio en Stars",
    "btn_plan_ext": "💳 Precio PayPal/otros",
    "btn_plan_toggle_on": "🟢 Activar plan",
    "btn_plan_toggle_off": "🔴 Desactivar plan",
    "plan_prompt_stars": "⭐ Envía el precio en Stars (0 = no vender con Stars, máx. {max}).\n/cancelar para salir.",
    "plan_prompt_ext": "💳 Envía el precio para PayPal/otros, p. ej. <code>7 $</code>, o <code>0</code> para no ofrecerlo.\n/cancelar para salir.",
    "product_prompt_name": "🏷 Envía el nombre {lang} del producto (máx. 64).\n/cancelar para salir.",
    "product_saved": "✅ Producto actualizado.",
    "product_unlinked": "🗑 Producto desvinculado. Las suscripciones activas siguen hasta su vencimiento, pero ya no se vende.",
    "product_unlink_ask": "⚠️ ¿Desvincular <b>{name}</b>? Dejará de venderse.",
    "product_check_ok": "✅ Permisos correctos en «{title}».",
    "product_check_fail": "⚠️ «{title}»: {error}",
    "link_add_hint": (
        "➕ <b>Vincular {kind}</b>\n\n1. Pulsa el botón de abajo y elige el {kind}.\n"
        "2. Acepta los permisos de administrador que propone Telegram (invitar usuarios, expulsar/restringir "
        "miembros y eliminar mensajes).\n"
        "3. Te avisaré por aquí en cuanto me agreguen para que lo vincules como producto."
    ),
    "btn_open_add_link": "🔗 Elegir {kind}",
    "sub_settings_title": (
        "<b>⚙️ Ajustes de suscripción</b>\nAcceso: <b>{mode}</b>\nValidez del enlace: <b>{ttl}</b>\n"
        "Recordatorio: <b>{reminder} días antes</b> (mínimo 3)\n"
        "Espectadores en el grupo: <b>{viewer_mode}</b>\n\n"
        "<i>Solicitud de ingreso: el bot aprueba solo al miembro que pagó (recomendado). "
        "Enlace de 1 uso: entra quien lo use primero.</i>"
    ),
    "access_mode_request": "solicitud de ingreso",
    "access_mode_link": "enlace de 1 uso",
    "btn_access_mode": "🔐 Cambiar modo de acceso",
    "btn_reminder_text": "✏️ Texto del recordatorio",
    "btn_expired_text": "✏️ Texto de vencimiento",
    "btn_access_text": "✏️ Texto de acceso aprobado",
    "ttl_minutes": "{n} min",
    "ttl_hours": "{n} h",
    # ===== /panel: enlaces =====
    "panel_links_title": "<b>🔗 Enlaces</b>\n<i>Elige una categoría o crea enlaces nuevos:</i>",
    "btn_custom_links": "🧩 Enlaces personalizados",
    "custom_links_title": "<b>🧩 Enlaces personalizados</b>\n{items}\n<i>Úsalos en cualquier botón con la acción «Abrir enlace».</i>",
    "custom_link_item": "• <b>{label}</b> <code>{key}</code>",
    "btn_custom_link_add": "➕ Nuevo enlace",
    "custom_link_prompt": "➕ Envía <code>Nombre | URL</code>, p. ej. <code>Mi Instagram | https://instagram.com/…</code>\n/cancelar para salir.",
    "custom_link_url_prompt": "✏️ Envía la nueva URL de «{label}».\n/cancelar para salir.",
    "custom_link_detail": "<b>🔗 {label}</b> <code>{key}</code>\n{url}\n\nUsado en: {usage}",
    "btn_custom_link_edit": "✏️ Cambiar URL",
    "btn_custom_link_delete": "🗑 Eliminar",
    "custom_link_saved": "✅ Enlace guardado.",
    "custom_link_deleted": "🗑 Enlace eliminado. Los botones que lo usaban quedan ocultos hasta que les pongas otro.",
    "err_custom_link": "Formato no válido. Usa <code>Nombre | https://…</code> (nombre de 1 a 40 caracteres).",
    "link_usage_none": "ningún botón",
    # ===== /panel: donaciones =====
    "panel_donations_title": (
        "<b>⭐ Donaciones</b>\nRangos en la sección de donaciones: <b>{tips}</b>\n"
        "Botones de donación en todo el bot: <b>{count}</b>\nEnlace directo a la sección: {deeplink}"
    ),
    "btn_tip_ranges": "⭐ Rangos de donación",
    "btn_tip_everywhere": "🔎 Dónde hay botones de donación",
    "btn_tip_texts": "✏️ Textos de donación y agradecimiento",
    "tip_ranges_title": "<b>⭐ Rangos de donación</b>\n<i>Pulsa un rango para cambiar su monto, apagarlo o eliminarlo.</i>",
    "btn_tip_add": "➕ Añadir rango",
    "tip_everywhere_title": "<b>🔎 Botones de donación</b>\n{items}",
    "tip_everywhere_item": "• {menu}: <b>{amount} ⭐</b> {state}",
    "tip_prompt_amount": "☕ Envía la cantidad de Stars (1–{max}).\n/cancelar para salir.",
    "tip_added": "✅ Rango añadido y activado.",
    # ===== /panel: anuncios y publicaciones =====
    "posts_title": "<b>📣 {kind}</b>\n{items}",
    "posts_kind_announce": "Anuncios",
    "posts_kind_donation": "Publicaciones de donación",
    "post_item": "• #{id} {status} → {target} · {when}",
    "posts_empty": "Sin publicaciones.",
    "btn_post_new": "➕ Nueva publicación",
    "btn_post_open": "{status} #{id}",
    "post_status_draft": "📝",
    "post_status_scheduled": "⏰",
    "post_status_sending": "📤",
    "post_status_sent": "✅",
    "post_status_cancelled": "⛔",
    "post_step_content": (
        "📣 <b>Nueva publicación · 1/4</b>\nEnvía el contenido: texto, o foto/video/GIF con texto (pie de foto).\n"
        "/cancelar para salir."
    ),
    "post_step_buttons": (
        "📣 <b>2/4 · Botones</b>\nEnvía un botón por línea con el formato <code>Texto | destino</code>.\n"
        "Destinos: una URL · <code>link:clave</code> · <code>menu:id</code> · <code>sub:producto</code> · "
        "<code>pay:producto</code> · <code>tip:monto</code> · <code>support</code>\n"
        "Envía <code>0</code> si no quieres botones{auto_hint}."
    ),
    "post_auto_tips_hint": " o <code>auto</code> para añadir un botón por cada rango de donación",
    "post_step_target": "📣 <b>3/4 · ¿A quién se envía?</b>",
    "post_step_when": (
        "📣 <b>4/4 · ¿Cuándo?</b>\nEnvía <code>ahora</code> o una fecha <code>AAAA-MM-DD HH:MM</code> ({tz}).\n"
        "Para repetir añade <code>cada N</code> horas, p. ej. <code>2026-11-05 20:00 cada 24</code>."
    ),
    "post_target_users": "👥 Usuarios del bot ({count})",
    "post_target_chats": "📢 Todos los grupos y canales vinculados",
    "post_target_chat": "📢 {title}",
    "post_target_me": "🧪 Solo a mí (prueba)",
    "post_target_label_users": "usuarios",
    "post_target_label_chats": "chats vinculados",
    "post_target_label_me": "prueba",
    "post_when_now": "ahora",
    "post_repeat": " · cada {hours} h",
    "post_saved": "✅ Publicación #{id} {state}.",
    "post_state_now": "enviándose ahora",
    "post_state_scheduled": "programada para {when}",
    "post_detail": "<b>📣 Publicación #{id}</b> {status}\nDestino: {target}\nCuándo: {when}\nEnviados: {sent} · Fallidos: {failed}",
    "btn_post_send_now": "▶️ Enviar ahora",
    "btn_post_preview": "👁 Vista previa",
    "btn_post_cancel": "⛔ Cancelar",
    "btn_post_delete": "🗑 Eliminar",
    "post_done_dm": "📣 Publicación #{id} enviada: {sent} entregados, {failed} fallidos.",
    "err_post_buttons": "Línea {line} no válida: «{text}». Revisa el formato <code>Texto | destino</code>.",
    "err_post_when": "Fecha no válida. Usa <code>ahora</code> o <code>AAAA-MM-DD HH:MM</code> (en el futuro).",
    "err_post_content": "Envía texto o una foto/video/GIF (con o sin pie de foto).",
    # ===== /panel: soporte =====
    "panel_support_title": (
        "<b>💬 Contactos y SUPPORT</b>\nModo del chat: <b>{mode}</b>\n\n"
        "<i>Solo con botón: el miembro pulsa «SUPPORT / DM» para escribirte y puedes finalizar la conversación "
        "desde el grupo de gestión. Siempre abierto: todo lo que escriba te llega.</i>"
    ),
    "support_mode_button": "solo con botón",
    "support_mode_always": "siempre abierto",
    "btn_support_mode": "🔄 Cambiar modo del chat",
    "btn_support_links": "🔗 Enlaces de contacto",
    "btn_support_menu": "🔘 Botones de SUPPORT",
    "btn_support_wait": "🔘 Botones de la respuesta automática",
    "btn_receipt_wait": "🔘 Botones de espera de pago",
    "btn_support_texts": "✏️ Textos de soporte",
    "cmd_desc_panel": "Panel de control (admins)",
    "ban_prompt": "Envía <code>ID motivo opcional</code> o <code>@usuario motivo opcional</code>.\n/cancelar para salir.",
    "err_ban_username": "No encontré a @{username}. Solo puedo vetar por @usuario a quien ya escribió al bot; usa su ID.",
}

# ===========================================================================
# SECCIONES (menús) Y BOTONES — valores de fábrica
# ---------------------------------------------------------------------------
# Cada sección es un mensaje (texto ES/EN + multimedia opcional + botones). Las del sistema toman su texto de
# TEXTS (editable en el panel); las creadas por el admin guardan su texto en la tabla ui_menus.
# Acciones de botón (destino entre paréntesis):
#   menu (id de sección) · url (URL o link:clave) · alert (texto, "ES || EN") · subscribe (producto)
#   receipt (producto o vacío = el miembro elige) · tip (Stars) · support · back · home · lang
# ===========================================================================
SYSTEM_MENUS: dict[str, tuple[str, str | None, str]] = {
    # id: (clave de texto, sección padre, nombre en el panel)
    "main": ("welcome", None, "🏠 Bienvenida (Menú Principal)"),
    "creator": ("creator_info", "main", "🔥 Suscripción Creadores"),
    "creator_paypal": ("creator_paypal", "creator", "💎 Creadores · PayPal"),
    "viewer": ("viewer_info", "main", "🎟️ Suscripción Espectadores"),
    "viewer_paypal": ("viewer_paypal", "viewer", "💎 Espectadores · PayPal"),
    "paypal_hub": ("paypal_hub", "main", "💳 PayPal y Confirmación"),
    "donate": ("donations", "main", "⭐ Donaciones"),
    "support": ("support_info", "main", "💬 Contacto Directo Creadores"),
    "support_wait": ("support_wait", None, "⏳ Respuesta automática de soporte"),
    "receipt_wait": ("receipt_received", None, "⏳ Espera de confirmación de pago"),
}
DONATE_MENU = "donate"
SUPPORT_MENU = "support"
BUTTON_ACTIONS: tuple[str, ...] = (
    "menu", "url", "alert", "header", "subscribe", "receipt", "tip", "support", "back", "home", "lang",
)
ACTIONS_WITH_TARGET = {"menu", "url", "alert", "subscribe", "receipt", "tip"}
ALWAYS_AVAILABLE_ACTIONS = frozenset({"header", "support", "back", "home", "lang"})
LEGACY_ACTIONS = {"nav": "menu", "stars": "subscribe"}
CUSTOM_BUTTON_PREFIX = "c"
CUSTOM_MENU_PREFIX = "m"
CUSTOM_LINK_PREFIX = "x_"
NEW_BUTTON_NAMES = ("🆕 Nuevo botón", "🆕 New button")
BUTTONS_PER_ROW_MAX = 3

# Botones que comparten fila con el anterior.
DEFAULT_PAIRED_BUTTONS: frozenset[tuple[str, str]] = frozenset({
    ("main", "sub_viewer"),       # «Suscríbete como Creador» y «Suscríbete como Espectador» en la MISMA fila (fila 2)
    ("support", "x"),
    ("support_wait", "x"),
    ("receipt_wait", "x"),
})


def _tip_buttons(amounts: tuple[int, ...] = DEFAULT_TIP_AMOUNTS) -> list[tuple[str, str, str, str, str, int]]:
    return [(f"t{a}", "⭐ {amount}", "⭐ {amount}", "tip", str(a), 1) for a in amounts]


def tip_button_pairs(amounts: tuple[int, ...] = DEFAULT_TIP_AMOUNTS) -> set[tuple[str, str]]:
    """Rangos de donación de fábrica en filas de 3."""
    return {("donate", f"t{a}") for i, a in enumerate(amounts) if i % 3}


def default_buttons(tip_amounts: tuple[int, ...] | None = None) -> dict[str, list[tuple[str, str, str, str, str, int]]]:
    """Botones de fábrica; `tip_amounts` sustituye los rangos de donación (migración de la versión anterior)."""
    if not tip_amounts:
        return DEFAULT_BUTTONS
    buttons = dict(DEFAULT_BUTTONS)
    buttons[DONATE_MENU] = _tip_buttons(tip_amounts) + [b for b in DEFAULT_BUTTONS[DONATE_MENU] if b[3] != "tip"]
    return buttons


_CONTACT_BUTTONS = [
    ("telegram", "✈️ Telegram de los creadores", "✈️ Creators on Telegram", "url", "link:contact_telegram_link", 1),
    ("x", "𝕏 Creadores en «X»", "𝕏 Creators on «X»", "url", "link:contact_x_link", 1),
]

# (button_id, nombre ES, nombre EN, acción, destino, activo)
DEFAULT_BUTTONS: dict[str, list[tuple[str, str, str, str, str, int]]] = {
    "main": [
        ("sub_header", "👇 SUSCRÍBETE EN LOS SIGUIENTES BOTONES 👇", "👇 SUBSCRIBE USING THE BUTTONS BELOW 👇",
         "header", "", 1),
        ("sub_creator", "🔥 SUSCRÍBETE COMO CREADOR", "🔥 SUBSCRIBE AS CREATOR", "menu", "creator", 1),
        ("sub_viewer", "🎟️ SUSCRÍBETE COMO ESPECTADOR", "🎟️ SUBSCRIBE AS VIEWER", "menu", "viewer", 1),
        ("donate", "⭐ DONACIONES", "⭐ DONATIONS", "menu", "donate", 1),
        ("paypal_main", "💎 SUSCRIPCIÓN CON PAYPAL Y CONFIRMACIÓN", "💎 PAYPAL SUBSCRIPTION & CONFIRMATION",
         "menu", "paypal_hub", 1),
        ("support", "💬 CONTACTA DIRECTAMENTE CON LOS CREADORES", "💬 CONTACT CREATORS DIRECTLY", "support", "", 1),
        ("lang", "🌐 Idioma / Language", "🌐 Language / Idioma", "lang", "", 1),
    ],
    "creator": [
        ("c_stars", "⭐ SUSCRÍBETE CON TELEGRAM STARS", "⭐ SUBSCRIBE WITH TELEGRAM STARS", "subscribe", "creator", 1),
        ("c_paypal", "💎 SUSCRÍBETE CON PAYPAL", "💎 SUBSCRIBE WITH PAYPAL", "menu", "creator_paypal", 1),
        ("c_confirm", "📸 CONFIRMAR PAGO PAYPAL", "📸 CONFIRM PAYPAL PAYMENT", "receipt", "creator", 1),
        ("c_rules", "📜 REGLAS DE BETA HOUSE", "📜 BETA HOUSE RULES", "url", "link:rules_link", 1),
    ],
    "creator_paypal": [
        ("cp_pay", "💎 PAGAR CON PAYPAL ({creator_paypal} / MES)", "💎 PAY WITH PAYPAL ({creator_paypal} / MONTH)",
         "url", "link:paypal_creator_link", 1),
        ("cp_confirm", "📸 YA PAGUÉ: ENVIAR COMPROBANTE", "📸 I PAID: SEND RECEIPT", "receipt", "creator", 1),
    ],
    "viewer": [
        ("v_stars", "⭐ SUSCRÍBETE CON TELEGRAM STARS", "⭐ SUBSCRIBE WITH TELEGRAM STARS", "subscribe", "viewer", 1),
        ("v_paypal", "💎 SUSCRÍBETE CON PAYPAL", "💎 SUBSCRIBE WITH PAYPAL", "menu", "viewer_paypal", 1),
        ("v_confirm", "📸 CONFIRMAR PAGO PAYPAL", "📸 CONFIRM PAYPAL PAYMENT", "receipt", "viewer", 1),
        ("v_rules", "📜 REGLAS DE BETA HOUSE", "📜 BETA HOUSE RULES", "url", "link:rules_link", 1),
    ],
    "viewer_paypal": [
        ("vp_pay", "💎 PAGAR CON PAYPAL ({viewer_paypal} / MES)", "💎 PAY WITH PAYPAL ({viewer_paypal} / MONTH)",
         "url", "link:paypal_viewer_link", 1),
        ("vp_confirm", "📸 YA PAGUÉ: ENVIAR COMPROBANTE", "📸 I PAID: SEND RECEIPT", "receipt", "viewer", 1),
    ],
    "paypal_hub": [
        ("ph_pay", "💎 IR A PAYPAL", "💎 GO TO PAYPAL", "url", "link:paypal_link", 1),
        # destino vacío: el miembro elige si pagó como Creador o Espectador y luego el plan
        ("ph_confirm", "📸 CONFIRMAR PAGO PAYPAL (CREADOR / ESPECTADOR)", "📸 CONFIRM PAYPAL PAYMENT (CREATOR / VIEWER)",
         "receipt", "", 1),
    ],
    "donate": [
        *_tip_buttons(),
        ("paypal", "💎 DONAR CON PAYPAL", "💎 DONATE WITH PAYPAL", "url", "link:paypal_donate_link", 1),
    ],
    "support": list(_CONTACT_BUTTONS),
    "support_wait": list(_CONTACT_BUTTONS),
    "receipt_wait": list(_CONTACT_BUTTONS),
}


# ---- Caché de secciones y enlaces personalizados -------------------------------
@dataclass(frozen=True, slots=True)
class Menu:
    menu_id: str
    title: str
    parent: str | None
    text_es: str | None     # None = sección del sistema (texto en TEXTS / panel)
    text_en: str | None
    media: str               # "tipo:file_id" o ""
    is_system: bool


_menus: dict[str, Menu] = {}
_custom_links: dict[str, tuple[str, str]] = {}   # clave -> (nombre, url)


def set_menus(menus: list[Menu]) -> None:
    _menus.clear()
    _menus.update({m.menu_id: m for m in menus})


def all_menus() -> list[Menu]:
    return list(_menus.values())


def get_menu(menu_id: str) -> Menu | None:
    return _menus.get(menu_id)


def set_custom_links(links: dict[str, tuple[str, str]]) -> None:
    _custom_links.clear()
    _custom_links.update(links)


def custom_links() -> dict[str, tuple[str, str]]:
    return dict(_custom_links)


def all_link_keys() -> list[str]:
    return [*LINK_CATALOG, *_custom_links]


def link_label(key: str) -> str:
    if key in LINK_CATALOG:
        return LINK_CATALOG[key][1]
    return _custom_links.get(key, (key, ""))[0]


# ===========================================================================
# TEXTOS — categorías del /panel
#   clave: (categoría, etiqueta). TEXTS se editan en ES y EN; las claves adm:… (equipo) en ES.
# ===========================================================================
TEXT_CATEGORIES: dict[str, str] = {
    "welcome": "👋 Bienvenida e idioma",
    "subs": "📺 Suscripciones y planes",
    "payconf": "📸 Confirmaciones de pago",
    "approval": "✅ Aprobación y acceso",
    "roles": "🎭 Roles en el grupo (creador / espectador)",
    "thanks": "🙏 Agradecimientos",
    "donations": "⭐ Donaciones",
    "auto": "🤖 Respuestas automáticas",
    "support": "💬 Soporte / DM",
    "buttons": "🔘 Nombres de botones del sistema",
    "team": "👥 Grupo de gestión y registros",
    "buttons_team": "👥 Botones del grupo de gestión",
    "buttons_panel": "🛠 Botones del panel",
}
TEXT_CATALOG: dict[str, tuple[str, str]] = {
    # Bienvenida e idioma
    "choose_lang": ("welcome", "Elegir idioma"),
    "lang_set": ("welcome", "Idioma elegido"),
    "welcome": ("welcome", "Mensaje de bienvenida"),
    "header_hint": ("welcome", "Aviso del botón divisor (header)"),
    # Suscripciones (textos de sección y planes)
    "creator_info": ("subs", "Sección Creadores"),
    "creator_paypal": ("subs", "Sección Creadores · PayPal"),
    "viewer_info": ("subs", "Sección Espectadores"),
    "viewer_paypal": ("subs", "Sección Espectadores · PayPal"),
    "paypal_hub": ("subs", "Sección PayPal y confirmación"),
    "choose_plan": ("subs", "Elegir plan (Stars)"),
    "no_plans": ("subs", "Sin planes disponibles"),
    "product_unavailable": ("subs", "Producto no disponible"),
    "stars_invoice_desc": ("subs", "Factura Stars · descripción"),
    "stars_invoice_label": ("subs", "Factura Stars · concepto"),
    "plan_month": ("subs", "Plan · 1 mes"),
    "plan_months": ("subs", "Plan · N meses"),
    "plan_lifetime": ("subs", "Plan · de por vida"),
    "plan_until": ("subs", "Plan con fecha de fin"),
    # Confirmaciones de pago
    "choose_product_receipt": ("payconf", "Elegir producto del comprobante"),
    "choose_plan_receipt": ("payconf", "Elegir plan del comprobante"),
    "receipt_instructions": ("payconf", "Instrucciones para enviar el comprobante"),
    "receipt_received": ("payconf", "Comprobante recibido (espera)"),
    "send_image": ("payconf", "Pedir captura como imagen"),
    "stars_fallback": ("payconf", "Stars recibidas (entrega pendiente)"),
    # Aprobación y acceso
    "receipt_rejected": ("approval", "Comprobante rechazado"),
    "link_expired": ("approval", "Enlace expirado sin usar"),
    "membership_reminder": ("approval", "Recordatorio antes de vencer"),
    "membership_expired": ("approval", "Suscripción vencida"),
    # Roles dentro del grupo
    "role_creator_on": ("roles", "Permisos de creador activados"),
    "role_viewer_on": ("roles", "Espectador: solo lectura"),
    "viewer_blocked": ("roles", "Aviso: espectador no puede publicar"),
    # Agradecimientos
    "access_granted": ("thanks", "Pago confirmado + acceso"),
    "access_renewed": ("thanks", "Renovación confirmada"),
    "access_activated": ("thanks", "Pago confirmado estando ya dentro del grupo"),
    "tip_thanks": ("thanks", "Gracias por donar"),
    # Donaciones
    "donations": ("donations", "Sección de donaciones"),
    "tip_invoice_title": ("donations", "Factura · título"),
    "tip_invoice_desc": ("donations", "Factura · descripción"),
    "tip_invoice_label": ("donations", "Factura · concepto"),
    "tip_invalid": ("donations", "Donación no disponible"),
    "tips_disabled": ("donations", "Donaciones desactivadas"),
    # Respuestas automáticas
    "relay_error": ("auto", "Error al entregar mensaje"),
    "invoice_invalid": ("auto", "Factura no válida"),
    "binance_info": ("auto", "Emergente de ID de pago (Binance)"),
    # Soporte
    "support_info": ("support", "Sección SUPPORT / DM"),
    "support_wait": ("support", "Respuesta automática al primer mensaje"),
    "support_closed_hint": ("support", "Aviso: abre el chat con el botón"),
    "support_ended": ("support", "Conversación finalizada"),
    # Botones del sistema
    "btn_lang_es": ("buttons", "Idioma español"),
    "btn_lang_en": ("buttons", "Idioma inglés"),
    "btn_back": ("buttons", "Volver"),
    "btn_home": ("buttons", "Menú principal"),
    "btn_join": ("buttons", "Entrar (acceso)"),
    "btn_tip_option": ("buttons", "Monto de donación"),
    "btn_renew": ("buttons", "Renovar suscripción"),
    "btn_open_support": ("buttons", "Abrir chat de soporte"),
    "plan_btn_stars": ("buttons", "Plan con precio en Stars"),
    "plan_btn_ext": ("buttons", "Plan con precio externo"),
    # Equipo (ES)
    "adm:topic_header": ("team", "Ficha de nuevo contacto"),
    "adm:support_opened": ("team", "Soporte abierto"),
    "adm:receipt_alert": ("team", "Alerta de comprobante"),
    "adm:approved_suffix": ("team", "Comprobante aprobado"),
    "adm:rejected_suffix": ("team", "Comprobante rechazado"),
    "adm:stars_vip_topic": ("team", "Pago con Stars recibido"),
    "adm:sale_log": ("team", "Canal de ventas · registro"),
    "adm:vip_joined": ("team", "Miembro entró"),
    "adm:tip_topic": ("team", "Donación recibida"),
    "adm:tip_log": ("team", "Canal de donaciones · registro"),
    "adm:membership_expired_topic": ("team", "Suscripción vencida"),
    "adm:membership_reminder_topic": ("team", "Recordatorio enviado"),
}
ADMIN_TEXT_PREFIX = "adm:"
_TEAM_BUTTON_KEYS = (
    "btn_approve_plan", "btn_change_plan", "btn_reject", "btn_pick_product", "btn_pick_plan",
    "btn_back_products", "btn_support_end", "btn_reissue", "btn_link_chat", "btn_ignore",
)
for _bkey in _TEAM_BUTTON_KEYS:
    TEXT_CATALOG[f"{ADMIN_TEXT_PREFIX}{_bkey}"] = ("buttons_team", ADMIN_TEXTS[_bkey])
for _bkey in ADMIN_TEXTS:
    if (_bkey.startswith("btn_") or _bkey.startswith("panel_group_")) and _bkey not in _TEAM_BUTTON_KEYS:
        TEXT_CATALOG[f"{ADMIN_TEXT_PREFIX}{_bkey}"] = ("buttons_panel", ADMIN_TEXTS[_bkey])
EDITABLE_ADMIN_TEXTS: frozenset[str] = frozenset(
    k[len(ADMIN_TEXT_PREFIX):] for k in TEXT_CATALOG if k.startswith(ADMIN_TEXT_PREFIX)
)
MENU_TEXT_KEYS = frozenset(text_key for text_key, _, _ in SYSTEM_MENUS.values())
LANG_FLAGS = {"es": "🇪🇸 ES", "en": "🇺🇸 EN"}

GLOBAL_PLACEHOLDERS = frozenset({
    "stars_price", "paypal_price", "vip_days", "ttl_min",
    "creator_stars", "creator_paypal", "viewer_stars", "viewer_paypal",
})
WELCOME_PLACEHOLDERS = frozenset({"mention", "id", "name", "MENTION", "ID", "NAME"})
BUTTON_NAME_PLACEHOLDERS = frozenset({
    "stars_price", "paypal_price", "vip_days", "amount",
    "creator_stars", "creator_paypal", "viewer_stars", "viewer_paypal",
})
TEXT_MAX_LENGTH = 4000          # textos normales (caben en un mensaje con margen)
LONG_TEXT_MAX_LENGTH = 16000    # bienvenida y secciones: se dividen en varios mensajes si hace falta
MEDIA_KINDS = ("photo", "video", "animation", "sticker")
CAPTION_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    kind: str      # "template" | "int" | "url" | "url_or_id" | "choice"
    group: str     # "texts" | "links" | "subs" | "support"
    category: str
    label: str
    help: str
    placeholders: frozenset[str] = frozenset()
    max_length: int = 0
    optional: bool = False
    min_value: int = 0
    max_value: int = 0
    choices: tuple[str, ...] = ()


def _default_fields(template: str) -> frozenset[str]:
    return frozenset(f for _, f, _, _ in string.Formatter().parse(template) if f)


SETTINGS: dict[str, Setting] = {}

for _ckey, (_cat, _label) in TEXT_CATALOG.items():
    if _ckey.startswith(ADMIN_TEXT_PREFIX):
        _akey = _ckey[len(ADMIN_TEXT_PREFIX):]
        _sk = f"adm_{_akey}"
        SETTINGS[_sk] = Setting(
            _sk, "template", "texts", _cat, f"👥 {_label}", "Texto para el equipo (grupo de gestión / canales / panel).",
            _default_fields(ADMIN_TEXTS[_akey]) | GLOBAL_PLACEHOLDERS, TEXT_MAX_LENGTH,
        )
        continue
    for _lang in TEXTS:
        _sk = f"{_ckey}_{_lang}"
        _extra = WELCOME_PLACEHOLDERS if _ckey == "welcome" else frozenset()
        SETTINGS[_sk] = Setting(
            _sk, "template", "texts", _cat, f"{_label} {LANG_FLAGS.get(_lang, _lang.upper())}",
            "Texto que ve el miembro.",
            _default_fields(TEXTS[_lang][_ckey]) | GLOBAL_PLACEHOLDERS | _extra,
            LONG_TEXT_MAX_LENGTH if _ckey in MENU_TEXT_KEYS else TEXT_MAX_LENGTH,
        )

for _lkey, (_cat, _label, _default, _optional) in LINK_CATALOG.items():
    _kind = "url_or_id" if _lkey == "binance_link" else "url"
    SETTINGS[_lkey] = Setting(
        _lkey, _kind, "links", _cat, f"🔗 {_label}",
        "URL de Binance o tu Binance Pay ID (se muestra en una ventana emergente)." if _kind == "url_or_id"
        else "Enlace usado por los botones que lo referencian (link:clave).",
        max_length=512, optional=_optional,
    )

SETTINGS["reminder_days"] = Setting(
    "reminder_days", "int", "subs", "settings", "⏰ Recordatorio antes de vencer",
    "Días de anticipación del recordatorio de renovación (mínimo 3).", min_value=3, max_value=30,
)
SETTINGS["invite_ttl_minutes"] = Setting(
    "invite_ttl_minutes", "int", "subs", "settings", "⏱ Validez del enlace de acceso",
    "Minutos que el enlace de acceso puede usarse antes de caducar (máx. 47 h = 2820 min).",
    min_value=INVITE_TTL_MIN_MINUTES, max_value=INVITE_TTL_MAX_MINUTES,
)
SETTINGS["access_mode"] = Setting(
    "access_mode", "choice", "subs", "settings", "🔐 Modo de acceso", "request o link.", choices=ACCESS_MODES,
)
SETTINGS["viewer_mode"] = Setting(
    "viewer_mode", "choice", "subs", "settings", "🎟️ Permisos del espectador en el grupo",
    "readonly (solo lectura) o text (solo texto, sin multimedia ni enlaces).", choices=VIEWER_MODES,
)
SETTINGS["support_mode"] = Setting(
    "support_mode", "choice", "support", "settings", "💬 Modo del chat de soporte", "button o always.",
    choices=("button", "always"),
)

_DEFAULTS: dict[str, Any] = {
    "reminder_days": lambda: str(DEFAULT_REMINDER_DAYS),
    "invite_ttl_minutes": lambda: str(DEFAULT_INVITE_TTL_MINUTES),
    "access_mode": lambda: _str("ACCESS_MODE", "request"),
    "viewer_mode": lambda: _str("VIEWER_MODE", "readonly"),
    "support_mode": lambda: _str("SUPPORT_MODE", "button"),
    **{key: (lambda k=key: LINK_CATALOG[k][2]) for key in LINK_CATALOG},
}

# Caché en memoria de bot_config: {key: {"value": str, "updated_at": str}}
_overrides: dict[str, dict[str, str]] = {}


def setting_default(key: str) -> str:
    if key in _DEFAULTS:
        return _DEFAULTS[key]()
    if key.startswith("adm_"):
        return ADMIN_TEXTS[key[4:]]
    text_key, lang = key.rsplit("_", 1)
    return TEXTS[lang][text_key]


def setting_value(key: str) -> str:
    override = _overrides.get(key)
    return override["value"] if override else setting_default(key)


def setting_is_custom(key: str) -> bool:
    return key in _overrides


def setting_updated_at(key: str) -> str | None:
    override = _overrides.get(key)
    return override["updated_at"] if override else None


def apply_override(key: str, value: str, updated_at: str = "") -> None:
    _overrides[key] = {"value": value, "updated_at": updated_at}


def drop_override(key: str) -> None:
    _overrides.pop(key, None)


def custom_count() -> int:
    return len(_overrides)


def load_overrides(rows: dict[str, dict[str, Any]]) -> list[str]:
    """Carga bot_config al arrancar. Ignora (y devuelve) claves desconocidas o valores ya no válidos."""
    _overrides.clear()
    skipped: list[str] = []
    for key, row in rows.items():
        if key not in SETTINGS:
            skipped.append(key)
            continue
        try:
            value = normalize_setting(key, row["value"])
        except SettingError:
            skipped.append(key)
            continue
        apply_override(key, value, row.get("updated_at") or "")
    return skipped


# ---- Accesores tipados -------------------------------------------------------
def reminder_days() -> int:
    return int(setting_value("reminder_days"))


def invite_ttl_seconds() -> int:
    return int(setting_value("invite_ttl_minutes")) * 60


def access_mode() -> str:
    mode = setting_value("access_mode")
    return mode if mode in ACCESS_MODES else "request"


def viewer_mode() -> str:
    value = setting_value("viewer_mode")
    return value if value in VIEWER_MODES else "readonly"


def support_mode() -> str:
    return setting_value("support_mode")


def link(key: str) -> str:
    """URL vigente de un enlace del catálogo o personalizado ('' si no está configurado)."""
    if key in LINK_CATALOG:
        return setting_value(key)
    return _custom_links.get(key, ("", ""))[1]


def ttl_minutes() -> int:
    return invite_ttl_seconds() // 60


def ttl_label() -> str:
    minutes = ttl_minutes()
    return ta("ttl_hours", n=minutes // 60) if minutes % 60 == 0 else ta("ttl_minutes", n=minutes)


def _reference_plan(product_key: str) -> Plan | None:
    """Plan de referencia de un producto para los textos: 1 mes activo (o el primero activo, o el de 1 mes)."""
    monthly = get_plan(product_key, 1)
    if monthly and monthly.is_active:
        return monthly
    return next(iter(plans(product_key)), None) or monthly


def _dynamic_vars() -> dict[str, Any]:
    creator, viewer = _reference_plan("creator"), _reference_plan("viewer")
    return {
        "stars_price": creator.stars_price if creator else 0,
        "paypal_price": creator.ext_price if creator else "",
        "creator_stars": creator.stars_price if creator else 0,
        "creator_paypal": creator.ext_price if creator else "",
        "viewer_stars": viewer.stars_price if viewer else 0,
        "viewer_paypal": viewer.ext_price if viewer else "",
        "vip_days": 30,
        "ttl_min": ttl_minutes(),
    }


def plan_label(lang: str | None, months: int) -> str:
    if months <= 0:
        return t(lang, "plan_lifetime")
    return t(lang, "plan_month") if months == 1 else t(lang, "plan_months", n=months)


def plan_label_admin(months: int) -> str:
    if months <= 0:
        return ta("plan_lifetime")
    return ta("plan_month") if months == 1 else ta("plan_months", n=months)


def plan_text(lang: str | None, months: int, expires_at: int | None) -> str:
    label = plan_label(lang, months)
    return t(lang, "plan_until", plan=label, date=format_date(expires_at)) if expires_at else label


def format_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def product_name(lang: str | None, product_key: str) -> str:
    product = get_product(product_key)
    if not product:
        return product_key
    return product.name_en if lang == "en" and product.name_en else product.name_es


def product_name_admin(product_key: str) -> str:
    product = get_product(product_key)
    return product.name_es if product else product_key


# ---- Botones -----------------------------------------------------------------
def resolve_button_url(target: str) -> str:
    """URL final de un botón `url` (resuelve las referencias link:clave). '' si no hay enlace."""
    return link(target[5:]) if target.startswith("link:") else target


def is_http_url(value: str) -> bool:
    return bool(_URL_RE.match(value))


def split_bilingual(value: str, lang: str | None) -> str:
    """'texto ES || text EN' → la parte del idioma del miembro (o el texto completo si no hay separador)."""
    if " || " not in value:
        return value
    es, en = value.split(" || ", 1)
    return en.strip() if lang == "en" else es.strip()


def render_button_name(name: str, **extra: Any) -> str:
    try:
        return name.format(**{**_dynamic_vars(), **extra})
    except (KeyError, IndexError, ValueError):
        return name


# ---- Deep links (t.me/<bot>?start=...) ---------------------------------------------
# m_<sección> abre una sección · s_<producto> planes con Stars · r_<producto> confirmar pago
# t_<monto> donación · sp abre el chat de soporte
DEEPLINK_RE = re.compile(r"^(m|s|r|t)_([A-Za-z0-9_]{1,40})$|^sp$")


def deep_link(bot_username: str, payload: str) -> str:
    return f"https://t.me/{bot_username}?start={payload}"


# ---- Validación de valores del panel ---------------------------------------
class SettingError(ValueError):
    """Valor de ajuste no válido. El mensaje ya viene listo para mostrarse al admin."""


_URL_RE = re.compile(r"^(https?|tg)://[^\s<>\"']+$")
_PAY_ID_RE = re.compile(r"^[^\s<>&{}\"']+$")
_FORBIDDEN_PRICE_CHARS = set("<>&{}\n\r")


def template_fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def check_template(template: str, allowed: frozenset[str]) -> None:
    try:
        fields = template_fields(template)
    except ValueError:
        raise SettingError(ta("err_braces")) from None
    for field in fields:
        if field not in allowed:
            shown = ", ".join("{" + p + "}" for p in sorted(allowed))
            raise SettingError(ta("err_unknown_var", var=field or "", allowed=shown))


def check_price_text(value: str, max_length: int = 24) -> str:
    value = value.strip()
    if not 1 <= len(value) <= max_length or _FORBIDDEN_PRICE_CHARS & set(value):
        raise SettingError(ta("err_price_text", max=max_length))
    return value


def normalize_setting(key: str, raw: str) -> str:
    """Valida y normaliza un valor para `key`. Devuelve el valor listo para guardar o lanza SettingError."""
    setting = SETTINGS[key]
    value = (raw or "").strip()
    if not value:
        raise SettingError(ta("err_empty"))
    if setting.kind == "template":
        if len(value) > setting.max_length:
            raise SettingError(ta("err_too_long", length=len(value), max=setting.max_length))
        check_template(value, setting.placeholders)
        return value
    if setting.kind == "int":
        if not value.isdigit() or not setting.min_value <= int(value) <= setting.max_value:
            raise SettingError(ta("err_int", min=setting.min_value, max=setting.max_value))
        return str(int(value))
    if setting.kind == "url":
        if len(value) > setting.max_length or not _URL_RE.match(value):
            raise SettingError(ta("err_url", max=setting.max_length))
        return value
    if setting.kind == "url_or_id":
        if len(value) > setting.max_length or not (_URL_RE.match(value) or _PAY_ID_RE.match(value)):
            raise SettingError(ta("err_url_or_id", max=setting.max_length))
        return value
    if setting.kind == "choice":
        if value not in setting.choices:
            raise SettingError(ta("err_empty"))
        return value
    raise SettingError(ta("err_empty"))


def check_url(value: str) -> str:
    value = value.strip()
    if len(value) > 512 or not _URL_RE.match(value):
        raise SettingError(ta("err_pb_url"))
    return value


def render_template(template: str, **kwargs: Any) -> str:
    variables = {**_dynamic_vars(), **kwargs}
    for name in ("mention", "id", "name"):
        if name in variables:
            variables.setdefault(name.upper(), variables[name])
    return template.format(**variables)


def t(lang: str | None, key: str, /, **kwargs: Any) -> str:
    """Texto para el miembro en su idioma, con los ajustes del /panel aplicados."""
    lang = lang if lang in TEXTS else DEFAULT_LANG
    default = TEXTS[lang].get(key) or TEXTS[DEFAULT_LANG][key]
    template = setting_value(f"{key}_{lang}") if key in TEXT_CATALOG else default
    try:
        return render_template(template, **kwargs)
    except (KeyError, IndexError, ValueError):
        return render_template(default, **kwargs)


def menu_text(menu_id: str, lang: str | None, /, **kwargs: Any) -> str:
    """Texto de una sección: del sistema (TEXTS + panel) o creada por el admin (ui_menus)."""
    menu = get_menu(menu_id)
    if menu is None:
        return ""
    if menu.is_system and menu_id in SYSTEM_MENUS:
        return t(lang, SYSTEM_MENUS[menu_id][0], **kwargs)
    template = (menu.text_en if lang == "en" and menu.text_en else menu.text_es) or menu.title
    try:
        return render_template(template, **kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def ta(key: str, /, **kwargs: Any) -> str:
    """Texto para el equipo (los marcados en TEXT_CATALOG como adm:… son editables desde /panel)."""
    default = ADMIN_TEXTS[key]
    template = setting_value(f"adm_{key}") if key in EDITABLE_ADMIN_TEXTS else default
    try:
        return template.format(**{**_dynamic_vars(), **kwargs})
    except (KeyError, IndexError, ValueError):
        try:
            return default.format(**{**_dynamic_vars(), **kwargs})
        except (KeyError, IndexError, ValueError):
            return default


# ===========================================================================
# Validación al arrancar
# ===========================================================================
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def is_bot_token(value: str) -> bool:
    return bool(_TOKEN_RE.match(value))


def is_supergroup_or_channel(chat_id: int) -> bool:
    return str(chat_id).startswith("-100") and len(str(chat_id)) >= 14


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def validate() -> None:
    """Valida TODA la configuración y reporta todos los problemas a la vez (falla rápido al arrancar)."""
    errors = list(_CONFIG_ERRORS)
    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_GROUP_ID": ADMIN_GROUP_ID,
        "SALES_LOG_CHANNEL_ID": SALES_LOG_CHANNEL_ID,
        "ADMIN_IDS": ADMIN_IDS,
        "BETA_HOUSE_CHAT_ID": BETA_HOUSE_CHAT_ID,
        "DB_PATH": DB_PATH,
    }
    if TIPS_ENABLED:
        required["TIPS_LOG_CHANNEL_ID"] = TIPS_LOG_CHANNEL_ID
    missing = [name for name, value in required.items() if not value]
    if missing:
        errors.append(f"Faltan variables obligatorias en .env: {', '.join(missing)}")
    if BOT_TOKEN and not is_bot_token(BOT_TOKEN):
        errors.append("BOT_TOKEN no tiene el formato de @BotFather (123456789:AA...).")

    chats = {"ADMIN_GROUP_ID": ADMIN_GROUP_ID, "SALES_LOG_CHANNEL_ID": SALES_LOG_CHANNEL_ID}
    if TIPS_ENABLED:
        chats["TIPS_LOG_CHANNEL_ID"] = TIPS_LOG_CHANNEL_ID
    for key, chat_id, _, _ in PRODUCT_SEEDS:
        if chat_id:
            chats[f"chat semilla '{key}'"] = chat_id
    for name, chat_id in chats.items():
        if chat_id and not is_supergroup_or_channel(chat_id):
            errors.append(f"{name}={chat_id} no parece un supergrupo/canal (debe empezar por -100).")
    for key, chat_id, _, _ in PRODUCT_SEEDS:
        if chat_id and chat_id == ADMIN_GROUP_ID:
            errors.append(f"El chat del producto '{key}' no puede ser el grupo de gestión.")

    if any(uid <= 0 for uid in ADMIN_IDS):
        errors.append("ADMIN_IDS solo admite IDs de usuario (números positivos).")
    if not 1 <= len(SUBSCRIPTION_TITLE) <= 32:
        errors.append(f"SUBSCRIPTION_TITLE debe tener 1–32 caracteres (tiene {len(SUBSCRIPTION_TITLE)}).")
    if SWEEP_INTERVAL_SECONDS < 10:
        errors.append("SWEEP_INTERVAL_SECONDS debe ser al menos 10.")
    if LOG_LEVEL not in _LOG_LEVELS:
        errors.append(f"LOG_LEVEL debe ser uno de: {', '.join(sorted(_LOG_LEVELS))}.")
    try:
        ZoneInfo(TIMEZONE)
    except Exception:
        errors.append(f"TIMEZONE no válida: {TIMEZONE} (instala tzdata o usa p. ej. America/Bogota).")

    for key, setting in SETTINGS.items():
        default = setting_default(key)
        if not default and setting.optional:
            continue
        try:
            normalize_setting(key, default)
        except SettingError as exc:
            errors.append(f"Valor por defecto de '{key}' inválido: {_plain(str(exc))}")

    for lang, table in TEXTS.items():
        missing_keys = set(TEXTS[DEFAULT_LANG]) - set(table)
        if missing_keys:
            errors.append(f"Faltan textos en '{lang}': {', '.join(sorted(missing_keys))}")
    uncatalogued = set(TEXTS[DEFAULT_LANG]) - set(TEXT_CATALOG)
    if uncatalogued:
        errors.append(f"Textos sin categoría en TEXT_CATALOG: {', '.join(sorted(uncatalogued))}")
    for key, role in PRODUCT_ROLES.items():
        if role not in MEMBER_ROLES:
            errors.append(f"Rol inválido para el producto '{key}': {role}.")
    for key, seeds in PLAN_SEEDS.items():
        for months, stars, _ext, _active in seeds:
            if months not in PLAN_MONTHS or not 0 <= stars <= STARS_PRICE_MAX:
                errors.append(f"Plan semilla inválido para '{key}': {months} meses / {stars} XTR.")
    for menu_id, (text_key, parent, _) in SYSTEM_MENUS.items():
        if text_key not in TEXTS[DEFAULT_LANG] or (parent and parent not in SYSTEM_MENUS):
            errors.append(f"Sección '{menu_id}' mal definida.")
        buttons = DEFAULT_BUTTONS.get(menu_id, [])
        if len(buttons) > MAX_BUTTONS_PER_MENU:
            errors.append(f"Sección '{menu_id}' supera {MAX_BUTTONS_PER_MENU} botones.")
        for bid, _es, _en, action, target, _ in buttons:
            if action not in BUTTON_ACTIONS or (action == "menu" and target not in SYSTEM_MENUS):
                errors.append(f"Botón '{menu_id}/{bid}' mal definido.")
            if action == "url" and target.startswith("link:") and target[5:] not in LINK_CATALOG:
                errors.append(f"Botón '{menu_id}/{bid}' usa un enlace inexistente: {target}.")

    if errors:
        raise RuntimeError("Configuración inválida:\n  - " + "\n  - ".join(errors))
