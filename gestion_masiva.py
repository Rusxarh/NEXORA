"""
gestion_masiva.py
Módulo de Gestión Masiva (Etapa 5): estudio de placas por período de
antigüedad, ficha de información local, y cola de trabajo (Job Manager)
para futuras consultas externas (RGM, Rama Judicial) que todavía no
existen.

Responsabilidades:
- Interpretar los 5 botones de período en condiciones concretas sobre
  `fecha_ingreso` (que nunca se modifica).
- Calcular el estado de información de cada placa (ACTUALIZADA/PENDIENTE/
  SIN_DATOS) a partir de sus snapshots_placa (ver database.py).
- Orquestar la cola de un estudio masivo: crear, priorizar, procesar por
  lotes acotados, pausar, reanudar, cancelar — sin ningún loop gigante
  dentro de una petición HTTP ni ningún proceso en segundo plano (Etapa 5
  §25): el progreso avanza porque el navegador reenvía el "siguiente
  lote" automáticamente (ver el bloque `scripts` de la plantilla).
- Exclusivo para Admin: todas las rutas dependen de
  usuarios.requerir_admin. Desde la Etapa 4.6, requerir_autenticacion ya
  bloquea a cualquier Operador de TODA la web, así que este módulo hereda
  ese bloqueo de raíz además de exigir explícitamente el rol Admin.

REGLA DE SEGURIDAD (Etapa 5 §2/§37): la información sensible (deudor,
documento, acreedor, radicado, proceso judicial) solo se renderiza aquí,
una interfaz ya 100% Admin-only en backend — nunca se expone a través de
ninguna ruta accesible a Operador.

RADICADOS (Etapa 5 §20): `datos` en snapshots_placa es un TEXT con JSON
serializado por este módulo. El radicado SIEMPRE se maneja como string
Python de principio a fin (json.dumps/json.loads no lo tocan si nunca se
construye como int/float); nunca se le aplica ninguna conversión numérica.
"""

import calendar
import json
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request

import database as db
import usuarios
from templates import templates

router = APIRouter()

POR_PAGINA = 10
TAMANO_LOTE_ESTUDIO = 20

PERIODOS = {
    "ultimo_dia": "Último día",
    "ultima_semana": "Última semana",
    "ultimos_15_dias": "Últimos 15 días",
    "ultimos_3_meses": "Últimos 3 meses+",
    "ultimos_6_meses": "Últimos 6 meses+",
}
PERIODO_POR_DEFECTO = "ultimos_15_dias"


# ---------------------------------------------------------------------------
# Períodos (Etapa 5 §7-8)
# ---------------------------------------------------------------------------

def _restar_meses(fecha: date, meses: int) -> date:
    """Resta `meses` meses calendario a `fecha`, recortando el día si el
    mes destino tiene menos días (ej. 31 mar - 1 mes -> 28/29 feb)."""
    mes_total = fecha.month - 1 - meses
    anio = fecha.year + mes_total // 12
    mes = mes_total % 12 + 1
    dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
    return date(anio, mes, dia)


def calcular_condicion_periodo(periodo: str, ahora: Optional[datetime] = None):
    """
    Traduce un período (botón) en (mayor_o_igual: bool, fecha_referencia: str)
    para usar contra `vehiculos.fecha_ingreso`.

    INTERPRETACIÓN DOCUMENTADA (Etapa 5 §7-8, analizada antes de
    implementar tal como pidió el enunciado, en vez de adivinar):

    - "Último día" / "Última semana" / "Últimos 15 días": placas
      INGRESADAS RECIENTEMENTE, es decir fecha_ingreso >= (hoy - N días).
      Esto coincide exactamente con los ejemplos del enunciado (hoy
      13/09/2026: último día -> desde 12/09; semana -> desde 06/09;
      15 días -> desde 29/08/2026).

    - "Últimos 3 meses+" / "Últimos 6 meses+": el símbolo "+" cambia el
      sentido de la comparación. Si significaran "ingresadas en los
      últimos 3/6 meses" serían redundantes con los tres botones
      anteriores (y el "+" no tendría ningún propósito). Se interpretan
      como "placas con 3+ / 6+ MESES DE ANTIGÜEDAD", es decir
      fecha_ingreso <= (hoy - 3 meses) / (hoy - 6 meses): placas viejas
      que probablemente necesiten revisión o estudio pendiente. Esta
      lectura es la más consistente con el objetivo funcional declarado
      ("permitir al Admin estudiar placas según antigüedad") porque hace
      que los 5 botones cubran el espectro completo (recientes vs.
      maduras) sin solaparse. Si esta interpretación no es la deseada,
      es un cambio de una sola función (`calcular_condicion_periodo`),
      sin ninguna migración de datos de por medio.
    """
    ahora = ahora or datetime.now()
    hoy = ahora.date()

    if periodo == "ultimo_dia":
        return True, f"{(hoy - timedelta(days=1)).isoformat()} 00:00:00"
    if periodo == "ultima_semana":
        return True, f"{(hoy - timedelta(days=7)).isoformat()} 00:00:00"
    if periodo == "ultimos_15_dias":
        return True, f"{(hoy - timedelta(days=15)).isoformat()} 00:00:00"
    if periodo == "ultimos_3_meses":
        return False, f"{_restar_meses(hoy, 3).isoformat()} 23:59:59"
    if periodo == "ultimos_6_meses":
        return False, f"{_restar_meses(hoy, 6).isoformat()} 23:59:59"

    raise db.ValorInvalidoError(f"Período inválido: {periodo}. Usa uno de {tuple(PERIODOS)}")


def _periodo_valido(periodo: str) -> str:
    return periodo if periodo in PERIODOS else PERIODO_POR_DEFECTO


def _construir_paginas(pagina_actual: int, total_paginas: int, ventana: int = 2):
    """
    Lista de números de página a mostrar en la paginación (Etapa 5A §20:
    [1][2][3][4][5][...][14]), con `None` como marcador de "...". Siempre
    incluye la primera y la última página, más una ventana alrededor de
    la actual.
    """
    if total_paginas <= 1:
        return [1]
    paginas = {1, total_paginas}
    for p in range(pagina_actual - ventana, pagina_actual + ventana + 1):
        if 1 <= p <= total_paginas:
            paginas.add(p)
    ordenadas = sorted(paginas)
    resultado = []
    anterior = None
    for p in ordenadas:
        if anterior is not None and p - anterior > 1:
            resultado.append(None)
        resultado.append(p)
        anterior = p
    return resultado


# ---------------------------------------------------------------------------
# Estado de información de una placa (Etapa 5 §11, §21-23)
# ---------------------------------------------------------------------------

def calcular_estado_informacion(placa: str, ahora: Optional[datetime] = None):
    """(estado, snapshot_dict_o_None) para UNA placa. SIN_DATOS si nunca
    tuvo snapshot; ACTUALIZADA si el más reciente tiene menos de
    VIGENCIA_INFORMACION_DIAS; PENDIENTE si es más viejo."""
    ahora = ahora or datetime.now()
    snapshot = db.obtener_ultimo_snapshot(placa)
    if snapshot is None:
        return "SIN_DATOS", None

    creado = datetime.strptime(snapshot["creado_en"][:19], "%Y-%m-%d %H:%M:%S")
    edad_dias = (ahora - creado).days
    estado = "ACTUALIZADA" if edad_dias <= db.VIGENCIA_INFORMACION_DIAS else "PENDIENTE"
    return estado, snapshot


def _parsear_datos(snapshot: Optional[dict]) -> dict:
    if not snapshot or not snapshot.get("datos"):
        return {}
    try:
        return json.loads(snapshot["datos"])
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@router.get("/gestion-masiva")
def gestion_masiva_vista(
    request: Request,
    usuario_actual: dict = Depends(usuarios.requerir_admin),
    periodo: str = PERIODO_POR_DEFECTO,
    q: str = "",
    estado: str = "",
    pagina: int = 1,
    placa_seleccionada: str = "",
    mensaje: Optional[str] = None,
    tipo: str = "info",
):
    periodo = _periodo_valido(periodo)
    mayor_o_igual, fecha_referencia = calcular_condicion_periodo(periodo)

    metricas = db.obtener_metricas_gestion_masiva(fecha_referencia, mayor_o_igual, q)
    total_metricas = metricas["total"] or 0
    metricas["pct_actualizadas"] = round(metricas["actualizadas"] / total_metricas * 100, 1) if total_metricas else 0
    metricas["pct_pendientes"] = round(metricas["pendientes"] / total_metricas * 100, 1) if total_metricas else 0
    metricas["pct_sin_datos"] = round(metricas["sin_datos"] / total_metricas * 100, 1) if total_metricas else 0

    placas, total = db.listar_placas_gestion_masiva(
        fecha_referencia, mayor_o_igual, q=q, filtro_estado=estado, pagina=pagina, por_pagina=POR_PAGINA
    )
    total_paginas = max((total + POR_PAGINA - 1) // POR_PAGINA, 1)
    paginas_numeros = _construir_paginas(pagina, total_paginas)

    ahora = datetime.now()
    for p in placas:
        ingreso = datetime.strptime(p["fecha_ingreso"][:19], "%Y-%m-%d %H:%M:%S")
        p["antiguedad_dias"] = (ahora - ingreso).days

    estudio = db.obtener_estudio_mas_reciente()
    if estudio:
        estudio["pendientes"] = max(estudio["total_placas"] - estudio["procesadas"], 0)
        estudio["progreso_pct"] = (
            round(estudio["procesadas"] / estudio["total_placas"] * 100, 1)
            if estudio["total_placas"] else 0
        )
    cola_estudio = db.listar_cola_estudio(estudio["id"]) if estudio else []

    detalle = None
    if placa_seleccionada:
        vehiculo = db.buscar_placa(placa_seleccionada)
        if vehiculo:
            estado_info, snapshot = calcular_estado_informacion(vehiculo["placa"])
            detalle = {
                "vehiculo": vehiculo,
                "estado_informacion": estado_info,
                "snapshot": snapshot,
                "datos": _parsear_datos(snapshot),
            }

    return templates.TemplateResponse(
        request,
        "gestion_masiva.html",
        {
            "request": request,
            "usuario_actual": usuario_actual,
            "periodos": PERIODOS,
            "periodo_actual": periodo,
            "q": q,
            "estado_filtro": estado,
            "pagina": pagina,
            "por_pagina": POR_PAGINA,
            "total_paginas": total_paginas,
            "paginas_numeros": paginas_numeros,
            "metricas": metricas,
            "placas": placas,
            "total_encontradas": total,
            "estudio": estudio,
            "cola_estudio": cola_estudio,
            "placa_seleccionada": placa_seleccionada,
            "detalle": detalle,
            "ahora": datetime.now(),
            "mensaje": mensaje,
            "tipo_mensaje": tipo,
            "ruta_activa": "gestion_masiva",
        },
    )


def _volver_con_filtros(periodo, q, estado, pagina, placa_seleccionada, mensaje, tipo):
    from urllib.parse import quote
    partes = [f"periodo={quote(periodo)}"]
    if q:
        partes.append(f"q={quote(q)}")
    if estado:
        partes.append(f"estado={quote(estado)}")
    if pagina and pagina != 1:
        partes.append(f"pagina={pagina}")
    if placa_seleccionada:
        partes.append(f"placa_seleccionada={quote(placa_seleccionada)}")
    partes.append(f"mensaje={quote(mensaje)}")
    partes.append(f"tipo={quote(tipo)}")
    from fastapi.responses import RedirectResponse
    from starlette.status import HTTP_303_SEE_OTHER
    return RedirectResponse(url="/gestion-masiva?" + "&".join(partes), status_code=HTTP_303_SEE_OTHER)


@router.post("/gestion-masiva/placa/{placa}/priorizar")
def priorizar_placa(
    placa: str,
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    pagina: int = Form(1),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    """
    Etapa 5 §21-24: da prioridad alta a una placa sobre la cola del
    estudio más reciente. Si esa placa no tiene información local
    (SIN_DATOS) o está desactualizada, esto es también lo que hoy
    representa "ejecutar consulta prioritaria" (§23): no hay ninguna
    fuente externa real todavía, así que priorizarla en la cola es toda
    la preparación de arquitectura que corresponde a esta etapa.
    """
    placa = placa.strip().upper()
    vehiculo = db.buscar_placa(placa)
    if vehiculo is None:
        return _volver_con_filtros(periodo, q, estado, pagina, "", f"La placa {placa} no existe.", "error")

    estudio = db.obtener_estudio_mas_reciente()
    if estudio is None or estudio["estado"] in ("CANCELADO", "COMPLETADO"):
        return _volver_con_filtros(
            periodo, q, estado, pagina, placa,
            "No hay ningún estudio masivo en curso para priorizar esta placa. Inicia un estudio primero.",
            "error",
        )

    resultado = db.priorizar_placa_en_cola(estudio["id"], placa)
    db.registrar_log(
        usuario_actual["id"], "PLACA_PRIORIZADA", placa_afectada=placa,
        observaciones=f"Placa {resultado} con prioridad alta en el estudio #{estudio['id']}.",
    )
    return _volver_con_filtros(periodo, q, estado, pagina, placa, f"La placa {placa} quedó con prioridad alta.", "exito")


@router.post("/gestion-masiva/estudio/iniciar")
def iniciar_estudio(
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    periodo = _periodo_valido(periodo)
    mayor_o_igual, fecha_referencia = calcular_condicion_periodo(periodo)
    placas = db.listar_todas_las_placas_periodo(fecha_referencia, mayor_o_igual, q=q, filtro_estado=estado)

    if not placas:
        return _volver_con_filtros(periodo, q, estado, 1, "", "No hay placas para estudiar con estos filtros.", "error")

    estudio_id = db.crear_estudio(usuario_actual["id"], periodo, estado, q, placas)
    db.registrar_log(
        usuario_actual["id"], "ESTUDIO_INICIADO",
        observaciones=(
            f"Estudio #{estudio_id} iniciado. Período: {PERIODOS[periodo]}. "
            f"Placas encoladas: {len(placas)}. Filtro estado: {estado or 'todos'}. Búsqueda: '{q}'."
        ),
    )
    return _volver_con_filtros(periodo, q, estado, 1, "", f"Estudio masivo iniciado con {len(placas)} placa(s).", "exito")


@router.post("/gestion-masiva/estudio/{estudio_id}/procesar-lote")
def procesar_lote_estudio(
    estudio_id: int,
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    """
    Procesa un LOTE ACOTADO (TAMANO_LOTE_ESTUDIO) de la cola, nunca todo
    de una vez (Etapa 5 §25: nada de loops gigantes dentro de una
    petición HTTP). El frontend reenvía este POST automáticamente hasta
    que el estudio se completa, se pausa o se cancela.

    "Procesar" en esta etapa significa resolver el estado de información
    LOCAL de cada placa (sin inventar ni llamar a ninguna fuente
    externa real, que todavía no existe): es exactamente lo que ya se
    haría al mostrar su ficha, solo que aquí se hace en lote y se marca
    el ítem de cola como COMPLETADO para que el progreso avance.
    """
    estudio = db.obtener_estudio(estudio_id)
    if estudio is None:
        return _volver_con_filtros(periodo, q, estado, 1, "", "El estudio indicado no existe.", "error")

    if estudio["estado"] != "EN_PROGRESO":
        return _volver_con_filtros(periodo, q, estado, 1, "", "El estudio no está en progreso: no se procesó ningún lote.", "info")

    lote = db.tomar_siguiente_lote(estudio_id, TAMANO_LOTE_ESTUDIO)

    if not lote:
        db.cambiar_estado_estudio(estudio_id, "COMPLETADO")
        db.registrar_log(usuario_actual["id"], "ESTUDIO_COMPLETADO", observaciones=f"Estudio #{estudio_id} completado.")
        return _volver_con_filtros(periodo, q, estado, 1, "", "Estudio masivo completado.", "exito")

    procesadas_lote, errores_lote = 0, 0
    for item in lote:
        vehiculo = db.buscar_placa(item["placa"])
        if vehiculo is None:
            db.marcar_item_cola(item["id"], "ERROR", error_detalle="La placa ya no existe en el inventario.")
            errores_lote += 1
            continue
        calcular_estado_informacion(item["placa"])  # resuelve/toca el estado local, no inventa datos
        db.marcar_item_cola(item["id"], "COMPLETADO")
        procesadas_lote += 1

    db.incrementar_contadores_estudio(estudio_id, procesadas_delta=procesadas_lote, con_error_delta=errores_lote)
    db.registrar_log(
        usuario_actual["id"], "ESTUDIO_LOTE_PROCESADO",
        observaciones=f"Estudio #{estudio_id}: lote de {len(lote)} procesado ({procesadas_lote} ok, {errores_lote} con error).",
    )
    return _volver_con_filtros(periodo, q, estado, 1, "", "", "info")


@router.post("/gestion-masiva/estudio/{estudio_id}/pausar")
def pausar_estudio(
    estudio_id: int,
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    estudio = db.obtener_estudio(estudio_id)
    if estudio is None or estudio["estado"] != "EN_PROGRESO":
        return _volver_con_filtros(periodo, q, estado, 1, "", "Solo se puede pausar un estudio en progreso.", "error")

    db.cambiar_estado_estudio(estudio_id, "PAUSADO")
    db.registrar_log(usuario_actual["id"], "ESTUDIO_PAUSADO", observaciones=f"Estudio #{estudio_id} pausado.")
    return _volver_con_filtros(periodo, q, estado, 1, "", "Estudio pausado.", "info")


@router.post("/gestion-masiva/estudio/{estudio_id}/reanudar")
def reanudar_estudio(
    estudio_id: int,
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    estudio = db.obtener_estudio(estudio_id)
    if estudio is None or estudio["estado"] != "PAUSADO":
        return _volver_con_filtros(periodo, q, estado, 1, "", "Solo se puede reanudar un estudio pausado.", "error")

    db.cambiar_estado_estudio(estudio_id, "EN_PROGRESO")
    db.registrar_log(usuario_actual["id"], "ESTUDIO_REANUDADO", observaciones=f"Estudio #{estudio_id} reanudado.")
    return _volver_con_filtros(periodo, q, estado, 1, "", "Estudio reanudado.", "exito")


@router.post("/gestion-masiva/estudio/{estudio_id}/cancelar")
def cancelar_estudio(
    estudio_id: int,
    confirmar: str = Form(""),
    periodo: str = Form(PERIODO_POR_DEFECTO),
    q: str = Form(""),
    estado: str = Form(""),
    usuario_actual: dict = Depends(usuarios.requerir_admin),
):
    if confirmar != "SI":
        return _volver_con_filtros(periodo, q, estado, 1, "", "Cancelación rechazada: falta confirmación manual explícita.", "error")

    estudio = db.obtener_estudio(estudio_id)
    if estudio is None or estudio["estado"] not in ("EN_PROGRESO", "PAUSADO"):
        return _volver_con_filtros(periodo, q, estado, 1, "", "Este estudio no se puede cancelar.", "error")

    # Cancelar NUNCA borra cola_estudio ni sus resultados/errores (Etapa 5 §29).
    db.cambiar_estado_estudio(estudio_id, "CANCELADO")
    db.registrar_log(usuario_actual["id"], "ESTUDIO_CANCELADO", observaciones=f"Estudio #{estudio_id} cancelado por el Admin.")
    return _volver_con_filtros(periodo, q, estado, 1, "", "Estudio cancelado. Los resultados obtenidos se conservan.", "info")
