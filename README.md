# @BetaHouse_bot — Bot de gestión de BETA HOUSE VIP (aiogram 3 + SQLite)

Bot que explica cómo funciona **BETA HOUSE** y cobra las suscripciones de sus dos tipos de miembro, ambos dentro del
**mismo supergrupo VIP**:

| Rol | Qué puede hacer en el grupo | Producto |
|---|---|---|
| 🔥 **Creador** | Publicar promociones con enlaces, textos, fotos y videos; prioridad en videochats | `creator` |
| 🎟️ **Espectador** | Solo lectura (ver promos, contactar creadores por privado, entrar a videochats) | `viewer` |

El bot identifica a cada miembro por lo que pagó y, al confirmar el pago, le da acceso al grupo y **activa los
permisos de su rol automáticamente**.

Basado en Alpha Pig Bros: misma arquitectura, mismo `/panel`, misma regla de oro (los handlers y servicios no
contienen textos, precios ni enlaces: todo vive en `config.py`, `.env` y la base de datos, editable en caliente).

## Estructura
```
config.py                      Textos ES/EN, productos y roles, planes semilla, secciones y botones, validación
database.py                    SQLite (WAL): miembros, temas, pagos, membresías, productos (varios por chat), planes…
bot.py                         Arranque, migración, siembra del catálogo, middlewares, tareas periódicas
handlers/user_handlers.py      Miembros: idioma, bienvenida, secciones, planes, pagos, comprobantes, soporte
handlers/admin_handlers.py     Grupo de gestión: aprobar, /aprobar, /rol, solicitudes de ingreso, entradas al grupo
handlers/group_handlers.py     Moderación por rol dentro de BETA HOUSE (red de seguridad)
handlers/panel/                /panel — un archivo por módulo
services/roles.py              Roles: permisos de creador / espectador, rol efectivo, sincronización
services/access.py             Enlaces personales, aprobación de ingreso, renovaciones, cambios de rol, vencimientos
services/catalog.py            Productos, planes, vinculación de chats, verificación de permisos
services/menus.py · broadcast.py · moderation.py · telegram_service.py
```

## Flujo del miembro
1. `/start` → selector de idioma 🇪🇸 / 🇺🇸 → **bienvenida** (editable) con los botones:
   - `👇 SUSCRÍBETE EN LOS SIGUIENTES BOTONES 👇` (divisor: solo texto, al pulsarlo muestra un aviso breve)
   - `🔥 SUSCRÍBETE COMO CREADOR` · `🎟️ SUSCRÍBETE COMO ESPECTADOR` (misma fila)
   - `⭐ DONACIONES`
   - `💎 SUSCRIPCIÓN CON PAYPAL Y CONFIRMACIÓN`
   - `💬 CONTACTA DIRECTAMENTE CON LOS CREADORES`
   - `🌐 Idioma / Language` (cambia todo el bot de idioma y vuelve a mostrar la bienvenida traducida)
2. Sección **Creador** / **Espectador**: explicación + `⭐ Telegram Stars` · `💎 PayPal` · `📸 Confirmar pago PayPal`,
   cada una con sus propios planes y precios.
3. Stars → acceso automático. PayPal → captura → el grupo de gestión ve **tipo + plan + precio** → «✅ Aprobar».
4. El bot envía un enlace personal (solicitud de ingreso: solo entra quien pagó). Al entrar, **aplica el rol**:
   creador = puede publicar; espectador = solo lectura. Le avisa por privado y deja constancia en su Tema.

## Roles y permisos en el grupo
- **Rol efectivo:** si el miembro tiene varias suscripciones activas en el grupo, manda la mayor (creador > espectador).
  - Espectador dentro del grupo que paga el plan de creador → se activa al instante, sin enlace nuevo, y se le dan
    permisos de publicación.
  - Vence el plan de creador pero sigue activo el de espectador → **no se le expulsa**: pasa a solo lectura.
  - Vence la última suscripción → se le saca del grupo (sin vetarlo).
- **Red de seguridad:** si alguien que no es creador publica algo (restricción no aplicada, entró por otra vía,
  mensaje «enviado como canal»…), el bot lo borra, le vuelve a aplicar su rol y le explica por privado por qué
  (máximo un aviso por hora). Nunca toca mensajes de administradores, del canal vinculado ni de servicio.
- Quien está en el grupo **sin suscripción** (y no es admin) se trata como espectador.
- **Permisos del espectador** (`/panel → 🛒 Suscripciones → ⚙️ Ajustes → 🎟️ Permisos del espectador`):
  `solo lectura` (recomendado) o `solo texto` (puede escribir, pero sin multimedia; el bot borra sus enlaces).
- **Videochats:** los espectadores restringidos pueden entrar a los videochats del grupo. Si quieres que hablen
  o abran cámara, los admins pueden darles la palabra dentro del videochat.

### ⚠️ Configuración obligatoria del grupo BETA HOUSE
1. **Ajustes del grupo → Permisos:** deja activados para todos los miembros *Enviar mensajes*, *Multimedia*
   (fotos, videos…) y *Vistas previas de enlaces*. Telegram no permite que una excepción individual dé más
   permisos que los generales: si el grupo es de solo lectura, los creadores tampoco podrían publicar.
   El bot restringe a cada espectador de forma individual al entrar.
2. **Bot administrador** con: *Invitar usuarios*, *Restringir/expulsar miembros* y *Eliminar mensajes*.
   `/panel → 🛒 → producto → 🔍 Verificar permisos` comprueba ambos puntos.

## /panel (solo ADMIN_IDS, en privado)
Igual que Alpha Pig Bros, más:
- **🛒 Suscripciones y productos:** `creator` y `viewer` comparten el chat de BETA HOUSE. En la ficha de cada producto:
  `🎭 Cambiar rol` (creador → espectador → sin gestión) y `🔄 Sincronizar permisos` (reaplica a todos los miembros
  activos). En «Chats detectados» puedes crear **otra tarifa/rol sobre un chat ya vinculado**.
- **📝 Textos:** nueva categoría «🎭 Roles en el grupo» (avisos de permisos de creador / espectador).
- **🔘 Botones:** nueva acción `📌 Encabezado / Divisor (solo texto)`. Los nombres admiten variables de precio:
  `{creator_paypal}` `{creator_stars}` `{viewer_paypal}` `{viewer_stars}` (también en todos los textos).

## Comandos (se registran solos al arrancar; no hace falta @BotFather)
| Quién | Dónde | Comandos |
|---|---|---|
| Miembros | Chat privado | `/start` `/menu` `/creador` `/espectador` `/pagar` `/mi_plan` `/soporte` `/donar` `/idioma` |
| Admins | Su chat privado | Todo lo anterior + `/panel` `/estado` `/sincronizar` |
| Equipo | Tema del miembro en el grupo de gestión | `/aprobar creador 3` · `/rol` · `/canal -100…` · `/revocar` · `/cerrar` · `/ban motivo` · `/unban` |

Las descripciones del menú «/» salen en español o inglés según la app de cada persona. El menú de admin aparece en
cuanto el admin escribe `/start` o `/panel` al bot (si no lo ves, cierra y vuelve a abrir el chat).

## Diagnóstico y correcciones
- `/estado`: permisos del bot y del grupo, Temas del grupo de gestión, canales de registro, enlaces heredados de
  Alpha Pig Bros, promoción cruzada anunciada, planes con su valor neto estimado en USD y ajustes clave.
  - **🛠 Abrir permisos generales**: si el grupo es de solo lectura, primero restringe a todos los espectadores y
    después abre mensajes, multimedia y enlaces (conserva fijar, invitar y editar información).
  - **🔄 Sincronizar permisos** (= `/sincronizar`): reaplica el rol de todos los miembros activos.
- Al arrancar, si hay puntos por revisar, el bot avisa por privado a los admins.
- **Enlaces heredados**: para que dejen de aparecer, cámbialos o guárdalos de nuevo (aunque sea el mismo valor) en
  `/panel → 🔗 Enlaces`.
- **Promoción cruzada**: texto `cross_promo` (ES/EN) en `/panel → 📝 Textos → Bienvenida`; se inserta en la
  bienvenida y en la sección de creadores con `{cross_promo}`.
- **Stars → USD**: en la ficha de cada plan se ve el neto estimado y cuántas ⭐ cobrar para recibir el precio de
  PayPal. La tasa se ajusta en `⚙️ Ajustes → 💱 Tasa de retiro de Stars` (por defecto 13 USD por 1000 ⭐).
- **Canales de creadores**: un mensaje «como canal» se borra salvo que el canal esté autorizado con `/canal -100…`
  en el Tema del creador y este tenga plan de creador activo. Al borrar uno no autorizado, el bot avisa al equipo
  con el comando listo para autorizarlo.
- **Rol «sin gestión»**: al pasar un producto a esa opción, el bot quita las restricciones que había puesto a sus
  miembros (o les deja el rol que les quede por otra suscripción).
- **Videochats**: al empezar un videochat en el grupo, el bot avisa por privado a todos los miembros activos con un
  botón para ir (como máximo una vez cada 30 min). Se apaga en `⚙️ Ajustes → 🔴 Aviso de videochat`.

## Primer arranque
Crea los productos `creator` y `viewer` sobre `BETA_HOUSE_CHAT_ID` con sus planes de 1 mes activos
(creador 1050 ⭐ / 15 $ USD · espectador 350 ⭐ / 5 $ USD, configurables en `.env`). Los planes de 3, 6, 12 meses y
vitalicio quedan creados y apagados para activarlos desde el panel. Los arranques siguientes nunca pisan lo editado.

Si reutilizas una base de datos de Alpha Pig Bros, la migración elimina automáticamente la restricción
«un producto por chat» conservando todos los datos.

## Despliegue (Railway)
Volume en `/data` + `DB_PATH=/data/bot_data.db`. **1 sola réplica.** Variables en `.env.example`.
Usa un **bot nuevo** de @BotFather para Beta House.
