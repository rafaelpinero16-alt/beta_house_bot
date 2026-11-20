"""Persistencia SQLite (aiosqlite): miembros, temas, pagos, membresías, donaciones, accesos, baneos,
botones dinámicos, clones autorizados y ajustes del /panel.

Una sola conexión compartida en modo WAL. El esquema se migra de forma aditiva:
el `bot_data.db` existente se conserva y nunca se borran datos.

Los temas se guardan por grupo (`forum_topics.group_id`): si cambias ADMIN_GROUP_ID, los temas
del grupo anterior quedan archivados y el bot crea temas nuevos, sin riesgo de que un número
de tema del grupo viejo coincida con otro del grupo nuevo y mezcle conversaciones.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import aiosqlite

import config

log = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    full_name        TEXT,
    username         TEXT,
    lang             TEXT NOT NULL DEFAULT 'es',
    awaiting_receipt INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Temas por grupo de soporte (la tabla antigua `topics` queda solo como archivo histórico).
CREATE TABLE IF NOT EXISTS forum_topics (
    group_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    topic_id   INTEGER NOT NULL,
    full_name  TEXT,
    username   TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, user_id),
    UNIQUE (group_id, topic_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    method      TEXT NOT NULL,                       -- stars | paypal | manual
    amount      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    charge_id   TEXT,                                -- telegram_payment_charge_id (Stars)
    resolved_by INTEGER,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS access_grants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    payment_id          INTEGER,
    chat_id             INTEGER NOT NULL,
    invite_link         TEXT NOT NULL UNIQUE,
    expire_at           INTEGER NOT NULL,            -- epoch UTC
    delivery_message_id INTEGER,
    status              TEXT NOT NULL DEFAULT 'issued',  -- issued | used | expired | revoked
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at           TEXT
);

-- Propinas: tabla aislada de los pagos VIP (contabilidad separada).
CREATE TABLE IF NOT EXISTS tips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    method     TEXT NOT NULL DEFAULT 'stars_tip',
    amount     INTEGER NOT NULL,                     -- en Stars (XTR)
    currency   TEXT NOT NULL DEFAULT 'XTR',
    payload    TEXT,
    charge_id  TEXT NOT NULL UNIQUE,                 -- telegram_payment_charge_id (para reembolsos)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Ajustes editables desde /panel (sobrescriben los valores por defecto de config.py / .env).
CREATE TABLE IF NOT EXISTS bot_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Suscripciones por tiempo: al vencer, el bot retira al miembro del chat del producto.
CREATE TABLE IF NOT EXISTS memberships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    product     TEXT NOT NULL,                       -- clave del producto (creator | viewer | …)
    chat_id     INTEGER NOT NULL,
    expires_at  INTEGER,                             -- epoch UTC; NULL = permanente
    status      TEXT NOT NULL DEFAULT 'active',      -- active | expired | cancelled
    payment_id  INTEGER,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Miembros vetados: el bot ignora todas sus actualizaciones.
CREATE TABLE IF NOT EXISTS banned_users (
    user_id   INTEGER PRIMARY KEY,
    reason    TEXT,
    banned_by INTEGER,
    banned_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Botones de cada menú (editables desde /panel → Botones). Se siembran desde config.DEFAULT_BUTTONS.
CREATE TABLE IF NOT EXISTS ui_buttons (
    menu_id      TEXT NOT NULL,
    button_id    TEXT NOT NULL,
    name_es      TEXT NOT NULL,
    name_en      TEXT NOT NULL,
    action_type  TEXT NOT NULL,                      -- url | alert | nav | stars
    target_value TEXT NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    same_row     INTEGER NOT NULL DEFAULT 0,         -- 1 = comparte fila con el botón anterior
    PRIMARY KEY (menu_id, button_id)
);

-- Clones autorizados del bot (cada uno es un despliegue independiente con su propio .env).
CREATE TABLE IF NOT EXISTS bot_clones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id         INTEGER NOT NULL UNIQUE,
    bot_username   TEXT,
    bot_token      TEXT NOT NULL,
    admin_group_id INTEGER NOT NULL,
    vip_chat_id    INTEGER NOT NULL,
    sales_log_id   INTEGER NOT NULL,
    tips_log_id    INTEGER NOT NULL DEFAULT 0,
    admin_ids      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    created_by     INTEGER,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Productos (tarifas/roles): varios productos pueden apuntar al MISMO chat (p. ej. Beta House: creator + viewer).
CREATE TABLE IF NOT EXISTS products (
    key          TEXT PRIMARY KEY,
    chat_id      INTEGER,
    chat_type    TEXT,
    name_es      TEXT NOT NULL,
    name_en      TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1,
    can_invite   INTEGER NOT NULL DEFAULT 0,
    can_restrict INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Planes de cada producto: 1, 3, 6, 12 meses y de por vida (0).
CREATE TABLE IF NOT EXISTS product_plans (
    product_key TEXT NOT NULL,
    months      INTEGER NOT NULL,
    stars_price INTEGER NOT NULL DEFAULT 0,        -- 0 = no se vende con Stars
    ext_price   TEXT NOT NULL DEFAULT '',          -- '' = no se ofrece por PayPal/otros
    is_active   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_key, months)
);

-- Chats donde el bot fue agregado (detectados por my_chat_member).
CREATE TABLE IF NOT EXISTS linked_chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT,
    chat_type    TEXT,
    status       TEXT,                             -- administrator | member | left | kicked
    can_invite   INTEGER NOT NULL DEFAULT 0,
    can_restrict INTEGER NOT NULL DEFAULT 0,
    can_post     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Secciones (mensajes que abren los botones). Las del sistema toman su texto de config/panel.
CREATE TABLE IF NOT EXISTS ui_menus (
    menu_id    TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    parent     TEXT,
    text_es    TEXT,
    text_en    TEXT,
    media      TEXT NOT NULL DEFAULT '',
    is_system  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Enlaces creados desde el panel (además del catálogo de config.LINK_CATALOG).
CREATE TABLE IF NOT EXISTS custom_links (
    key        TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Anuncios y publicaciones de donación (inmediatos o programados).
CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,                    -- announce | donation
    text         TEXT NOT NULL DEFAULT '',
    media        TEXT NOT NULL DEFAULT '',
    buttons      TEXT NOT NULL DEFAULT '[]',       -- JSON [[texto, url], ...]
    target       TEXT NOT NULL,                    -- users | chats | chat:<id> | me:<id>
    send_at      INTEGER,
    repeat_hours INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'draft',    -- draft | scheduled | sending | sent | cancelled
    sent_count   INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    created_by   INTEGER,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    last_sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_payments_user   ON payments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_products_chat   ON products(chat_id);

-- Canales autorizados de creadores: pueden publicar «como canal» en los grupos con roles
-- mientras su dueño tenga una suscripción de creador activa.
CREATE TABLE IF NOT EXISTS allowed_channels (
    channel_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    added_by   INTEGER,
    added_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_allowed_channels_user ON allowed_channels(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_due       ON posts(status, send_at);
CREATE INDEX IF NOT EXISTS idx_buttons_menu    ON ui_buttons(menu_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_memberships     ON memberships(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_memberships_usr ON memberships(user_id, product, status);
CREATE INDEX IF NOT EXISTS idx_tips_user       ON tips(user_id);
CREATE INDEX IF NOT EXISTS idx_grants_status   ON access_grants(status, expire_at);
CREATE INDEX IF NOT EXISTS idx_grants_user     ON access_grants(user_id, status);
"""

# Columnas añadidas sobre tablas que ya existían en versiones anteriores.
MIGRATIONS: dict[str, dict[str, str]] = {
    "payments": {
        "resolved_at": "TEXT",
        "product": "TEXT",          # vip | full | pnp | dirty (NULL = aún sin asignar)
        "plan_days": "INTEGER",     # 0 = permanente
        "receipt_kind": "TEXT",     # vip | channel | NULL (sección desde la que se envió el comprobante)
        "plan_months": "INTEGER",   # 0 = de por vida
    },
    "memberships": {
        "reminded": "INTEGER NOT NULL DEFAULT 0",   # 1 = ya se envió el recordatorio previo
    },
    "users": {
        "receipt_kind": "TEXT",                                # producto del comprobante esperado ('*' = sin elegir)
        "receipt_months": "INTEGER",                           # plan elegido para el comprobante
        "support_ack_pending": "INTEGER NOT NULL DEFAULT 0",   # enviar mensaje de espera al próximo mensaje
        "support_open": "INTEGER NOT NULL DEFAULT 0",          # chat de soporte abierto
    },
    "products": {
        # Rol dentro del chat: creator (publica texto, multimedia y enlaces) | viewer (solo lectura) |
        # none (el bot no toca los permisos del miembro).
        "member_role": "TEXT NOT NULL DEFAULT 'none'",
    },
    "access_grants": {
        "product_key": "TEXT",
        "plan_months": "INTEGER",
        "pending_start": "INTEGER NOT NULL DEFAULT 0",         # 1 = la suscripción empieza al entrar
    },
}


def _check_db_path(path: str) -> None:
    """Falla con un mensaje claro si la carpeta de la base de datos no existe o no es escribible."""
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        raise RuntimeError(f"La carpeta de DB_PATH no existe: {folder}")
    if not os.access(folder, os.W_OK):
        raise RuntimeError(f"Sin permiso de escritura en la carpeta de DB_PATH: {folder}")
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise RuntimeError(f"Sin permiso de escritura sobre la base de datos: {path}")


_PRODUCTS_DDL = """
CREATE TABLE products__new (
    key          TEXT PRIMARY KEY,
    chat_id      INTEGER,
    chat_type    TEXT,
    name_es      TEXT NOT NULL,
    name_en      TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1,
    can_invite   INTEGER NOT NULL DEFAULT 0,
    can_restrict INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    member_role  TEXT NOT NULL DEFAULT 'none'
)
"""


async def _products_chat_is_unique(conn: aiosqlite.Connection) -> bool:
    """True si la tabla products (versión anterior) tiene un índice UNIQUE solo sobre chat_id."""
    async with conn.execute("PRAGMA index_list(products)") as cur:
        indexes = [dict(r) for r in await cur.fetchall()]
    for index in indexes:
        if not index["unique"] or index.get("origin") == "pk":
            continue
        async with conn.execute(f"PRAGMA index_info('{index['name']}')") as cur:
            columns = [r["name"] for r in await cur.fetchall()]
        if columns == ["chat_id"]:
            return True
    return False


async def _migrate_products_multi_chat(conn: aiosqlite.Connection) -> bool:
    """Quita el UNIQUE(chat_id) de products recreando la tabla y conservando todos los datos (idempotente)."""
    if not await _products_chat_is_unique(conn):
        return False
    async with conn.execute("PRAGMA table_info(products)") as cur:
        old_columns = [r["name"] for r in await cur.fetchall()]
    keep = [c for c in old_columns if c in {
        "key", "chat_id", "chat_type", "name_es", "name_en", "is_active", "can_invite", "can_restrict",
        "created_at", "member_role",
    }]
    cols = ", ".join(keep)
    await conn.execute("BEGIN")
    try:
        await conn.execute("DROP TABLE IF EXISTS products__new")
        await conn.execute(_PRODUCTS_DDL)
        await conn.execute(f"INSERT INTO products__new ({cols}) SELECT {cols} FROM products")
        await conn.execute("DROP TABLE products")
        await conn.execute("ALTER TABLE products__new RENAME TO products")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_products_chat ON products(chat_id)")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    log.info("Migración: products ya admite varios productos por chat (UNIQUE(chat_id) eliminado).")
    return True


async def init_db() -> None:
    global _conn
    _check_db_path(config.DB_PATH)
    _conn = await aiosqlite.connect(config.DB_PATH)
    _conn.row_factory = aiosqlite.Row
    async with _conn.execute("PRAGMA quick_check") as cur:
        result = (await cur.fetchone())[0]
    if result != "ok":
        raise RuntimeError(f"La base de datos {config.DB_PATH} está dañada: {result}")
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA busy_timeout=5000")
    await _conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        async with _conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for col, ddl in columns.items():
            if col not in existing:
                await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    await _conn.commit()
    await _migrate_products_multi_chat(_conn)

    async with _conn.execute(
        "SELECT COUNT(*) FROM forum_topics WHERE group_id = ?", (config.ADMIN_GROUP_ID,)
    ) as cur:
        active_topics = (await cur.fetchone())[0]
    log.info("Base de datos lista (%s) — temas en el grupo actual: %s", config.DB_PATH, active_topics)


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("La base de datos no está inicializada (llama a init_db()).")
    return _conn


async def _fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    async with _db().execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    async with _db().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _write(sql: str, params: tuple = ()) -> aiosqlite.Cursor:
    cur = await _db().execute(sql, params)
    await _db().commit()
    return cur


# ------------------------------- Ajustes (/panel) --------------------------
async def get_all_config() -> dict[str, dict[str, Any]]:
    """{key: {"value": ..., "updated_at": ...}} de todos los ajustes personalizados."""
    rows = await _fetchall("SELECT key, value, updated_at FROM bot_config")
    return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}


async def set_config(key: str, value: str) -> None:
    await _write(
        """
        INSERT INTO bot_config (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (key, value),
    )


async def delete_config(key: str) -> bool:
    cur = await _write("DELETE FROM bot_config WHERE key = ?", (key,))
    return cur.rowcount == 1


# ------------------------------- Usuarios ----------------------------------
async def upsert_user(
    user_id: int, full_name: str, username: str | None, initial_lang: str = "es"
) -> None:
    """Crea o actualiza el usuario. `initial_lang` solo se aplica a usuarios nuevos."""
    await _write(
        """
        INSERT INTO users (user_id, full_name, username, lang) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name = excluded.full_name,
            username  = excluded.username,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, full_name, username, initial_lang),
    )


async def get_user(user_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def get_lang(user_id: int) -> str:
    row = await _fetchone("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    return row["lang"] if row else "es"


async def set_lang(user_id: int, lang: str) -> None:
    await _write("UPDATE users SET lang = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (lang, user_id))


async def set_receipt_mode(user_id: int, product: str | None, months: int | None = None) -> None:
    """Marca que el miembro va a enviar un comprobante (producto y plan elegidos); None lo apaga."""
    await _write(
        "UPDATE users SET awaiting_receipt = ?, receipt_kind = ?, receipt_months = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ?",
        (int(product is not None), product, months, user_id),
    )


async def get_receipt_mode(user_id: int) -> tuple[str, int | None] | None:
    """(producto, meses) del comprobante esperado, o None. Producto '*' = el miembro aún no eligió."""
    row = await _fetchone(
        "SELECT awaiting_receipt, receipt_kind, receipt_months FROM users WHERE user_id = ?", (user_id,)
    )
    if not row or not row["awaiting_receipt"]:
        return None
    kind = row["receipt_kind"] or "*"
    if kind == "channel":  # versión anterior
        kind = "*"
    return kind, row["receipt_months"]


async def set_support_open(user_id: int, is_open: bool) -> bool:
    cur = await _write(
        "UPDATE users SET support_open = ? WHERE user_id = ? AND support_open != ?", (int(is_open), user_id, int(is_open))
    )
    return cur.rowcount == 1


async def is_support_open(user_id: int) -> bool:
    row = await _fetchone("SELECT support_open FROM users WHERE user_id = ?", (user_id,))
    return bool(row and row["support_open"])


async def find_user_by_username(username: str) -> dict[str, Any] | None:
    return await _fetchone(
        "SELECT * FROM users WHERE lower(username) = lower(?) ORDER BY updated_at DESC LIMIT 1", (username.lstrip("@"),)
    )


async def broadcast_user_ids() -> list[int]:
    """Miembros que han interactuado con el bot y no están vetados."""
    rows = await _fetchall(
        "SELECT user_id FROM users WHERE user_id NOT IN (SELECT user_id FROM banned_users) ORDER BY user_id"
    )
    return [r["user_id"] for r in rows]


async def set_support_ack(user_id: int, pending: bool) -> None:
    await _write("UPDATE users SET support_ack_pending = ? WHERE user_id = ?", (int(pending), user_id))


async def pop_support_ack(user_id: int) -> bool:
    """True una sola vez tras pulsar Soporte (atómico): indica que toca enviar el mensaje de espera."""
    cur = await _write(
        "UPDATE users SET support_ack_pending = 0 WHERE user_id = ? AND support_ack_pending = 1", (user_id,)
    )
    return cur.rowcount == 1


# ------------------------------- Temas -------------------------------------
# Todas las consultas se limitan al grupo de soporte configurado (config.ADMIN_GROUP_ID).
async def get_topic_id(user_id: int) -> int | None:
    row = await _fetchone(
        "SELECT topic_id FROM forum_topics WHERE group_id = ? AND user_id = ?", (config.ADMIN_GROUP_ID, user_id)
    )
    return row["topic_id"] if row else None


async def get_user_by_topic(topic_id: int) -> int | None:
    row = await _fetchone(
        "SELECT user_id FROM forum_topics WHERE group_id = ? AND topic_id = ?", (config.ADMIN_GROUP_ID, topic_id)
    )
    return row["user_id"] if row else None


async def save_topic(user_id: int, topic_id: int, full_name: str, username: str | None) -> None:
    await _write(
        "INSERT OR REPLACE INTO forum_topics (group_id, user_id, topic_id, full_name, username) "
        "VALUES (?, ?, ?, ?, ?)",
        (config.ADMIN_GROUP_ID, user_id, topic_id, full_name, username),
    )


async def delete_topic(user_id: int) -> None:
    await _write(
        "DELETE FROM forum_topics WHERE group_id = ? AND user_id = ?", (config.ADMIN_GROUP_ID, user_id)
    )


# ------------------------------- Pagos -------------------------------------
async def create_payment(
    user_id: int,
    method: str,
    amount: str | None,
    status: str = "pending",
    charge_id: str | None = None,
    resolved_by: int | None = None,
    product: str | None = None,
    plan_months: int | None = None,
    receipt_kind: str | None = None,
) -> int:
    cur = await _write(
        "INSERT INTO payments (user_id, method, amount, status, charge_id, resolved_by, product, plan_months, "
        "receipt_kind, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'pending' THEN NULL ELSE CURRENT_TIMESTAMP END)",
        (user_id, method, amount, status, charge_id, resolved_by, product, plan_months, receipt_kind, status),
    )
    return cur.lastrowid


async def set_payment_plan(payment_id: int, product: str, plan_months: int, amount: str | None = None) -> None:
    await _write(
        "UPDATE payments SET product = ?, plan_months = ?, amount = COALESCE(?, amount) WHERE id = ?",
        (product, plan_months, amount, payment_id),
    )


async def get_payment(payment_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM payments WHERE id = ?", (payment_id,))


async def get_pending_payment(user_id: int) -> dict[str, Any] | None:
    return await _fetchone(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (user_id,)
    )


async def charge_id_exists(charge_id: str) -> bool:
    """True si ese cobro de Stars ya se registró (como pago VIP o como propina)."""
    row = await _fetchone(
        "SELECT 1 AS x FROM payments WHERE charge_id = ? UNION ALL SELECT 1 FROM tips WHERE charge_id = ? LIMIT 1",
        (charge_id, charge_id),
    )
    return row is not None


async def transition_payment(
    payment_id: int, from_status: str, to_status: str, resolved_by: int | None = None
) -> bool:
    """Cambio de estado atómico: evita aprobar dos veces el mismo pago (doble clic, dos admins)."""
    cur = await _write(
        "UPDATE payments SET status = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status = ?",
        (to_status, resolved_by, payment_id, from_status),
    )
    return cur.rowcount == 1


# ------------------------------- Propinas ----------------------------------
async def create_tip(
    user_id: int, amount: int, charge_id: str, payload: str | None = None, currency: str = "XTR"
) -> int | None:
    """Registra una propina. Devuelve su id, o None si ese charge_id ya existía (actualización duplicada)."""
    cur = await _write(
        "INSERT OR IGNORE INTO tips (user_id, amount, currency, payload, charge_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, currency, payload, charge_id),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


async def get_user_tip_total(user_id: int) -> int:
    row = await _fetchone("SELECT COALESCE(SUM(amount), 0) AS total FROM tips WHERE user_id = ?", (user_id,))
    return int(row["total"]) if row else 0


async def get_tips_total() -> int:
    row = await _fetchone("SELECT COALESCE(SUM(amount), 0) AS total FROM tips")
    return int(row["total"]) if row else 0


# ------------------------------- Membresías --------------------------------
async def get_active_membership(user_id: int, product: str) -> dict[str, Any] | None:
    return await _fetchone(
        "SELECT * FROM memberships WHERE user_id = ? AND product = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (user_id, product),
    )


async def save_membership(
    membership_id: int | None, user_id: int, product: str, chat_id: int, expires_at: int | None, payment_id: int | None
) -> int:
    """Crea la membresía o actualiza su vencimiento (renovación)."""
    if membership_id:
        await _write(
            "UPDATE memberships SET expires_at = ?, payment_id = COALESCE(?, payment_id), reminded = 0, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (expires_at, payment_id, membership_id),
        )
        return membership_id
    cur = await _write(
        "INSERT INTO memberships (user_id, product, chat_id, expires_at, payment_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, product, chat_id, expires_at, payment_id),
    )
    return cur.lastrowid


async def get_expired_memberships(now: int | None = None) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT * FROM memberships WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
        (now or int(time.time()),),
    )


async def get_memberships_to_remind(window_seconds: int, now: int | None = None) -> list[dict[str, Any]]:
    """Membresías activas que vencen dentro de la ventana y aún no recibieron recordatorio."""
    now = now or int(time.time())
    return await _fetchall(
        "SELECT * FROM memberships WHERE status = 'active' AND reminded = 0 AND expires_at IS NOT NULL "
        "AND expires_at > ? AND expires_at <= ?",
        (now, now + window_seconds),
    )


async def mark_reminded(membership_id: int) -> bool:
    cur = await _write("UPDATE memberships SET reminded = 1 WHERE id = ? AND reminded = 0", (membership_id,))
    return cur.rowcount == 1


async def count_active_members(product: str) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM memberships WHERE product = ? AND status = 'active'", (product,)
    )
    return int(row["n"]) if row else 0


async def get_user_active_memberships(user_id: int) -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM memberships WHERE user_id = ? AND status = 'active'", (user_id,))


async def get_active_memberships_in_chat(user_id: int, chat_id: int) -> list[dict[str, Any]]:
    """Membresías activas (no vencidas) del miembro en un chat concreto (puede tener varias: creator y viewer)."""
    return await _fetchall(
        "SELECT * FROM memberships WHERE user_id = ? AND chat_id = ? AND status = 'active' "
        "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC",
        (user_id, chat_id, int(time.time())),
    )


async def list_active_members_in_chat(chat_id: int) -> list[int]:
    rows = await _fetchall(
        "SELECT DISTINCT user_id FROM memberships WHERE chat_id = ? AND status = 'active' ORDER BY user_id", (chat_id,)
    )
    return [r["user_id"] for r in rows]


async def add_allowed_channel(channel_id: int, user_id: int, added_by: int) -> int | None:
    """Autoriza un canal para un miembro. Devuelve el dueño anterior si ya pertenecía a OTRO miembro (no lo cambia)."""
    row = await _fetchone("SELECT user_id FROM allowed_channels WHERE channel_id = ?", (channel_id,))
    if row and row["user_id"] != user_id:
        return row["user_id"]
    await _write(
        "INSERT OR REPLACE INTO allowed_channels (channel_id, user_id, added_by) VALUES (?, ?, ?)",
        (channel_id, user_id, added_by),
    )
    return None


async def remove_allowed_channel(channel_id: int, user_id: int) -> bool:
    cur = await _write("DELETE FROM allowed_channels WHERE channel_id = ? AND user_id = ?", (channel_id, user_id))
    return cur.rowcount > 0


async def get_channel_owner(channel_id: int) -> int | None:
    row = await _fetchone("SELECT user_id FROM allowed_channels WHERE channel_id = ?", (channel_id,))
    return row["user_id"] if row else None


async def list_user_channels(user_id: int) -> list[int]:
    rows = await _fetchall("SELECT channel_id FROM allowed_channels WHERE user_id = ? ORDER BY added_at", (user_id,))
    return [r["channel_id"] for r in rows]


async def list_active_product_members(product_key: str) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT DISTINCT user_id, chat_id FROM memberships WHERE product = ? AND status = 'active'", (product_key,)
    )


async def count_active_by_product(chat_id: int) -> dict[str, int]:
    rows = await _fetchall(
        "SELECT product, COUNT(DISTINCT user_id) AS n FROM memberships WHERE chat_id = ? AND status = 'active' "
        "GROUP BY product", (chat_id,),
    )
    return {r["product"]: r["n"] for r in rows}


async def close_membership(membership_id: int, to_status: str) -> bool:
    cur = await _write(
        "UPDATE memberships SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'active'",
        (to_status, membership_id),
    )
    return cur.rowcount == 1


# ------------------------------- Accesos (enlaces de 1 uso) ----------------
async def create_grant(
    user_id: int, payment_id: int | None, chat_id: int, invite_link: str, expire_at: int,
    product_key: str | None = None, plan_months: int | None = None, pending_start: bool = False,
) -> int:
    cur = await _write(
        "INSERT INTO access_grants (user_id, payment_id, chat_id, invite_link, expire_at, product_key, plan_months, "
        "pending_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, payment_id, chat_id, invite_link, expire_at, product_key, plan_months, int(pending_start)),
    )
    return cur.lastrowid


async def get_grant(grant_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM access_grants WHERE id = ?", (grant_id,))


async def set_grant_message(grant_id: int, message_id: int) -> None:
    await _write("UPDATE access_grants SET delivery_message_id = ? WHERE id = ?", (message_id, grant_id))


async def get_grant_by_link(invite_link: str) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM access_grants WHERE invite_link = ?", (invite_link,))


async def get_active_grants(user_id: int, chat_id: int | None = None) -> list[dict[str, Any]]:
    if chat_id is None:
        return await _fetchall(
            "SELECT * FROM access_grants WHERE user_id = ? AND status = 'issued' ORDER BY id DESC", (user_id,)
        )
    return await _fetchall(
        "SELECT * FROM access_grants WHERE user_id = ? AND chat_id = ? AND status = 'issued' ORDER BY id DESC",
        (user_id, chat_id),
    )


async def get_expired_grants(now: int | None = None) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT * FROM access_grants WHERE status = 'issued' AND expire_at <= ?", (now or int(time.time()),)
    )


async def close_grant(grant_id: int, to_status: str) -> bool:
    """issued -> used | expired | revoked (atómico, solo una vez)."""
    cur = await _write(
        "UPDATE access_grants SET status = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'issued'",
        (to_status, grant_id),
    )
    return cur.rowcount == 1


# ------------------------------- Baneos ------------------------------------
async def ban_user(user_id: int, reason: str | None, banned_by: int | None) -> bool:
    """True si quedó baneado ahora (False si ya lo estaba)."""
    cur = await _write(
        "INSERT OR IGNORE INTO banned_users (user_id, reason, banned_by) VALUES (?, ?, ?)", (user_id, reason, banned_by)
    )
    return cur.rowcount == 1


async def unban_user(user_id: int) -> bool:
    cur = await _write("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    return cur.rowcount == 1


async def is_banned(user_id: int) -> bool:
    return await _fetchone("SELECT 1 AS x FROM banned_users WHERE user_id = ?", (user_id,)) is not None


async def get_banned_ids() -> set[int]:
    return {r["user_id"] for r in await _fetchall("SELECT user_id FROM banned_users")}


async def list_banned(limit: int = 30) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT b.*, u.full_name, u.username FROM banned_users b LEFT JOIN users u ON u.user_id = b.user_id "
        "ORDER BY b.banned_at DESC LIMIT ?",
        (limit,),
    )


async def count_banned() -> int:
    row = await _fetchone("SELECT COUNT(*) AS n FROM banned_users")
    return int(row["n"]) if row else 0


# ------------------------------- Botones (ui_buttons) ----------------------
_BUTTON_FIELDS = {"name_es", "name_en", "action_type", "target_value", "is_active", "sort_order", "same_row"}


async def get_buttons_by_action(action_type: str, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ui_buttons WHERE action_type = ?" + (" AND is_active = 1" if active_only else "")
    return await _fetchall(sql + " ORDER BY menu_id, sort_order", (action_type,))


async def get_buttons_using_link(link_key: str) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT * FROM ui_buttons WHERE action_type = 'url' AND target_value = ? ORDER BY menu_id", (f"link:{link_key}",)
    )


async def disable_buttons_targeting(action_types: tuple[str, ...], target: str) -> int:
    marks = ",".join("?" * len(action_types))
    cur = await _write(
        f"UPDATE ui_buttons SET is_active = 0 WHERE action_type IN ({marks}) AND target_value = ?",
        (*action_types, target),
    )
    return cur.rowcount


async def upgrade_legacy_buttons() -> int:
    """Convierte botones de versiones anteriores a las acciones actuales (idempotente)."""
    changes = 0
    statements = [
        ("UPDATE ui_buttons SET action_type = 'menu' WHERE action_type = 'nav'", ()),
        ("UPDATE ui_buttons SET action_type = 'receipt', target_value = 'creator' "
         "WHERE action_type = 'menu' AND target_value = 'confirm_vip'", ()),
        ("UPDATE ui_buttons SET action_type = 'receipt', target_value = '' "
         "WHERE action_type = 'menu' AND target_value = 'confirm'", ()),
        ("UPDATE ui_buttons SET action_type = 'support', target_value = '' "
         "WHERE action_type = 'menu' AND target_value = 'support' AND menu_id = 'main'", ()),
        ("DELETE FROM ui_buttons WHERE menu_id IN ('confirm_vip', 'confirm')", ()),
    ]
    for sql, params in statements:
        cur = await _db().execute(sql, params)
        changes += cur.rowcount
    for row in await _fetchall("SELECT menu_id, button_id, target_value FROM ui_buttons WHERE action_type = 'stars'"):
        product = (row["target_value"] or "creator").split(":", 1)[0]
        await _db().execute(
            "UPDATE ui_buttons SET action_type = 'subscribe', target_value = ? WHERE menu_id = ? AND button_id = ?",
            (product, row["menu_id"], row["button_id"]),
        )
        changes += 1
    # Donaciones: los rangos pasan a ser botones. Si la sección aún no los tiene, se desplazan los existentes al final.
    has_tips = await _fetchone("SELECT 1 AS x FROM ui_buttons WHERE menu_id = 'donate' AND action_type = 'tip'")
    if not has_tips:
        await _db().execute("UPDATE ui_buttons SET sort_order = sort_order + 1000 WHERE menu_id = 'donate'")
    await _db().commit()
    return changes


async def seed_buttons(defaults: dict[str, list[tuple]], paired: set[tuple[str, str]] | frozenset = frozenset()) -> int:
    """Inserta los botones de fábrica que falten (nunca pisa los editados). Devuelve cuántos añadió."""
    added = 0
    for menu_id, buttons in defaults.items():
        for index, (button_id, name_es, name_en, action, target, active) in enumerate(buttons):
            cur = await _db().execute(
                "INSERT OR IGNORE INTO ui_buttons (menu_id, button_id, name_es, name_en, action_type, target_value, "
                "is_active, sort_order, same_row) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (menu_id, button_id, name_es, name_en, action, target, active, (index + 1) * 10,
                 int((menu_id, button_id) in paired)),
            )
            added += cur.rowcount
    await _db().commit()
    return added


async def get_buttons(menu_id: str, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ui_buttons WHERE menu_id = ?" + (" AND is_active = 1" if active_only else "")
    return await _fetchall(sql + " ORDER BY sort_order, button_id", (menu_id,))


async def get_button(menu_id: str, button_id: str) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM ui_buttons WHERE menu_id = ? AND button_id = ?", (menu_id, button_id))


async def get_active_buttons_by_action(action_type: str) -> list[dict[str, Any]]:
    return await _fetchall(
        "SELECT * FROM ui_buttons WHERE action_type = ? AND is_active = 1", (action_type,)
    )


async def update_button(menu_id: str, button_id: str, **fields: Any) -> bool:
    fields = {k: v for k, v in fields.items() if k in _BUTTON_FIELDS}
    if not fields:
        return False
    assignments = ", ".join(f"{k} = ?" for k in fields)
    cur = await _write(
        f"UPDATE ui_buttons SET {assignments} WHERE menu_id = ? AND button_id = ?",
        (*fields.values(), menu_id, button_id),
    )
    return cur.rowcount == 1


async def add_button(menu_id: str, prefix: str, name_es: str, name_en: str, action: str, target: str) -> str:
    """Añade un botón personalizado (apagado) al final del menú. Devuelve su button_id."""
    rows = await get_buttons(menu_id)
    used = {r["button_id"] for r in rows}
    number = 1
    while f"{prefix}{number}" in used:
        number += 1
    button_id = f"{prefix}{number}"
    last = max((r["sort_order"] for r in rows), default=0)
    await _write(
        "INSERT INTO ui_buttons (menu_id, button_id, name_es, name_en, action_type, target_value, is_active, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (menu_id, button_id, name_es, name_en, action, target, last + 10),
    )
    return button_id


async def delete_button(menu_id: str, button_id: str) -> bool:
    cur = await _write("DELETE FROM ui_buttons WHERE menu_id = ? AND button_id = ?", (menu_id, button_id))
    return cur.rowcount == 1


async def move_button_up(menu_id: str, button_id: str) -> bool:
    rows = await get_buttons(menu_id)
    ids = [r["button_id"] for r in rows]
    if button_id not in ids or ids.index(button_id) == 0:
        return False
    i = ids.index(button_id)
    ids[i - 1], ids[i] = ids[i], ids[i - 1]
    for order, bid in enumerate(ids, start=1):
        await _db().execute(
            "UPDATE ui_buttons SET sort_order = ? WHERE menu_id = ? AND button_id = ?", (order * 10, menu_id, bid)
        )
    await _db().commit()
    return True


# ------------------------------- Clones autorizados ------------------------
async def create_clone(data: dict[str, Any], created_by: int | None) -> int:
    cur = await _write(
        "INSERT INTO bot_clones (bot_id, bot_username, bot_token, admin_group_id, vip_chat_id, sales_log_id, "
        "tips_log_id, admin_ids, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["bot_id"], data["bot_username"], data["bot_token"], data["admin_group_id"], data["vip_chat_id"],
            data["sales_log_id"], data["tips_log_id"], data["admin_ids"], created_by,
        ),
    )
    return cur.lastrowid


async def list_clones() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM bot_clones ORDER BY id DESC")


async def get_clone(clone_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM bot_clones WHERE id = ?", (clone_id,))


async def clone_exists(bot_id: int) -> bool:
    return await _fetchone("SELECT 1 AS x FROM bot_clones WHERE bot_id = ?", (bot_id,)) is not None


async def set_clone_status(clone_id: int, status: str) -> bool:
    cur = await _write("UPDATE bot_clones SET status = ? WHERE id = ?", (status, clone_id))
    return cur.rowcount == 1


async def delete_clone(clone_id: int) -> bool:
    cur = await _write("DELETE FROM bot_clones WHERE id = ?", (clone_id,))
    return cur.rowcount == 1


async def count_clones() -> int:
    row = await _fetchone("SELECT COUNT(*) AS n FROM bot_clones")
    return int(row["n"]) if row else 0


# ------------------------------- Productos y planes ------------------------
async def list_products() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM products ORDER BY created_at, key")


async def get_product(key: str) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM products WHERE key = ?", (key,))


async def get_product_by_chat(chat_id: int) -> dict[str, Any] | None:
    """Compatibilidad: primer producto del chat. Usa get_products_by_chat() cuando el chat tiene varios."""
    return await _fetchone("SELECT * FROM products WHERE chat_id = ? ORDER BY created_at, key LIMIT 1", (chat_id,))


async def get_products_by_chat(chat_id: int) -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM products WHERE chat_id = ? ORDER BY created_at, key", (chat_id,))


async def create_product(key: str, chat_id: int, chat_type: str, name_es: str, name_en: str,
                         can_invite: bool, can_restrict: bool, plan_months: tuple[int, ...],
                         member_role: str = "none") -> None:
    await _db().execute(
        "INSERT INTO products (key, chat_id, chat_type, name_es, name_en, is_active, can_invite, can_restrict, "
        "member_role) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (key, chat_id, chat_type, name_es, name_en, int(can_invite), int(can_restrict), member_role),
    )
    for months in plan_months:
        await _db().execute(
            "INSERT OR IGNORE INTO product_plans (product_key, months) VALUES (?, ?)", (key, months)
        )
    await _db().commit()


_PRODUCT_FIELDS = {"name_es", "name_en", "is_active", "can_invite", "can_restrict", "chat_type", "chat_id", "member_role"}


async def update_product(key: str, **fields: Any) -> bool:
    fields = {k: v for k, v in fields.items() if k in _PRODUCT_FIELDS}
    if not fields:
        return False
    cur = await _write(
        f"UPDATE products SET {', '.join(f'{k} = ?' for k in fields)} WHERE key = ?", (*fields.values(), key)
    )
    return cur.rowcount == 1


async def delete_product(key: str) -> None:
    await _db().execute("DELETE FROM product_plans WHERE product_key = ?", (key,))
    await _db().execute("DELETE FROM products WHERE key = ?", (key,))
    await _db().commit()


async def product_key_exists(key: str) -> bool:
    return await _fetchone("SELECT 1 AS x FROM products WHERE key = ?", (key,)) is not None


async def list_plans() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM product_plans ORDER BY product_key, months")


_PLAN_FIELDS = {"stars_price", "ext_price", "is_active"}


async def update_plan(product_key: str, months: int, **fields: Any) -> bool:
    fields = {k: v for k, v in fields.items() if k in _PLAN_FIELDS}
    await _db().execute(
        "INSERT OR IGNORE INTO product_plans (product_key, months) VALUES (?, ?)", (product_key, months)
    )
    cur = await _db().execute(
        f"UPDATE product_plans SET {', '.join(f'{k} = ?' for k in fields)} WHERE product_key = ? AND months = ?",
        (*fields.values(), product_key, months),
    )
    await _db().commit()
    return cur.rowcount == 1


async def has_active_plans(product_key: str) -> bool:
    return await _fetchone(
        "SELECT 1 AS x FROM product_plans WHERE product_key = ? AND is_active = 1", (product_key,)
    ) is not None


async def seed_products(seeds: list[tuple[str, int, str, str]], plan_months: tuple[int, ...],
                        roles: dict[str, str] | None = None) -> list[str]:
    """Primera ejecución: crea los productos semilla (varios pueden compartir chat). Idempotente.
    Devuelve las claves creadas ahora (para sembrar sus planes iniciales)."""
    added: list[str] = []
    roles = roles or {}
    for key, chat_id, name_es, name_en in seeds:
        if not chat_id or await product_key_exists(key):
            continue
        await create_product(key, chat_id, "", name_es, name_en, False, False, plan_months, roles.get(key, "none"))
        added.append(key)
    return added


# ------------------------------- Chats detectados --------------------------
async def upsert_linked_chat(chat_id: int, title: str, chat_type: str, status: str,
                             can_invite: bool, can_restrict: bool, can_post: bool) -> None:
    await _write(
        """
        INSERT INTO linked_chats (chat_id, title, chat_type, status, can_invite, can_restrict, can_post)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, chat_type = excluded.chat_type,
            status = excluded.status, can_invite = excluded.can_invite, can_restrict = excluded.can_restrict,
            can_post = excluded.can_post, updated_at = CURRENT_TIMESTAMP
        """,
        (chat_id, title, chat_type, status, int(can_invite), int(can_restrict), int(can_post)),
    )


async def get_linked_chat(chat_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM linked_chats WHERE chat_id = ?", (chat_id,))


async def list_admin_chats() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM linked_chats WHERE status = 'administrator' ORDER BY updated_at DESC")


# ------------------------------- Secciones ---------------------------------
async def seed_menus(system_menus: dict[str, tuple[str, str | None, str]]) -> int:
    added = 0
    for menu_id, (_text_key, parent, title) in system_menus.items():
        cur = await _db().execute(
            "INSERT OR IGNORE INTO ui_menus (menu_id, title, parent, is_system) VALUES (?, ?, ?, 1)",
            (menu_id, title, parent),
        )
        added += cur.rowcount
    await _db().commit()
    return added


async def list_menus() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM ui_menus ORDER BY is_system DESC, created_at, menu_id")


async def create_menu(prefix: str, title: str, parent: str) -> str:
    used = {r["menu_id"] for r in await _fetchall("SELECT menu_id FROM ui_menus")}
    number = 1
    while f"{prefix}{number}" in used:
        number += 1
    menu_id = f"{prefix}{number}"
    await _write(
        "INSERT INTO ui_menus (menu_id, title, parent, text_es, text_en, is_system) VALUES (?, ?, ?, ?, ?, 0)",
        (menu_id, title, parent, title, title),
    )
    return menu_id


_MENU_FIELDS = {"title", "parent", "text_es", "text_en", "media"}


async def update_menu(menu_id: str, **fields: Any) -> bool:
    fields = {k: v for k, v in fields.items() if k in _MENU_FIELDS}
    if not fields:
        return False
    cur = await _write(
        f"UPDATE ui_menus SET {', '.join(f'{k} = ?' for k in fields)} WHERE menu_id = ?", (*fields.values(), menu_id)
    )
    return cur.rowcount == 1


async def delete_menu(menu_id: str) -> None:
    await _db().execute("DELETE FROM ui_buttons WHERE menu_id = ?", (menu_id,))
    await _db().execute("DELETE FROM ui_menus WHERE menu_id = ? AND is_system = 0", (menu_id,))
    await _db().execute(
        "UPDATE ui_buttons SET is_active = 0 WHERE action_type = 'menu' AND target_value = ?", (menu_id,)
    )
    await _db().commit()


# ------------------------------- Enlaces personalizados --------------------
async def list_custom_links() -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM custom_links ORDER BY created_at, key")


async def upsert_custom_link(key: str, label: str, url: str) -> None:
    await _write(
        "INSERT INTO custom_links (key, label, url) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label = excluded.label, url = excluded.url",
        (key, label, url),
    )


async def delete_custom_link(key: str) -> None:
    await _write("DELETE FROM custom_links WHERE key = ?", (key,))


# ------------------------------- Publicaciones -----------------------------
async def create_post(kind: str, text: str, media: str, buttons: str, target: str, send_at: int | None,
                      repeat_hours: int, status: str, created_by: int | None) -> int:
    cur = await _write(
        "INSERT INTO posts (kind, text, media, buttons, target, send_at, repeat_hours, status, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, text, media, buttons, target, send_at, repeat_hours, status, created_by),
    )
    return cur.lastrowid


async def get_post(post_id: int) -> dict[str, Any] | None:
    return await _fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))


async def list_posts(kind: str, limit: int = 15) -> list[dict[str, Any]]:
    return await _fetchall("SELECT * FROM posts WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))


async def count_scheduled_posts() -> int:
    row = await _fetchone("SELECT COUNT(*) AS n FROM posts WHERE status = 'scheduled'")
    return int(row["n"]) if row else 0


async def claim_due_posts(now: int | None = None) -> list[dict[str, Any]]:
    """Publicaciones programadas que ya toca enviar; se marcan 'sending' para no duplicarlas."""
    now = now or int(time.time())
    rows = await _fetchall(
        "SELECT * FROM posts WHERE status = 'scheduled' AND send_at IS NOT NULL AND send_at <= ?", (now,)
    )
    claimed = []
    for row in rows:
        cur = await _db().execute(
            "UPDATE posts SET status = 'sending' WHERE id = ? AND status = 'scheduled'", (row["id"],)
        )
        if cur.rowcount == 1:
            claimed.append(row)
    await _db().commit()
    return claimed


async def finish_post(post_id: int, sent: int, failed: int, next_send_at: int | None) -> None:
    await _write(
        "UPDATE posts SET sent_count = sent_count + ?, fail_count = fail_count + ?, last_sent_at = CURRENT_TIMESTAMP, "
        "status = CASE WHEN ? IS NULL THEN 'sent' ELSE 'scheduled' END, send_at = COALESCE(?, send_at) WHERE id = ?",
        (sent, failed, next_send_at, next_send_at, post_id),
    )


async def set_post_status(post_id: int, status: str, send_at: int | None = None) -> None:
    await _write(
        "UPDATE posts SET status = ?, send_at = COALESCE(?, send_at) WHERE id = ?", (status, send_at, post_id)
    )


async def delete_post(post_id: int) -> None:
    await _write("DELETE FROM posts WHERE id = ?", (post_id,))


# ------------------------------- Migración de versiones anteriores ---------
async def pop_legacy_config(keys: tuple[str, ...]) -> dict[str, str]:
    """Lee y borra de bot_config ajustes que ya no existen (precios/medios de la versión anterior)."""
    rows = await get_all_config()
    found = {k: rows[k]["value"] for k in keys if k in rows}
    for key in found:
        await _db().execute("DELETE FROM bot_config WHERE key = ?", (key,))
    await _db().commit()
    return found
