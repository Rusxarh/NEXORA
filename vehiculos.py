"""
vehiculos.py
Módulo de vehículos (placas).

Responsabilidades:
- Alta individual y alta masiva de placas: normaliza a mayúsculas y
  valida el formato (regex) ANTES de tocar la base de datos. La capa de
  datos (database.py) se mantiene "tonta": solo persiste y valida
  duplicados/tipo_vehiculo, la regla de formato vive aquí.
- Baja manual de una placa: procesa la confirmación del modal de
  index.html y exige que quien confirma sea un Admin autenticado.
- Inventario con búsqueda y filtro por estado (todos/activo/baja).
- Carga masiva de placas (solo Admin).
- Consulta de una placa (ficha, solo lectura) e Historial de esa placa
  (reutiliza logs_auditoria) — abiertas a cualquier usuario autenticado.

REGLA INVIOLABLE (heredada de la versión original): ninguna baja de
placa se ejecuta automáticamente. El backend rechaza cualquier
solicitud de baja que no incluya el campo oculto `confirmar` con el
valor exacto "SI" (ese campo únicamente lo produce el modal, tras un
clic explícito del usuario en "Confirmar baja"), sin importar el rol.

REGLA DE SEGURIDAD (Etapa 1): quién da de alta o de baja una placa ya
NO se toma de un campo de formulario `usuario_id` (ese campo se
eliminó de los templates): se toma siempre de `usuario_actual`, resuelto
por `usuarios.requerir_autenticacion` / `usuarios.requerir_admin` a
partir de la sesión de servidor.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request

import database as db
import usuarios
from templates import templates

router = APIRouter()

# Mismo patrón que el atributo `pattern` del <input name="placa"> en
# index.html: letras, números y guiones, 5 a 10 caracteres. Repetirlo
# aquí evita que una petición que no pase por el navegador (curl, un
# bot, etc.) se salte la validación del formulario.
PATRON_PLACA = re.compile(r"^[A-Za-z0-9\-]{5,10}$")


def normalizar_y_validar_placa(placa: str) -> str:
    """Pone la placa en mayúsculas y valida su formato. Lanza
    db.ValorInvalidoError si no cumple el patrón esperado."""
    placa = placa.strip().upper()
    if not PATRON_PLACA.match(placa):
        raise db.ValorInvalidoError(
            "El formato de la placa no es válido. Usa solo letras, números "
            "y guiones (5 a 10 caracteres)."
        )
    return placa


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------

@router.get("/")
def index(
    request: Request,
    usuario_actual: dict = Depends(usuarios.requerir_autenticacion),
    filtro: str = "todos",
    q: str = "",
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    # "todos" no es un estado real de la tabla: solo activo/baja filtran en SQL.
    estado_filtro = filtro if filtro in db.ESTADOS_VEHICULO else None
    vehiculos = db.buscar_vehiculos(query=q.strip() or None, estado=estado_filtro)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "vehiculos": vehiculos,
            "filtro": filtro,
            "q": q,
            "tipos_vehiculo": db.TIPOS_VEHICULO,
            "usuario_actual": usuario_actual,
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
            "ruta_activa": "inventario",
        },
    )


# ---------------------------------------------------------------------------
# Consulta de placas (solo lectura) — Etapa 4
# ---------------------------------------------------------------------------

@router.get("/consulta")
def consulta_vista(
    request: Request,
    usuario_actual: dict = Depends(usuarios.requerir_autenticacion),
    q: str = "",
):
    buscado = bool(q.strip())
    vehiculo = db.obtener_ficha_vehiculo(q) if buscado else None

    return templates.TemplateResponse(
        request,
        "consulta.html",
        {
            "request": request,
            "usuario_actual": usuario_actual,
            "q": q,
            "buscado": buscado,
            "vehiculo": vehiculo,
            "ruta_activa": "consulta",
        },
    )


# ---------------------------------------------------------------------------
# Historial de una placa (solo lectura) — Etapa 4
#
# Reutiliza logs_auditoria vía db.listar_logs_de_placa: NO es lo mismo que
# la Auditoría general (esa responde "¿qué pasó en el sistema?"; esto
# responde "¿qué pasó con ESTA placa?"), pero ambas leen la misma tabla,
# sin duplicar datos ni crear una tabla de historial separada.
# ---------------------------------------------------------------------------

@router.get("/historial/{placa}")
def historial_vista(
    request: Request,
    placa: str,
    usuario_actual: dict = Depends(usuarios.requerir_autenticacion),
):
    vehiculo = db.buscar_placa(placa)
    logs = db.listar_logs_de_placa(placa) if vehiculo else []

    return templates.TemplateResponse(
        request,
        "historial.html",
        {
            "request": request,
            "usuario_actual": usuario_actual,
            "placa_consultada": placa.strip().upper(),
            "vehiculo": vehiculo,
            "logs": logs,
            "ruta_activa": "historial",
        },
    )


@router.post("/vehiculos/alta")
def alta_vehiculo(
    placa: str = Form(...),
    tipo_vehiculo: str = Form(...),
    observaciones: str = Form(""),
    # Etapa 4.5: el alta individual queda restringida a Admin ("por ahora
    # NO conceder Alta individual al Operador", spec Etapa 4.5 §8). Antes
    # de esta etapa cualquier usuario autenticado podía registrar placas;
    # es un cambio de comportamiento deliberado, no un descuido.
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    observaciones = observaciones.strip() or None

    try:
        placa = normalizar_y_validar_placa(placa)
        db.insertar_placa(placa, tipo_vehiculo, usuario_actual["id"], observaciones)
        mensaje = f"La placa {placa} fue registrada correctamente."
        tipo = "exito"
    except db.PlacaDuplicadaError as e:
        mensaje, tipo = str(e), "error"
    except db.ValorInvalidoError as e:
        mensaje, tipo = str(e), "error"

    return usuarios.redirigir_con_mensaje("/", mensaje, tipo)


@router.post("/vehiculos/baja")
def dar_de_baja(
    placa: str = Form(...),
    observaciones: str = Form(""),
    confirmar: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    observaciones = observaciones.strip() or None

    if confirmar != "SI":
        # Nunca se ejecuta cambiar_estado_a_baja sin esta confirmación
        # explícita, ni siquiera para un Admin.
        mensaje = "Baja rechazada: no se recibió confirmación manual explícita."
        return usuarios.redirigir_con_mensaje("/", mensaje, "error")

    try:
        placa = placa.strip().upper()
        db.cambiar_estado_a_baja(placa, usuario_actual["id"], observaciones)
        mensaje = f"La placa {placa} fue dada de baja correctamente."
        tipo = "exito"
    except db.RegistroNoEncontradoError as e:
        mensaje, tipo = str(e), "error"
    except db.ValorInvalidoError as e:
        mensaje, tipo = str(e), "error"

    return usuarios.redirigir_con_mensaje("/", mensaje, tipo)


# ---------------------------------------------------------------------------
# Carga masiva de placas (solo Admin)
# ---------------------------------------------------------------------------

@router.get("/vehiculos/carga-masiva")
def carga_masiva_vista(
    request: Request,
    usuario_actual: dict = Depends(usuarios.requerir_admin),
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    return templates.TemplateResponse(
        request,
        "carga_masiva.html",
        {
            "request": request,
            "tipos_vehiculo": db.TIPOS_VEHICULO,
            "usuario_actual": usuario_actual,
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
            # "gestion_masiva" (no "carga_masiva"): Carga masiva ahora vive
            # dentro del módulo único "Gestión masiva de placas" en el
            # sidebar (ver base.html/_tabs_gestion_masiva.html); no tiene
            # su propia entrada de sidebar independiente.
            "ruta_activa": "gestion_masiva",
        },
    )


@router.post("/vehiculos/carga-masiva")
def carga_masiva_procesar(
    lineas: str = Form(...),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    placas_a_insertar = []
    formato_invalido = []

    for linea in lineas.splitlines():
        linea = linea.strip()
        if not linea:
            continue

        partes = [p.strip() for p in linea.split(",")]
        placa_cruda = partes[0] if partes else ""
        if not placa_cruda:
            continue

        tipo_vehiculo = partes[1].lower() if len(partes) > 1 and partes[1] else "carro"
        observaciones = partes[2] if len(partes) > 2 and partes[2] else None

        try:
            placa = normalizar_y_validar_placa(placa_cruda)
        except db.ValorInvalidoError:
            formato_invalido.append(placa_cruda.strip().upper())
            continue

        placas_a_insertar.append(
            {
                "placa": placa,
                "tipo_vehiculo": tipo_vehiculo,
                "observaciones": observaciones,
            }
        )

    if not placas_a_insertar and not formato_invalido:
        return usuarios.redirigir_con_mensaje(
            "/vehiculos/carga-masiva",
            "No se encontró ninguna placa válida para procesar.",
            "error",
        )

    if placas_a_insertar:
        resumen = db.insertar_placas_masivo(placas_a_insertar, usuario_actual["id"])
    else:
        resumen = {"insertadas": [], "duplicadas": [], "invalidas": []}

    resumen["invalidas"] = resumen["invalidas"] + formato_invalido

    partes_mensaje = [f"{len(resumen['insertadas'])} insertada(s)"]
    if resumen["duplicadas"]:
        partes_mensaje.append(
            f"{len(resumen['duplicadas'])} duplicada(s): {', '.join(resumen['duplicadas'])}"
        )
    if resumen["invalidas"]:
        partes_mensaje.append(
            f"{len(resumen['invalidas'])} inválida(s): {', '.join(resumen['invalidas'])}"
        )

    if resumen["insertadas"] and not resumen["duplicadas"] and not resumen["invalidas"]:
        tipo_mensaje = "exito"
    elif not resumen["insertadas"]:
        tipo_mensaje = "error"
    else:
        tipo_mensaje = "info"

    mensaje = "Carga masiva: " + " · ".join(partes_mensaje)

    return usuarios.redirigir_con_mensaje("/vehiculos/carga-masiva", mensaje, tipo_mensaje)
