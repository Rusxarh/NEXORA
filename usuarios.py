"""
usuarios.py
Módulo de usuarios, roles y autenticación.

Responsabilidades:
- CRUD de la tabla `usuarios` (alta, listado, cambio de estado),
  delegando la persistencia en database.py.
- Autenticación real: login con usuario/contraseña, contraseñas
  guardadas como hash (PBKDF2-HMAC-SHA256, salteado), sesiones de
  servidor identificadas por un token opaco en una cookie httponly.
- Control de acceso: `requerir_autenticacion` y `requerir_admin` son
  Dependencies de FastAPI que cualquier ruta protegida declara; si
  fallan, lanzan una excepción que main.py traduce en un redirect
  (a /login o a "/") mediante un exception_handler global.

REGLA DE SEGURIDAD: el usuario de la sesión SIEMPRE se resuelve a
partir del token de la cookie (buscado en la tabla `sesiones`), nunca
de un campo de formulario. Ningún form de la aplicación debe volver a
pedir un "Usuario ID": quien actúa es quien está autenticado.
"""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from urllib.parse import quote

import camara
import database as db
from templates import templates

router = APIRouter()

COOKIE_SESION = "session_token"

_PBKDF2_ALGORITMO = "pbkdf2_sha256"
_PBKDF2_ITERACIONES = 260_000


class PermisoDenegadoError(Exception):
    """Se lanza cuando el usuario autenticado no tiene el rol requerido."""


class NoAutenticadoError(Exception):
    """Se lanza cuando no hay una sesión válida (o expiró/fue cerrada)."""


# ---------------------------------------------------------------------------
# Hash de contraseñas (solo librerías estándar de Python, sin dependencias
# nuevas): PBKDF2-HMAC-SHA256 con salt aleatorio por usuario. Formato
# almacenado: "pbkdf2_sha256$iteraciones$salt$hash_hex".
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    sal = secrets.token_hex(16)
    derivado = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), sal.encode("utf-8"), _PBKDF2_ITERACIONES
    )
    return f"{_PBKDF2_ALGORITMO}${_PBKDF2_ITERACIONES}${sal}${derivado.hex()}"


def verificar_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        algoritmo, iteraciones, sal, hash_esperado = password_hash.split("$")
    except ValueError:
        return False
    if algoritmo != _PBKDF2_ALGORITMO:
        return False

    derivado = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), sal.encode("utf-8"), int(iteraciones)
    )
    # comparación en tiempo constante: evita filtrar por timing cuánto del
    # hash coincide.
    return hmac.compare_digest(derivado.hex(), hash_esperado)


# ---------------------------------------------------------------------------
# Sesión y control de acceso
# ---------------------------------------------------------------------------

def _es_ruta_segura(ruta: Optional[str]) -> bool:
    """Evita 'open redirect': solo se acepta como destino una ruta interna
    que empiece con '/' (y no con '//', que un navegador puede interpretar
    como un host externo)."""
    return bool(ruta) and ruta.startswith("/") and not ruta.startswith("//")


def redirigir_con_mensaje(destino: str, mensaje: str, tipo: str):
    """Redirige a `destino` mostrando un mensaje flash vía querystring.
    Reutilizada por usuarios.py, vehiculos.py y auditoria.py."""
    url = f"{destino}?mensaje={quote(mensaje)}&tipo={quote(tipo)}"
    return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)


def obtener_usuario_actual(request: Request) -> Optional[dict]:
    """Resuelve el usuario de la sesión actual a partir del token de la
    cookie. Nunca a partir de un campo editable por el cliente."""
    token = request.cookies.get(COOKIE_SESION)
    if not token:
        return None
    return db.obtener_usuario_por_token(token)


def es_admin(usuario: Optional[dict]) -> bool:
    """True si `usuario` es un Admin activo."""
    return bool(usuario) and usuario["rol"] == "Admin" and usuario["estado"] == "activo"


def requerir_autenticacion(request: Request) -> dict:
    """
    Dependency de FastAPI: exige una sesión válida, un usuario activo y
    (Etapa 4.6) rol Admin.

    Desde la Etapa 4.6 la aplicación web es EXCLUSIVA para Admin: los
    Operadores operarán únicamente por Telegram cuando exista. Este
    chequeo se hace aquí (y no solo en requerir_admin) para que TODA ruta
    protegida —incluidas las que en la Etapa 4.5 eran de solo-lectura
    para ambos roles (Dashboard, Inventario, Consulta, Historial)— quede
    bloqueada para Operador en backend, sin depender de que cada ruta
    recuerde usar requerir_admin. Si una sesión de Operador ya existía
    (por ejemplo, de antes de esta etapa), esta función también la
    invalida en la práctica: nunca vuelve a pasar este chequeo.

    Si falla, lanza NoAutenticadoError, que main.py traduce en un
    redirect a /login.
    """
    usuario = obtener_usuario_actual(request)
    if usuario is None or usuario["estado"] != "activo":
        raise NoAutenticadoError("Debes iniciar sesión para acceder a esta página.")
    if usuario["rol"] != "Admin":
        raise NoAutenticadoError(
            "Los Operadores no tienen acceso a la aplicación web. "
            "Tu operación se realizará a través de Telegram."
        )
    return usuario


def requerir_admin(usuario_actual: dict = Depends(requerir_autenticacion)) -> dict:
    """
    Dependency de FastAPI: exige, además de sesión válida, rol Admin.
    Se encadena sobre requerir_autenticacion, así que un usuario no
    autenticado recibe NoAutenticadoError (redirect a /login) y uno
    autenticado pero no-Admin recibe PermisoDenegadoError (redirect a "/").
    """
    if not es_admin(usuario_actual):
        raise PermisoDenegadoError(
            "Esta acción requiere un usuario con rol 'Admin' activo en la sesión."
        )
    return usuario_actual


# ---------------------------------------------------------------------------
# Permisos granulares (Etapa 4.5) — preparación para módulos futuros
# ---------------------------------------------------------------------------
#
# Hoy solo existen dos roles (Admin/Operador) y las rutas ya aprobadas les
# alcanza con requerir_autenticacion/requerir_admin. Este mapeo ROL →
# PERMISOS queda definido para que RGM, Consulta Judicial, OFICIOS y CAMARA
# (todavía no implementados) puedan declarar `Depends(requerir_permiso(...))`
# sin tener que inventar un tercer rol ni duplicar lógica de autorización.
# No se usa todavía en ninguna ruta existente para no arriesgar ninguna
# funcionalidad ya aprobada.

PERMISOS_OPERADOR = frozenset({
    "consulta_individual",
    "historial_individual",
    "inventario_lectura",
    "rgm_individual",        # RGM no implementado todavía
    "judicial_individual",   # Consulta Judicial no implementada todavía
    "oficios_solicitar",     # OFICIOS no implementado todavía
    "camara_operativa",      # CAMARA no implementada todavía
})


def tiene_permiso(usuario: dict, permiso: str) -> bool:
    """Admin tiene todos los permisos ('*'); Operador solo los listados en
    PERMISOS_OPERADOR. Cualquier otro rol (no debería existir) no tiene
    ninguno."""
    if es_admin(usuario):
        return True
    return bool(usuario) and usuario["rol"] == "Operador" and permiso in PERMISOS_OPERADOR


def requerir_permiso(permiso: str):
    """
    Factory de Dependency de FastAPI: exige que el usuario autenticado
    tenga el permiso indicado. Uso previsto (futuro):

        @router.get("/rgm/consulta")
        def rgm_individual(usuario_actual=Depends(usuarios.requerir_permiso("rgm_individual"))):
            ...
    """
    def dependencia(usuario_actual: dict = Depends(requerir_autenticacion)) -> dict:
        if not tiene_permiso(usuario_actual, permiso):
            raise PermisoDenegadoError(
                "Tu rol no tiene permiso para realizar esta acción."
            )
        return usuario_actual
    return dependencia


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@router.get("/login")
def login_vista(
    request: Request,
    siguiente: str = "/",
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    # Si ya hay una sesión válida, no tiene sentido mostrar el login de nuevo.
    if obtener_usuario_actual(request) is not None:
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "siguiente": siguiente if _es_ruta_segura(siguiente) else "/",
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
        },
    )


@router.post("/login")
def login_procesar(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    siguiente: str = Form("/"),
):
    destino = siguiente if _es_ruta_segura(siguiente) else "/"
    usuario = db.obtener_usuario_por_username(username.strip())

    credenciales_validas = (
        usuario is not None
        and usuario["estado"] == "activo"
        and verificar_password(password, usuario["password_hash"])
    )

    if not credenciales_validas:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "siguiente": destino,
                "mensaje": "Usuario o contraseña incorrectos.",
                "tipo_mensaje": "error",
            },
            status_code=401,
        )

    if usuario["rol"] != "Admin":
        # Etapa 4.6: la web es exclusiva para Admin. No se crea sesión
        # para un Operador aunque sus credenciales sean correctas: su
        # operación futura será por Telegram, no por esta interfaz.
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "siguiente": destino,
                "mensaje": (
                    "Tu usuario no tiene acceso a la aplicación web. "
                    "La operación de los Operadores se realizará a través de Telegram."
                ),
                "tipo_mensaje": "error",
            },
            status_code=403,
        )

    db.actualizar_ultimo_acceso(usuario["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    token = db.crear_sesion(usuario["id"])
    response = RedirectResponse(url=destino, status_code=HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_SESION,
        token,
        max_age=60 * 60 * db.DURACION_SESION_HORAS,
        httponly=True,
        samesite="lax",
        # secure=True queda pendiente para cuando la app se sirva por HTTPS;
        # en http://127.0.0.1 (desarrollo) el navegador descartaría la cookie.
    )
    return response


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE_SESION)
    if token:
        db.eliminar_sesion(token)

    response = redirigir_con_mensaje("/login", "Sesión cerrada correctamente.", "exito")
    response.delete_cookie(COOKIE_SESION)
    return response


# ---------------------------------------------------------------------------
# Vista de gestión de usuarios (solo Admin)
# ---------------------------------------------------------------------------

@router.get("/usuarios")
def usuarios_vista(
    request: Request,
    usuario_actual: dict = Depends(requerir_admin),
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    lista_usuarios = db.listar_usuarios(solo_activos=False)
    # Recalcula el estado CAMARA de cada Operador al vuelo (sin proceso en
    # segundo plano: ver camara.sincronizar_estado_camara), para que la
    # lista siempre muestre alertas/suspensiones al día.
    lista_usuarios = [
        camara.sincronizar_estado_camara(u["id"]) if u["rol"] == "Operador" else u
        for u in lista_usuarios
    ]

    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {
            "request": request,
            "usuarios": lista_usuarios,
            "roles_usuario": db.ROLES_USUARIO,
            "usuario_actual": usuario_actual,
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
            "ruta_activa": "usuarios",
        },
    )


@router.get("/usuarios/{usuario_id}")
def usuario_ficha(
    request: Request,
    usuario_id: int,
    usuario_actual: dict = Depends(requerir_admin),
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    objetivo = db.obtener_usuario_por_id(usuario_id)
    if objetivo is None:
        return redirigir_con_mensaje("/usuarios", f"El usuario id={usuario_id} no existe.", "error")
    if objetivo["rol"] == "Operador":
        objetivo = camara.sincronizar_estado_camara(usuario_id)

    return templates.TemplateResponse(
        request,
        "usuario_ficha.html",
        {
            "request": request,
            "usuario_actual": usuario_actual,
            "objetivo": objetivo,
            "grados_operativos": db.GRADOS_OPERATIVOS,
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
            "ruta_activa": "usuarios",
        },
    )


@router.post("/usuarios/alta")
def alta_usuario(
    telegram_id: int = Form(...),
    nombre: str = Form(...),
    rol: str = Form(...),
    usuario_actual: dict = Depends(requerir_admin),
):
    nombre = nombre.strip()

    try:
        db.crear_usuario(telegram_id, nombre, rol)
        mensaje = f"Usuario '{nombre}' registrado correctamente."
        tipo = "exito"
    except db.ValorInvalidoError as e:
        mensaje, tipo = str(e), "error"
    except sqlite3.IntegrityError:
        mensaje = f"El telegram_id {telegram_id} ya está registrado en otro usuario."
        tipo = "error"

    return redirigir_con_mensaje("/usuarios", mensaje, tipo)


@router.post("/usuarios/estado")
def cambiar_estado_de_usuario(
    usuario_id_objetivo: int = Form(...),
    nuevo_estado: str = Form(...),
    volver_a: str = Form(""),
    usuario_actual: dict = Depends(requerir_admin),
):
    destino = volver_a if _es_ruta_segura(volver_a) else "/usuarios"
    objetivo = db.obtener_usuario_por_id(usuario_id_objetivo)

    try:
        db.cambiar_estado_usuario(usuario_id_objetivo, nuevo_estado)
        nombre = objetivo["nombre"] if objetivo else f"id={usuario_id_objetivo}"

        if nuevo_estado == "inactivo":
            # Etapa 4.6 §7: desactivar invalida sesiones existentes,
            # queda auditado y encola el evento para el futuro Telegram.
            # NO borra al usuario ni su historial/placas/auditoría.
            db.invalidar_sesiones_de_usuario(usuario_id_objetivo)
            db.crear_evento_notificacion(usuario_id_objetivo, "USER_DEACTIVATED")
            db.registrar_log(
                usuario_actual["id"], "USUARIO_DESACTIVADO",
                observaciones=f"Usuario objetivo: {nombre} (id={usuario_id_objetivo}). Sesiones invalidadas.",
            )
            mensaje = f"Usuario '{nombre}' desactivado. Sus sesiones fueron cerradas."
        else:
            # Etapa 4.6 §8: reactivación, también auditada y notificable.
            db.crear_evento_notificacion(usuario_id_objetivo, "USER_REACTIVATED")
            db.registrar_log(
                usuario_actual["id"], "USUARIO_REACTIVADO",
                observaciones=f"Usuario objetivo: {nombre} (id={usuario_id_objetivo}).",
            )
            mensaje = f"Usuario '{nombre}' reactivado."
        tipo = "exito"
    except db.RegistroNoEncontradoError as e:
        mensaje, tipo = str(e), "error"
    except db.ValorInvalidoError as e:
        mensaje, tipo = str(e), "error"

    return redirigir_con_mensaje(destino, mensaje, tipo)


@router.post("/usuarios/{usuario_id}/password")
def cambiar_password_usuario(
    usuario_id: int,
    nueva_password: str = Form(...),
    usuario_actual: dict = Depends(requerir_admin),
):
    """
    Etapa 4.6 §5: solo Admin puede cambiar la contraseña de otro usuario.
    Nunca se recibe/guarda en texto plano (hash_password), invalida las
    sesiones existentes del afectado y queda auditado sin registrar la
    contraseña.
    """
    objetivo = db.obtener_usuario_por_id(usuario_id)
    if objetivo is None:
        return redirigir_con_mensaje("/usuarios", f"El usuario id={usuario_id} no existe.", "error")
    if not objetivo["username"]:
        return redirigir_con_mensaje(
            f"/usuarios/{usuario_id}",
            "Este usuario todavía no tiene un nombre de usuario web asignado.",
            "error",
        )
    if len(nueva_password) < 8:
        return redirigir_con_mensaje(
            f"/usuarios/{usuario_id}", "La contraseña debe tener al menos 8 caracteres.", "error"
        )

    db.establecer_credenciales(usuario_id, objetivo["username"], hash_password(nueva_password))
    db.invalidar_sesiones_de_usuario(usuario_id)
    db.registrar_log(
        usuario_actual["id"], "CAMBIO_PASSWORD",
        observaciones=f"Usuario objetivo: {objetivo['nombre']} (id={usuario_id}). Sesiones invalidadas.",
    )
    return redirigir_con_mensaje(
        f"/usuarios/{usuario_id}", f"Contraseña de '{objetivo['nombre']}' actualizada.", "exito"
    )


@router.post("/usuarios/{usuario_id}/grado")
def cambiar_grado_usuario(
    usuario_id: int,
    grado_operativo: str = Form(...),
    usuario_actual: dict = Depends(requerir_admin),
):
    """Etapa 4.6 §9-11: el Grado Operativo pertenece al Operador, nunca a
    la placa. Solo Admin puede asignarlo/modificarlo."""
    objetivo = db.obtener_usuario_por_id(usuario_id)
    if objetivo is None:
        return redirigir_con_mensaje("/usuarios", f"El usuario id={usuario_id} no existe.", "error")
    if objetivo["rol"] != "Operador":
        return redirigir_con_mensaje(
            f"/usuarios/{usuario_id}", "El Grado Operativo solo aplica a usuarios con rol Operador.", "error"
        )

    try:
        grado_anterior = objetivo["grado_operativo"] or "sin asignar"
        db.establecer_grado_operativo(usuario_id, grado_operativo)
        db.registrar_log(
            usuario_actual["id"], "CAMBIO_GRADO_OPERATIVO",
            observaciones=f"Usuario objetivo: {objetivo['nombre']} (id={usuario_id}). {grado_anterior} -> {grado_operativo}.",
        )
        mensaje = f"Grado operativo de '{objetivo['nombre']}' actualizado a {grado_operativo}."
        tipo = "exito"
    except db.ValorInvalidoError as e:
        mensaje, tipo = str(e), "error"

    return redirigir_con_mensaje(f"/usuarios/{usuario_id}", mensaje, tipo)


@router.post("/usuarios/{usuario_id}/camara/reactivar")
def reactivar_camara(
    usuario_id: int,
    motivo: str = Form(""),
    usuario_actual: dict = Depends(requerir_admin),
):
    """Etapa 4.6 §26: reactivación MANUAL del servicio CAMARA, independiente
    de la reactivación del usuario."""
    try:
        objetivo = camara.reactivar_camara_manual(usuario_id, usuario_actual["id"], motivo.strip() or None)
        mensaje, tipo = f"CAMARA de '{objetivo['nombre']}' reactivada.", "exito"
    except db.RegistroNoEncontradoError as e:
        mensaje, tipo = str(e), "error"

    return redirigir_con_mensaje(f"/usuarios/{usuario_id}", mensaje, tipo)
