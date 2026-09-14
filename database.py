"""
database.py
Módulo de acceso a datos para MasterPlacas.db

Responsabilidad única: inicializar el esquema y exponer funciones CRUD
básicas sobre las tablas vehiculos, usuarios y logs_auditoria.

No contiene lógica de scraping ni de consulta a portales externos.
Toda la información de vehículos se ingresa manualmente por usuarios
autorizados (Admin/Operador) a través del bot/dashboard.
"""

import secrets
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "MasterPlacas.db"

# Valores permitidos (se validan en Python porque SQLite no tiene ENUM nativo)
TIPOS_VEHICULO = ("carro", "moto")
ESTADOS_VEHICULO = ("activo", "baja")
ROLES_USUARIO = ("Admin", "Operador")
ESTADOS_USUARIO = ("activo", "inactivo")

# Etapa 4.6: el Grado Operativo pertenece al Operador (nunca a la placa) y
# el estado del servicio CAMARA es independiente del estado del usuario
# (ver camara.py para la lógica de cálculo de ambos).
GRADOS_OPERATIVOS = ("Grado 1", "Grado 2", "Grado 3")
ESTADOS_CAMARA = ("ACTIVA", "EN_ALERTA", "SUSPENDIDA")

# Tipos de evento que camara.py/usuarios.py encolan en `eventos_notificacion`
# para que el futuro bot de Telegram los consuma. No se implementa ningún
# consumidor todavía (Etapa 4.6 §27): esto es solo la cola/registro.
TIPOS_EVENTO_NOTIFICACION = (
    "USER_DEACTIVATED",
    "USER_REACTIVATED",
    "CAMERA_WARNING",
    "CAMERA_SUSPENDED",
    "CAMERA_REACTIVATED",
    "CAMERA_ACTIVITY_REGISTERED",
)

# Etapa 5: Gestión Masiva. Ver gestion_masiva.py para la lógica de
# períodos, estados de información y cola de trabajo; estas tuplas solo
# fijan los valores permitidos (mismo patrón que el resto del archivo).
ESTADOS_INFORMACION_PLACA = ("ACTUALIZADA", "PENDIENTE", "SIN_DATOS")
ESTADOS_ESTUDIO = ("EN_PROGRESO", "PAUSADO", "CANCELADO", "COMPLETADO")
ESTADOS_ITEM_COLA = ("PENDIENTE", "PROCESANDO", "COMPLETADO", "ERROR")

# Un snapshot se considera "vigente" (ACTUALIZADA) si tiene menos de este
# número de días; más viejo que esto es PENDIENTE de actualizar. La Etapa
# 4.6 no definió ningún umbral de vigencia, así que se documenta aquí como
# la interpretación adoptada: reutiliza el mismo umbral de 30 días que ya
# se usa para el Grado Operativo (Etapa 4.6 §12), por consistencia y para
# no inventar un número arbitrario nuevo. Es un valor puro de lectura (no
# se persiste), así que cambiarlo no requiere ninguna migración.
VIGENCIA_INFORMACION_DIAS = 30

# Duración de una sesión de login (horas) antes de requerir volver a iniciar sesión.
DURACION_SESION_HORAS = 12


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------

@contextmanager
def get_connection():
    """
    Context manager para obtener una conexión SQLite con:
    - row_factory como sqlite3.Row (acceso a columnas por nombre)
    - claves foráneas habilitadas (SQLite las trae desactivadas por defecto)
    - commit automático al salir sin errores, rollback si hay excepción
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    nombre      TEXT NOT NULL,
    rol         TEXT NOT NULL CHECK (rol IN ('Admin', 'Operador')),
    estado      TEXT NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo', 'inactivo')),
    fecha_creacion TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS vehiculos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    placa          TEXT NOT NULL UNIQUE,
    tipo_vehiculo  TEXT NOT NULL CHECK (tipo_vehiculo IN ('carro', 'moto')),
    estado         TEXT NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo', 'baja')),
    fecha_ingreso  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    fecha_baja     TEXT,
    observaciones  TEXT
);

CREATE TABLE IF NOT EXISTS logs_auditoria (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id     INTEGER NOT NULL,
    accion         TEXT NOT NULL,
    placa_afectada TEXT,
    fecha_hora     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
);

CREATE TABLE IF NOT EXISTS sesiones (
    token       TEXT PRIMARY KEY,
    usuario_id  INTEGER NOT NULL,
    creado_en   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    expira_en   TEXT NOT NULL,
    FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
);

-- Etapa 4.6: cola de eventos para el futuro bot de Telegram (no hay
-- ningún consumidor todavía). Cada fila es un hecho ya ocurrido
-- (desactivación, alerta/suspensión de CAMARA, etc.) pendiente de
-- notificar; `entregado` lo marcará el consumidor cuando exista.
CREATE TABLE IF NOT EXISTS eventos_notificacion (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id  INTEGER NOT NULL,
    tipo_evento TEXT NOT NULL,
    datos       TEXT,
    creado_en   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    entregado   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
);

-- Etapa 5: snapshots de información externa por placa. Cada consulta
-- (futura: RGM/Judicial; hoy: solo lo que un Admin registre manualmente,
-- ya que ninguna fuente externa real está implementada todavía) es un
-- INSERT nuevo, nunca un UPDATE: el historial anterior nunca se destruye
-- (Etapa 5 §30). El estado "vigente" de una placa se calcula a partir del
-- snapshot más reciente (ver gestion_masiva.calcular_estado_informacion),
-- nunca se guarda como columna aparte para no desincronizarse.
CREATE TABLE IF NOT EXISTS snapshots_placa (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    placa       TEXT NOT NULL,
    fuente      TEXT NOT NULL,
    datos       TEXT,
    creado_en   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (placa) REFERENCES vehiculos (placa)
);

-- Etapa 5: Job Manager de estudios masivos. Un estudio agrupa una cola de
-- placas a estudiar (cola_estudio) armada a partir de un período+filtro
-- concretos. NO hay ningún proceso en segundo plano: el progreso avanza
-- por lotes acotados vía peticiones HTTP explícitas (ver
-- gestion_masiva.procesar_siguiente_lote), nunca un loop gigante dentro
-- de una sola petición.
CREATE TABLE IF NOT EXISTS estudios_masivos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id       INTEGER NOT NULL,
    periodo        TEXT NOT NULL,
    filtro_estado  TEXT,
    busqueda       TEXT,
    total_placas   INTEGER NOT NULL DEFAULT 0,
    procesadas     INTEGER NOT NULL DEFAULT 0,
    con_error      INTEGER NOT NULL DEFAULT 0,
    estado         TEXT NOT NULL DEFAULT 'EN_PROGRESO',
    creado_en      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    actualizado_en TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (admin_id) REFERENCES usuarios (id)
);

CREATE TABLE IF NOT EXISTS cola_estudio (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    estudio_id     INTEGER NOT NULL,
    placa          TEXT NOT NULL,
    prioridad      INTEGER NOT NULL DEFAULT 0,
    orden          INTEGER NOT NULL,
    estado         TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos       INTEGER NOT NULL DEFAULT 0,
    procesado_en   TEXT,
    error_detalle  TEXT,
    FOREIGN KEY (estudio_id) REFERENCES estudios_masivos (id)
);

-- Índices para las búsquedas más frecuentes
CREATE INDEX IF NOT EXISTS idx_vehiculos_placa  ON vehiculos (placa);
CREATE INDEX IF NOT EXISTS idx_vehiculos_estado ON vehiculos (estado);
CREATE INDEX IF NOT EXISTS idx_logs_usuario     ON logs_auditoria (usuario_id);
CREATE INDEX IF NOT EXISTS idx_logs_placa       ON logs_auditoria (placa_afectada);
CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones (usuario_id);
CREATE INDEX IF NOT EXISTS idx_eventos_usuario   ON eventos_notificacion (usuario_id);
CREATE INDEX IF NOT EXISTS idx_eventos_entregado ON eventos_notificacion (entregado);
CREATE INDEX IF NOT EXISTS idx_snapshots_placa   ON snapshots_placa (placa);
CREATE INDEX IF NOT EXISTS idx_cola_estudio_id   ON cola_estudio (estudio_id, estado, prioridad, orden);
"""


def _migrar_columnas_login(conn):
    """
    Migración aditiva: agrega `username` y `password_hash` a `usuarios` si
    todavía no existen. No borra ni modifica ninguna fila existente; los
    usuarios creados antes de esta etapa simplemente quedan con estos dos
    campos en NULL (pueden seguir usándose para el bot, pero no podrán
    iniciar sesión en el dashboard web hasta que se les asignen credenciales
    con crear_admin.py o vía /usuarios/alta).
    """
    columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(usuarios)")}

    if "username" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN username TEXT")
    if "password_hash" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN password_hash TEXT")

    # UNIQUE se crea como índice aparte: SQLite no permite agregar la
    # restricción UNIQUE directamente en un ALTER TABLE ADD COLUMN.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_username ON usuarios (username)"
    )


def _migrar_columnas_logs(conn):
    """
    Migración aditiva: agrega `observaciones` a `logs_auditoria` si todavía
    no existe. Los eventos ya registrados quedan con este campo en NULL
    (no había forma de saber su observación original); los eventos nuevos
    de alta/baja sí la guardan, para que el Historial (Etapa 4) pueda
    mostrarla sin inventar una segunda tabla de historial.
    """
    columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(logs_auditoria)")}
    if "observaciones" not in columnas:
        conn.execute("ALTER TABLE logs_auditoria ADD COLUMN observaciones TEXT")


def _migrar_rol_operador():
    """
    Migración Etapa 4.5: renombra el rol 'Colaborador' a 'Operador' en todo
    el sistema.

    SQLite no permite modificar un CHECK constraint con ALTER TABLE, así
    que hace falta el procedimiento oficial de SQLite para cambios de
    esquema: crear una tabla nueva con el CHECK actualizado, copiar todas
    las filas conservando exactamente su `id` (para no romper las
    referencias existentes desde sesiones.usuario_id ni
    logs_auditoria.usuario_id), eliminar la tabla vieja y renombrar la
    nueva. No se pierde ningún usuario, columna ni historial.

    Usa su propia conexión (en vez de reutilizar get_connection) porque
    necesita desactivar temporalmente `PRAGMA foreign_keys` durante la
    reconstrucción: SQLite exige que ese pragma se cambie fuera de una
    transacción, y get_connection lo deja siempre en ON.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        fila = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='usuarios'"
        ).fetchone()
        if fila is None or "'Colaborador'" not in fila["sql"]:
            # Tabla nueva (ya creada con el CHECK actual) o ya migrada
            # en una ejecución anterior: no hay nada que hacer.
            return

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE usuarios_nueva (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL UNIQUE,
                nombre         TEXT NOT NULL,
                rol            TEXT NOT NULL CHECK (rol IN ('Admin', 'Operador')),
                estado         TEXT NOT NULL DEFAULT 'activo' CHECK (estado IN ('activo', 'inactivo')),
                fecha_creacion TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                username       TEXT,
                password_hash  TEXT
            )
            """
        )
        # El rol se convierte aquí, dentro del SELECT que alimenta la
        # tabla nueva: un UPDATE previo sobre la tabla vieja fallaría,
        # porque su CHECK original todavía solo permite
        # ('Admin', 'Colaborador') y rechazaría el valor 'Operador'.
        conn.execute(
            """
            INSERT INTO usuarios_nueva
                (id, telegram_id, nombre, rol, estado, fecha_creacion, username, password_hash)
            SELECT
                id, telegram_id, nombre,
                CASE WHEN rol = 'Colaborador' THEN 'Operador' ELSE rol END,
                estado, fecha_creacion, username, password_hash
            FROM usuarios
            """
        )
        conn.execute("DROP TABLE usuarios")
        conn.execute("ALTER TABLE usuarios_nueva RENAME TO usuarios")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_username ON usuarios (username)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def _migrar_columnas_operativas(conn):
    """
    Migración aditiva (Etapa 4.6): agrega a `usuarios` las columnas de
    Grado Operativo y control de actividad CAMARA. Los usuarios existentes
    quedan con grado_operativo=NULL (no aplica a Admin; un Operador
    existente lo tendrá sin asignar hasta que el Admin lo asigne desde su
    ficha) y estado_camara='ACTIVA' por defecto: ningún usuario arranca
    en alerta o suspendido por el solo hecho de migrar.
    """
    columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(usuarios)")}
    if "grado_operativo" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN grado_operativo TEXT")
    if "estado_camara" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN estado_camara TEXT NOT NULL DEFAULT 'ACTIVA'")
    if "ultima_actividad_camara" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN ultima_actividad_camara TEXT")
    if "ultimo_acceso" not in columnas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN ultimo_acceso TEXT")


def _migrar_codigo_operativo(conn):
    """
    Migración aditiva (Etapa 5 §35): agrega `codigo_operativo`, un código
    de 4 dígitos independiente del id interno de SQLite, asignado
    secuencialmente y que la desactivación lógica NUNCA libera.

    Los usuarios existentes (creados antes de esta etapa) reciben su
    código en el mismo orden en que fueron creados (id ascendente), una
    sola vez, la primera vez que esta migración corre. `crear_usuario`
    asigna el siguiente código disponible a los usuarios nuevos.
    """
    columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(usuarios)")}
    es_columna_nueva = "codigo_operativo" not in columnas
    if es_columna_nueva:
        conn.execute("ALTER TABLE usuarios ADD COLUMN codigo_operativo TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_codigo ON usuarios (codigo_operativo)"
    )
    if es_columna_nueva:
        filas = conn.execute("SELECT id FROM usuarios ORDER BY id ASC").fetchall()
        for i, fila in enumerate(filas, start=1):
            conn.execute(
                "UPDATE usuarios SET codigo_operativo = ? WHERE id = ?",
                (f"{i:04d}", fila["id"]),
            )


def inicializar_db():
    """Crea las tablas e índices si no existen, y aplica migraciones aditivas
    sobre tablas ya existentes. Es seguro llamarla varias veces."""
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrar_columnas_login(conn)
        _migrar_columnas_logs(conn)
    # Se ejecuta con conexión propia (ver docstring) y después de que
    # username/password_hash ya existan, para que la tabla reconstruida
    # las conserve.
    _migrar_rol_operador()
    # Se ejecuta DESPUÉS de _migrar_rol_operador a propósito: si esa
    # migración de rol alguna vez tuviera que reconstruir la tabla
    # `usuarios` (hoy ya no aplica, la base real quedó migrada en la
    # Etapa 4.5), su copia de columnas es una lista fija que no incluye
    # estas nuevas columnas operativas; agregarlas antes se perdería en
    # esa reconstrucción. Corriendo esta migración después, nunca hay
    # nada que perder.
    with get_connection() as conn:
        _migrar_columnas_operativas(conn)
        _migrar_codigo_operativo(conn)
    print(f"Base de datos inicializada en: {DB_PATH}")


# ---------------------------------------------------------------------------
# Excepciones propias
# ---------------------------------------------------------------------------

class PlacaDuplicadaError(Exception):
    """Se lanza al intentar insertar una placa que ya existe."""


class RegistroNoEncontradoError(Exception):
    """Se lanza cuando se busca/actualiza un registro que no existe."""


class ValorInvalidoError(Exception):
    """Se lanza cuando un valor no cumple con los valores permitidos."""


# ---------------------------------------------------------------------------
# CRUD: usuarios
# ---------------------------------------------------------------------------

def _normalizar_username(username: str):
    """
    Normaliza el username a minúsculas para que el login sea insensible a
    mayúsculas/minúsculas ('Admin', 'ADMIN' y 'admin' son la misma cuenta).
    Se aplica tanto al guardar como al buscar, así el índice UNIQUE de la
    columna también impide crear dos cuentas que solo difieran en el case.
    """
    return username.strip().lower() if username else None


def crear_usuario(
    telegram_id: int,
    nombre: str,
    rol: str,
    username: str = None,
    password_hash: str = None,
) -> int:
    """
    Registra un nuevo usuario autorizado. Devuelve el id creado.

    `username`/`password_hash` son opcionales: un usuario creado solo para
    el bot de Telegram (por ejemplo, por un caller externo a este dashboard)
    puede omitirlos y simplemente no tendrá acceso de login web hasta que
    se le asignen con `establecer_credenciales`.

    También asigna el siguiente `codigo_operativo` disponible (Etapa 5
    §35): un código de 4 dígitos independiente del id de SQLite, que la
    desactivación lógica nunca libera.
    """
    if rol not in ROLES_USUARIO:
        raise ValorInvalidoError(f"Rol inválido: {rol}. Use uno de {ROLES_USUARIO}")

    with get_connection() as conn:
        fila = conn.execute(
            "SELECT COALESCE(MAX(CAST(codigo_operativo AS INTEGER)), 0) AS maximo FROM usuarios"
        ).fetchone()
        siguiente_codigo = f"{fila['maximo'] + 1:04d}"

        cur = conn.execute(
            """INSERT INTO usuarios (telegram_id, nombre, rol, username, password_hash, codigo_operativo)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (telegram_id, nombre, rol, _normalizar_username(username), password_hash, siguiente_codigo),
        )
        return cur.lastrowid


def obtener_usuario_por_telegram_id(telegram_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def obtener_usuario_por_username(username: str):
    """
    Busca un usuario por su nombre de login web (no por telegram_id).
    Insensible a mayúsculas/minúsculas: compara con LOWER(username) en vez
    de solo confiar en que el valor guardado ya esté en minúsculas, para
    que también funcione con cuentas creadas antes de esta normalización.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE LOWER(username) = ?",
            (_normalizar_username(username),),
        ).fetchone()
        return dict(row) if row else None


def establecer_credenciales(usuario_id: int, username: str, password_hash: str):
    """
    Asigna o actualiza el username y el hash de contraseña de un usuario
    existente. Usada por crear_admin.py (ejecutado manualmente en la
    terminal) y, más adelante, por la gestión de usuarios del dashboard.
    Nunca recibe la contraseña en texto plano: siempre un hash ya calculado.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE usuarios SET username = ?, password_hash = ? WHERE id = ?",
            (_normalizar_username(username), password_hash, usuario_id),
        )
        if cur.rowcount == 0:
            raise RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")


def listar_usuarios(solo_activos: bool = True):
    query = "SELECT * FROM usuarios"
    if solo_activos:
        query += " WHERE estado = 'activo'"
    query += " ORDER BY nombre"

    with get_connection() as conn:
        return [dict(r) for r in conn.execute(query).fetchall()]


def cambiar_estado_usuario(usuario_id: int, nuevo_estado: str):
    if nuevo_estado not in ESTADOS_USUARIO:
        raise ValorInvalidoError(f"Estado inválido: {nuevo_estado}")

    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE usuarios SET estado = ? WHERE id = ?", (nuevo_estado, usuario_id)
        )
        if cur.rowcount == 0:
            raise RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")


# ---------------------------------------------------------------------------
# CRUD: vehiculos
# ---------------------------------------------------------------------------

def insertar_placa(
    placa: str,
    tipo_vehiculo: str,
    usuario_id: int,
    observaciones: str = None,
) -> int:
    """
    Inserta una nueva placa en estado 'activo' y registra el log de auditoría.
    Lanza PlacaDuplicadaError si la placa ya existe.
    """
    placa = placa.strip().upper()

    if tipo_vehiculo not in TIPOS_VEHICULO:
        raise ValorInvalidoError(
            f"Tipo de vehículo inválido. Usa uno de: {', '.join(TIPOS_VEHICULO)}."
        )

    with get_connection() as conn:
        existente = conn.execute(
            "SELECT id FROM vehiculos WHERE placa = ?", (placa,)
        ).fetchone()
        if existente:
            raise PlacaDuplicadaError(f"La placa {placa} ya existe en el sistema.")

        cur = conn.execute(
            """INSERT INTO vehiculos (placa, tipo_vehiculo, observaciones)
               VALUES (?, ?, ?)""",
            (placa, tipo_vehiculo, observaciones),
        )
        vehiculo_id = cur.lastrowid

        _registrar_log(conn, usuario_id, "ALTA_PLACA", placa, observaciones)
        return vehiculo_id


def insertar_placas_masivo(placas: list, usuario_id: int) -> dict:
    """
    Inserta varias placas a la vez.
    `placas` es una lista de dicts: {"placa": ..., "tipo_vehiculo": ..., "observaciones": ...}
    Devuelve un resumen {"insertadas": [...], "duplicadas": [...], "invalidas": [...]}
    para que la carga masiva no se detenga por un solo error.
    """
    resumen = {"insertadas": [], "duplicadas": [], "invalidas": []}

    for item in placas:
        placa = item.get("placa", "").strip().upper()
        tipo = item.get("tipo_vehiculo")
        obs = item.get("observaciones")

        try:
            insertar_placa(placa, tipo, usuario_id, obs)
            resumen["insertadas"].append(placa)
        except PlacaDuplicadaError:
            resumen["duplicadas"].append(placa)
        except ValorInvalidoError:
            resumen["invalidas"].append(placa)

    return resumen


def listar_placas_activas():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM vehiculos WHERE estado = 'activo' ORDER BY fecha_ingreso DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def buscar_placa(placa: str):
    placa = placa.strip().upper()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM vehiculos WHERE placa = ?", (placa,)
        ).fetchone()
        return dict(row) if row else None


def obtener_ficha_vehiculo(placa: str):
    """
    Ficha completa de un vehículo para /consulta: sus propios datos más
    quién lo dio de alta. `vehiculos` no guarda el usuario_id de quien
    registró la placa (por diseño: cualquier usuario_id enviado por el
    cliente sería falsificable), así que ese dato se resuelve leyendo el
    evento ALTA_PLACA correspondiente en logs_auditoria (que sí guarda un
    usuario_id de servidor, nunca de formulario). None si la placa no existe.
    """
    vehiculo = buscar_placa(placa)
    if vehiculo is None:
        return None

    with get_connection() as conn:
        fila_alta = conn.execute(
            """SELECT u.nombre FROM logs_auditoria l
               JOIN usuarios u ON u.id = l.usuario_id
               WHERE l.placa_afectada = ? AND l.accion = 'ALTA_PLACA'
               ORDER BY l.fecha_hora ASC LIMIT 1""",
            (vehiculo["placa"],),
        ).fetchone()

    vehiculo["registrado_por"] = fila_alta["nombre"] if fila_alta else None
    return vehiculo


def listar_logs_de_placa(placa: str):
    """
    Historial de una placa para /historial/{placa}: reutiliza
    logs_auditoria (no crea una tabla de historial nueva), filtrando por
    placa directamente en SQL y resolviendo el nombre del usuario con un
    JOIN, para no mostrar solo un "Usuario ID" en la interfaz. Más
    reciente primero.
    """
    placa = placa.strip().upper()
    with get_connection() as conn:
        filas = conn.execute(
            """SELECT l.*, u.nombre AS usuario_nombre, u.username AS usuario_username
               FROM logs_auditoria l
               LEFT JOIN usuarios u ON u.id = l.usuario_id
               WHERE l.placa_afectada = ?
               ORDER BY l.fecha_hora DESC""",
            (placa,),
        ).fetchall()
        return [dict(f) for f in filas]


def buscar_vehiculos(query: str = None, estado: str = None):
    """
    Lista vehículos combinando búsqueda por placa (insensible a
    mayúsculas/minúsculas, coincidencia parcial) y filtro por estado.
    Sin argumentos, equivale a listar todos ordenados por fecha de ingreso.
    Usada por el Inventario: mantiene el SQL de búsqueda/filtro en la capa
    de datos en vez de traer todas las filas y filtrar en Python o en la
    plantilla.
    """
    if estado is not None and estado not in ESTADOS_VEHICULO:
        raise ValorInvalidoError(f"Estado inválido: {estado}")

    sql = "SELECT * FROM vehiculos WHERE 1=1"
    params = []

    if query:
        sql += " AND UPPER(placa) LIKE ?"
        params.append(f"%{query.strip().upper()}%")
    if estado:
        sql += " AND estado = ?"
        params.append(estado)

    sql += " ORDER BY fecha_ingreso DESC"

    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def obtener_resumen_vehiculos():
    """
    Conteos agregados de vehículos para el Dashboard, en una sola consulta
    (evita traer todas las filas a Python solo para contarlas). Devuelve
    0 en cada campo cuando la tabla está vacía (COALESCE), nunca None.
    """
    with get_connection() as conn:
        fila = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN estado = 'activo' THEN 1 ELSE 0 END), 0) AS activos,
                COALESCE(SUM(CASE WHEN estado = 'baja' THEN 1 ELSE 0 END), 0) AS bajas,
                COALESCE(SUM(CASE WHEN estado = 'activo' AND tipo_vehiculo = 'carro' THEN 1 ELSE 0 END), 0) AS carros_activos,
                COALESCE(SUM(CASE WHEN estado = 'activo' AND tipo_vehiculo = 'moto' THEN 1 ELSE 0 END), 0) AS motos_activas
            FROM vehiculos
            """
        ).fetchone()
        return dict(fila)


def obtener_resumen_usuarios():
    """Conteos agregados de usuarios para el Dashboard, en una sola consulta."""
    with get_connection() as conn:
        fila = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN estado = 'activo' THEN 1 ELSE 0 END), 0) AS activos
            FROM usuarios
            """
        ).fetchone()
        return dict(fila)


def cambiar_estado_a_baja(placa: str, usuario_id: int, observaciones: str = None):
    """
    Da de baja una placa. Requiere confirmación manual del usuario que llama
    a esta función (no se invoca automáticamente desde ningún proceso).
    """
    placa = placa.strip().upper()

    with get_connection() as conn:
        actual = conn.execute(
            "SELECT * FROM vehiculos WHERE placa = ?", (placa,)
        ).fetchone()
        if not actual:
            raise RegistroNoEncontradoError(f"La placa {placa} no existe")
        if actual["estado"] == "baja":
            raise ValorInvalidoError(f"La placa {placa} ya está dada de baja")

        campos = ["estado = 'baja'", "fecha_baja = datetime('now', 'localtime')"]
        params = []
        if observaciones is not None:
            campos.append("observaciones = ?")
            params.append(observaciones)
        params.append(placa)

        conn.execute(
            f"UPDATE vehiculos SET {', '.join(campos)} WHERE placa = ?", params
        )
        _registrar_log(conn, usuario_id, "BAJA_PLACA", placa, observaciones)


# ---------------------------------------------------------------------------
# CRUD: logs_auditoria
# ---------------------------------------------------------------------------

def _registrar_log(conn, usuario_id: int, accion: str, placa_afectada: str = None, observaciones: str = None):
    """Versión interna que reutiliza una conexión/transacción ya abierta."""
    conn.execute(
        """INSERT INTO logs_auditoria (usuario_id, accion, placa_afectada, observaciones)
           VALUES (?, ?, ?, ?)""",
        (usuario_id, accion, placa_afectada, observaciones),
    )


def registrar_log(usuario_id: int, accion: str, placa_afectada: str = None, observaciones: str = None) -> int:
    """Versión pública: abre su propia conexión/transacción."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO logs_auditoria (usuario_id, accion, placa_afectada, observaciones)
               VALUES (?, ?, ?, ?)""",
            (usuario_id, accion, placa_afectada, observaciones),
        )
        return cur.lastrowid


def listar_logs(placa: str = None, usuario_id: int = None, limite: int = 100):
    """Lista logs de auditoría, opcionalmente filtrados por placa o usuario."""
    query = "SELECT * FROM logs_auditoria WHERE 1=1"
    params = []

    if placa:
        query += " AND placa_afectada = ?"
        params.append(placa.strip().upper())
    if usuario_id:
        query += " AND usuario_id = ?"
        params.append(usuario_id)

    query += " ORDER BY fecha_hora DESC LIMIT ?"
    params.append(limite)

    with get_connection() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# NUEVO (Paso 6 - Dashboard Web Admin): funciones añadidas a tu database.py
# original. No modifican nada existente, solo agregan lo que el dashboard
# necesita y que el bot de Telegram no requería.
# ---------------------------------------------------------------------------

def listar_todas_las_placas():
    """
    Lista TODOS los vehículos (activos y dados de baja), ordenados por
    fecha de ingreso descendente. El bot solo necesitaba las activas
    (listar_placas_activas); el inventario del dashboard debe poder
    mostrar también el historial de bajas.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM vehiculos ORDER BY fecha_ingreso DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def obtener_usuario_por_id(usuario_id: int):
    """Busca un usuario por su id interno (no por telegram_id)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Sesiones de login (Etapa 1 — autenticación real)
# ---------------------------------------------------------------------------
#
# El servidor genera un token aleatorio opaco (no el usuario_id) y lo guarda
# aquí junto con su fecha de expiración. El navegador solo recibe el token
# en una cookie httponly; el usuario_id real nunca viaja en el cliente, así
# que no se puede falsificar escribiendo otro id en un campo o cookie.

def crear_sesion(usuario_id: int) -> str:
    """Crea una sesión nueva para usuario_id y devuelve el token opaco."""
    token = secrets.token_urlsafe(32)
    expira_en = (
        datetime.now() + timedelta(hours=DURACION_SESION_HORAS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        # Aprovecha para limpiar sesiones vencidas de este mismo usuario,
        # así la tabla no crece sin límite con cada login.
        conn.execute(
            "DELETE FROM sesiones WHERE usuario_id = ? AND expira_en <= datetime('now', 'localtime')",
            (usuario_id,),
        )
        conn.execute(
            "INSERT INTO sesiones (token, usuario_id, expira_en) VALUES (?, ?, ?)",
            (token, usuario_id, expira_en),
        )
    return token


def obtener_usuario_por_token(token: str):
    """
    Devuelve el usuario asociado a un token de sesión vigente (no vencido),
    o None si el token no existe, ya expiró, o el usuario fue borrado.
    Se resuelve con un JOIN "en vivo": si el usuario fue desactivado después
    de crear la sesión, su fila seguirá encontrándose (para poder revisar su
    estado), pero requerir_autenticacion() la rechazará por estado inactivo.
    """
    with get_connection() as conn:
        fila = conn.execute(
            """SELECT u.* FROM sesiones s
               JOIN usuarios u ON u.id = s.usuario_id
               WHERE s.token = ? AND s.expira_en > datetime('now', 'localtime')""",
            (token,),
        ).fetchone()
        return dict(fila) if fila else None


def eliminar_sesion(token: str):
    """Invalida un token de sesión (logout). No falla si el token no existe."""
    with get_connection() as conn:
        conn.execute("DELETE FROM sesiones WHERE token = ?", (token,))


def invalidar_sesiones_de_usuario(usuario_id: int):
    """
    Invalida TODAS las sesiones activas de un usuario (Etapa 4.6): usada al
    desactivarlo o al cambiarle la contraseña, para que una sesión web ya
    abierta no seguir funcionando tras cualquiera de esas dos acciones.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,))


def actualizar_ultimo_acceso(usuario_id: int, momento: str):
    with get_connection() as conn:
        conn.execute("UPDATE usuarios SET ultimo_acceso = ? WHERE id = ?", (momento, usuario_id))


# ---------------------------------------------------------------------------
# Grado Operativo y estado CAMARA (Etapa 4.6)
#
# El Grado Operativo y el estado_camara viven en `usuarios`, NUNCA en
# `vehiculos`: la antigüedad de una placa se calcula al vuelo a partir de
# su fecha_ingreso (que nunca se modifica), no se le asigna un grado
# guardado. Ver camara.py para el cálculo de ambos.
# ---------------------------------------------------------------------------

def establecer_grado_operativo(usuario_id: int, grado: str):
    if grado not in GRADOS_OPERATIVOS:
        raise ValorInvalidoError(f"Grado operativo inválido: {grado}. Usa uno de {GRADOS_OPERATIVOS}")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE usuarios SET grado_operativo = ? WHERE id = ?", (grado, usuario_id)
        )
        if cur.rowcount == 0:
            raise RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")


def actualizar_estado_camara(usuario_id: int, nuevo_estado: str):
    if nuevo_estado not in ESTADOS_CAMARA:
        raise ValorInvalidoError(f"Estado CAMARA inválido: {nuevo_estado}")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE usuarios SET estado_camara = ? WHERE id = ?", (nuevo_estado, usuario_id)
        )
        if cur.rowcount == 0:
            raise RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")


def registrar_actividad_camara_db(usuario_id: int, momento: str):
    """
    Escritura cruda de una actividad CAMARA real: siempre guarda el
    momento; solo restaura el estado a ACTIVA si NO estaba SUSPENDIDA
    (Etapa 5 §36: la actividad real ya no levanta una suspensión, solo un
    Admin puede hacerlo vía actualizar_estado_camara/reactivar_camara_manual).
    La orquestación (auditoría, eventos de notificación) vive en
    camara.registrar_actividad_camara, que es quien debe llamarse
    normalmente; esta función es el detalle de persistencia.
    """
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT estado_camara FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if fila is None:
            raise RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")

        if fila["estado_camara"] == "SUSPENDIDA":
            conn.execute(
                "UPDATE usuarios SET ultima_actividad_camara = ? WHERE id = ?",
                (momento, usuario_id),
            )
        else:
            conn.execute(
                "UPDATE usuarios SET ultima_actividad_camara = ?, estado_camara = 'ACTIVA' WHERE id = ?",
                (momento, usuario_id),
            )


# ---------------------------------------------------------------------------
# Cola de eventos de notificación (Etapa 4.6) — preparación para Telegram
# ---------------------------------------------------------------------------

def crear_evento_notificacion(usuario_id: int, tipo_evento: str, datos: str = None) -> int:
    if tipo_evento not in TIPOS_EVENTO_NOTIFICACION:
        raise ValorInvalidoError(f"Tipo de evento inválido: {tipo_evento}")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO eventos_notificacion (usuario_id, tipo_evento, datos) VALUES (?, ?, ?)",
            (usuario_id, tipo_evento, datos),
        )
        return cur.lastrowid


def listar_eventos_pendientes(limite: int = 100):
    """Eventos aún no entregados, para cuando exista un consumidor de
    Telegram. No hay ningún consumidor implementado en esta etapa."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM eventos_notificacion WHERE entregado = 0 ORDER BY creado_en ASC LIMIT ?",
            (limite,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Snapshots de información externa por placa (Etapa 5)
#
# Cada fila es un hecho histórico inmutable: nunca se actualiza una fila
# existente, solo se inserta una nueva (Etapa 5 §30). "El snapshot vigente
# de una placa" es siempre el de creado_en más reciente para esa placa.
# ---------------------------------------------------------------------------

def crear_snapshot(placa: str, fuente: str, datos: str = None) -> int:
    """
    `datos` es un TEXT (JSON serializado por el caller) con lo que se haya
    obtenido: nunca se interpreta ni se convierte aquí, así que campos
    como el número de radicado viajan intactos como texto (Etapa 5 §20).
    """
    placa = placa.strip().upper()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO snapshots_placa (placa, fuente, datos) VALUES (?, ?, ?)",
            (placa, fuente, datos),
        )
        return cur.lastrowid


def obtener_ultimo_snapshot(placa: str):
    placa = placa.strip().upper()
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT * FROM snapshots_placa WHERE placa = ? ORDER BY creado_en DESC, id DESC LIMIT 1",
            (placa,),
        ).fetchone()
        return dict(fila) if fila else None


_EXPR_ESTADO_INFORMACION = f"""
    CASE
        WHEN MAX(s.creado_en) IS NULL THEN 'SIN_DATOS'
        WHEN julianday('now', 'localtime') - julianday(MAX(s.creado_en)) <= {VIGENCIA_INFORMACION_DIAS}
            THEN 'ACTUALIZADA'
        ELSE 'PENDIENTE'
    END
"""


def listar_placas_gestion_masiva(
    fecha_referencia: str,
    mayor_o_igual: bool,
    q: str = "",
    filtro_estado: str = "",
    pagina: int = 1,
    por_pagina: int = 10,
):
    """
    Placas cuya fecha_ingreso cumpla la condición de período ya resuelta
    por gestion_masiva.calcular_condicion_periodo: >= fecha_referencia
    para los períodos "recientes" (último día/semana/15 días) o <=
    fecha_referencia para los períodos "3+/6+ meses" (antigüedad mínima).
    `mayor_o_igual` es un booleano controlado internamente (nunca texto
    de usuario) que decide cuál de los dos operadores usar en el SQL.

    Además aplica búsqueda de placa (parcial, insensible a mayúsculas) y
    filtro por estado de información calculado (ACTUALIZADA/PENDIENTE/
    SIN_DATOS a partir del snapshot más reciente, ver
    VIGENCIA_INFORMACION_DIAS). Paginado en SQL: nunca trae más filas de
    las que la página necesita.

    Devuelve (lista_de_placas, total_sin_paginar).
    """
    if filtro_estado and filtro_estado not in ESTADOS_INFORMACION_PLACA:
        raise ValorInvalidoError(f"Estado de información inválido: {filtro_estado}")

    operador = ">=" if mayor_o_igual else "<="
    q_norm = q.strip().upper()
    like_param = f"%{q_norm}%"

    base_sql = f"""
        SELECT v.*, MAX(s.creado_en) AS ultimo_snapshot,
               {_EXPR_ESTADO_INFORMACION} AS estado_informacion
        FROM vehiculos v
        LEFT JOIN snapshots_placa s ON s.placa = v.placa
        WHERE v.fecha_ingreso {operador} ?
          AND (? = '' OR UPPER(v.placa) LIKE ?)
        GROUP BY v.placa
        HAVING (? = '' OR estado_informacion = ?)
    """
    params_base = [fecha_referencia, q_norm, like_param, filtro_estado, filtro_estado]

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({base_sql})", params_base
        ).fetchone()[0]

        offset = max(pagina - 1, 0) * por_pagina
        filas = conn.execute(
            base_sql + " ORDER BY v.fecha_ingreso DESC LIMIT ? OFFSET ?",
            params_base + [por_pagina, offset],
        ).fetchall()

        return [dict(f) for f in filas], total


def listar_todas_las_placas_periodo(
    fecha_referencia: str, mayor_o_igual: bool, q: str = "", filtro_estado: str = ""
):
    """
    Como listar_placas_gestion_masiva pero SIN paginar y devolviendo solo
    las placas (no el vehículo completo): usada exclusivamente para armar
    la cola completa de un estudio masivo al iniciarlo (Etapa 5 §25/§26),
    donde sí hace falta el conjunto completo, no una página.
    """
    if filtro_estado and filtro_estado not in ESTADOS_INFORMACION_PLACA:
        raise ValorInvalidoError(f"Estado de información inválido: {filtro_estado}")

    operador = ">=" if mayor_o_igual else "<="
    q_norm = q.strip().upper()
    like_param = f"%{q_norm}%"

    sql = f"""
        SELECT v.placa, MAX(s.creado_en) AS ultimo_snapshot,
               {_EXPR_ESTADO_INFORMACION} AS estado_informacion
        FROM vehiculos v
        LEFT JOIN snapshots_placa s ON s.placa = v.placa
        WHERE v.fecha_ingreso {operador} ?
          AND (? = '' OR UPPER(v.placa) LIKE ?)
        GROUP BY v.placa
        HAVING (? = '' OR estado_informacion = ?)
        ORDER BY v.fecha_ingreso DESC
    """
    with get_connection() as conn:
        filas = conn.execute(sql, [fecha_referencia, q_norm, like_param, filtro_estado, filtro_estado]).fetchall()
        return [f["placa"] for f in filas]


def obtener_metricas_gestion_masiva(fecha_referencia: str, mayor_o_igual: bool, q: str = ""):
    """
    Conteos agregados (total/actualizadas/pendientes/sin_datos) para las
    tarjetas de métricas: reflejan el período + búsqueda, pero NO el
    filtro de estado (para poder mostrar el desglose completo sin que se
    colapse al aplicar ese mismo filtro).
    """
    operador = ">=" if mayor_o_igual else "<="
    q_norm = q.strip().upper()
    like_param = f"%{q_norm}%"

    sql = f"""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN estado_informacion = 'ACTUALIZADA' THEN 1 ELSE 0 END), 0) AS actualizadas,
            COALESCE(SUM(CASE WHEN estado_informacion = 'PENDIENTE' THEN 1 ELSE 0 END), 0) AS pendientes,
            COALESCE(SUM(CASE WHEN estado_informacion = 'SIN_DATOS' THEN 1 ELSE 0 END), 0) AS sin_datos
        FROM (
            SELECT v.placa, {_EXPR_ESTADO_INFORMACION} AS estado_informacion
            FROM vehiculos v
            LEFT JOIN snapshots_placa s ON s.placa = v.placa
            WHERE v.fecha_ingreso {operador} ?
              AND (? = '' OR UPPER(v.placa) LIKE ?)
            GROUP BY v.placa
        )
    """
    with get_connection() as conn:
        fila = conn.execute(sql, [fecha_referencia, q_norm, like_param]).fetchone()
        return dict(fila)


# ---------------------------------------------------------------------------
# Job Manager de estudios masivos (Etapa 5)
#
# NO hay ningún proceso en segundo plano: el progreso avanza por lotes
# acotados vía peticiones HTTP explícitas (gestion_masiva.py), nunca un
# loop gigante dentro de una sola petición (Etapa 5 §25).
# ---------------------------------------------------------------------------

def crear_estudio(admin_id: int, periodo: str, filtro_estado: str, busqueda: str, placas: list) -> int:
    """Crea un estudio y encola las placas dadas en el orden recibido
    (prioridad 0 = normal). Devuelve el id del estudio."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO estudios_masivos (admin_id, periodo, filtro_estado, busqueda, total_placas)
               VALUES (?, ?, ?, ?, ?)""",
            (admin_id, periodo, filtro_estado or None, busqueda or None, len(placas)),
        )
        estudio_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO cola_estudio (estudio_id, placa, orden) VALUES (?, ?, ?)",
            [(estudio_id, placa, i) for i, placa in enumerate(placas)],
        )
        return estudio_id


def obtener_estudio(estudio_id: int):
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT * FROM estudios_masivos WHERE id = ?", (estudio_id,)
        ).fetchone()
        return dict(fila) if fila else None


def obtener_estudio_mas_reciente():
    """El estudio más reciente del sistema (Gestión Masiva es una
    herramienta administrativa compartida, no hay una cola por Admin)."""
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT * FROM estudios_masivos ORDER BY creado_en DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(fila) if fila else None


def listar_cola_estudio(estudio_id: int, limite: int = 200):
    with get_connection() as conn:
        filas = conn.execute(
            """SELECT * FROM cola_estudio WHERE estudio_id = ?
               ORDER BY prioridad DESC, orden ASC LIMIT ?""",
            (estudio_id, limite),
        ).fetchall()
        return [dict(f) for f in filas]


def cambiar_estado_estudio(estudio_id: int, nuevo_estado: str):
    if nuevo_estado not in ESTADOS_ESTUDIO:
        raise ValorInvalidoError(f"Estado de estudio inválido: {nuevo_estado}")
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE estudios_masivos SET estado = ?, actualizado_en = datetime('now', 'localtime')
               WHERE id = ?""",
            (nuevo_estado, estudio_id),
        )
        if cur.rowcount == 0:
            raise RegistroNoEncontradoError(f"Estudio id={estudio_id} no existe")


def priorizar_placa_en_cola(estudio_id: int, placa: str) -> str:
    """
    Da prioridad alta a una placa dentro de la cola de un estudio (Etapa 5
    §24): si ya estaba encolada (en cualquier estado), sube su prioridad;
    si no estaba, la agrega al frente. Nunca modifica otras filas.
    Devuelve 'actualizada' o 'agregada'.
    """
    placa = placa.strip().upper()
    with get_connection() as conn:
        existente = conn.execute(
            "SELECT id FROM cola_estudio WHERE estudio_id = ? AND placa = ? AND estado = 'PENDIENTE'",
            (estudio_id, placa),
        ).fetchone()
        if existente:
            conn.execute(
                "UPDATE cola_estudio SET prioridad = 100 WHERE id = ?", (existente["id"],)
            )
            return "actualizada"

        orden_min = conn.execute(
            "SELECT COALESCE(MIN(orden), 0) AS m FROM cola_estudio WHERE estudio_id = ?",
            (estudio_id,),
        ).fetchone()["m"]
        conn.execute(
            """INSERT INTO cola_estudio (estudio_id, placa, prioridad, orden)
               VALUES (?, ?, 100, ?)""",
            (estudio_id, placa, orden_min - 1),
        )
        conn.execute(
            "UPDATE estudios_masivos SET total_placas = total_placas + 1 WHERE id = ?",
            (estudio_id,),
        )
        return "agregada"


def tomar_siguiente_lote(estudio_id: int, tamano: int):
    """Toma (sin marcar todavía) hasta `tamano` items PENDIENTE de la cola,
    en orden de prioridad y luego de encolado."""
    with get_connection() as conn:
        filas = conn.execute(
            """SELECT * FROM cola_estudio WHERE estudio_id = ? AND estado = 'PENDIENTE'
               ORDER BY prioridad DESC, orden ASC LIMIT ?""",
            (estudio_id, tamano),
        ).fetchall()
        return [dict(f) for f in filas]


def marcar_item_cola(item_id: int, nuevo_estado: str, error_detalle: str = None):
    if nuevo_estado not in ESTADOS_ITEM_COLA:
        raise ValorInvalidoError(f"Estado de item de cola inválido: {nuevo_estado}")
    with get_connection() as conn:
        conn.execute(
            """UPDATE cola_estudio
               SET estado = ?, intentos = intentos + 1,
                   procesado_en = datetime('now', 'localtime'), error_detalle = ?
               WHERE id = ?""",
            (nuevo_estado, error_detalle, item_id),
        )


def incrementar_contadores_estudio(estudio_id: int, procesadas_delta: int = 0, con_error_delta: int = 0):
    with get_connection() as conn:
        conn.execute(
            """UPDATE estudios_masivos
               SET procesadas = procesadas + ?, con_error = con_error + ?,
                   actualizado_en = datetime('now', 'localtime')
               WHERE id = ?""",
            (procesadas_delta, con_error_delta, estudio_id),
        )


# ---------------------------------------------------------------------------
# Punto de entrada manual: inicializa la DB al ejecutar el archivo directamente
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    inicializar_db()
