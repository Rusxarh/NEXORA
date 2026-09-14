"""
camara.py
Grado Operativo y control de actividad del servicio CAMARA (Etapa 4.6).

Este módulo NO implementa el bot de Telegram ni CAMARA en sí (ninguno de
los dos existe todavía). Contiene únicamente la lógica de negocio pura
para que, cuando ambos se construyan, puedan conectarse aquí:

- Cálculo de la antigüedad/"grado" de una placa a partir de su
  fecha_ingreso (NUNCA se guarda ni se modifica en la placa: se calcula
  al vuelo cada vez que se necesita).
- Verificación de si el Grado Operativo de un Operador le permite
  trabajar una placa según esa antigüedad calculada.
- Cálculo del estado CAMARA (ACTIVA / EN_ALERTA / SUSPENDIDA) de un
  Operador según los cortes de calendario del día 15 y el último día de
  cada mes (nunca "N días desde la última actividad").

No hay ningún proceso en segundo plano/cron en este proyecto todavía (y
esta etapa no debe introducir uno: no hay APScheduler, n8n ni tarea
programada). El estado CAMARA se recalcula de forma perezosa cada vez que
hace falta mostrarlo (ver usuarios.usuarios_vista / usuario_ficha), lo
cual es sistemáticamente correcto porque evaluar_estado_camara es una
función pura de (estado guardado, última actividad, fecha actual).
"""

import calendar
from datetime import date, datetime, timedelta
from typing import Optional

import database as db

GRADO_A_NUMERO = {"Grado 1": 1, "Grado 2": 2, "Grado 3": 3}


# ---------------------------------------------------------------------------
# Grado Operativo vs. antigüedad de placa
# ---------------------------------------------------------------------------

def _a_fecha(valor) -> date:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    # Los timestamps de SQLite llegan como "YYYY-MM-DD HH:MM:SS".
    return datetime.strptime(str(valor)[:19], "%Y-%m-%d %H:%M:%S").date()


def calcular_grado_placa(fecha_ingreso, ahora: Optional[datetime] = None) -> int:
    """
    Clasifica la antigüedad de una placa (Etapa 4.6 §12), SIN modificarla
    ni guardar nada: 0-14 días -> 1, 15-29 días -> 2, 30+ días -> 3.
    """
    ahora_fecha = (ahora or datetime.now()).date()
    antiguedad_dias = (ahora_fecha - _a_fecha(fecha_ingreso)).days
    if antiguedad_dias >= 30:
        return 3
    if antiguedad_dias >= 15:
        return 2
    return 1


def operador_puede_trabajar_placa(grado_operativo: str, fecha_ingreso_placa, ahora: Optional[datetime] = None) -> bool:
    """
    El Grado Operativo del Operador es su nivel MÍNIMO de antigüedad
    permitido, no un valor exacto (Etapa 4.6 §13): Grado 1 ve toda placa,
    Grado 2 placas de 15+ días, Grado 3 placas de 30+ días. Nunca toca la
    placa: solo calcula su antigüedad al vuelo.
    """
    if grado_operativo not in GRADO_A_NUMERO:
        return False
    return GRADO_A_NUMERO[grado_operativo] <= calcular_grado_placa(fecha_ingreso_placa, ahora)


# ---------------------------------------------------------------------------
# Cortes de calendario CAMARA (día 15 y último día del mes)
# ---------------------------------------------------------------------------

def limites_periodo(fecha: date) -> tuple:
    """
    (inicio_periodo, fecha_corte) del período de control CAMARA que
    contiene `fecha`. Dos períodos por mes, NUNCA "cada 15 días" desde la
    última actividad:
      - Período 1: día 1 al día 15 (corte: día 15).
      - Período 2: día 16 al último día real del mes (28/29/30/31, según
        `calendar.monthrange`, nunca un número fijo).
    """
    ultimo_dia_mes = calendar.monthrange(fecha.year, fecha.month)[1]
    if fecha.day <= 15:
        return date(fecha.year, fecha.month, 1), date(fecha.year, fecha.month, 15)
    return date(fecha.year, fecha.month, 16), date(fecha.year, fecha.month, ultimo_dia_mes)


def evaluar_estado_camara(usuario: dict, ahora: Optional[datetime] = None) -> str:
    """
    Recalcula (SIN persistir) cuál debería ser el estado CAMARA de un
    Operador, a partir de su última actividad real y su estado guardado.

    Reglas (Etapa 4.6 §19-24, CORREGIDAS por Etapa 5 §36 — ver nota abajo):
    - Si el estado guardado es SUSPENDIDA, se devuelve SUSPENDIDA sin
      excepción: ni la actividad real ni el inicio de un nuevo período la
      levantan. Solo un Admin puede levantarla (reactivar_camara_manual).
    - Si NO está SUSPENDIDA y hubo actividad real dentro del período de
      control actual (desde el inicio del período hasta `ahora`), el
      estado es ACTIVA (esto sí resuelve un EN_ALERTA).
    - Si no hay actividad en el período actual:
        - antes del día de alerta (corte - 1 día): se conserva el estado
          guardado (no se adelanta la alerta).
        - desde el día de alerta hasta el día anterior al corte: EN_ALERTA.
        - desde el día del corte en adelante: SUSPENDIDA.
    - Un Admin no tiene estado CAMARA (el campo no aplica): se devuelve
      tal cual esté guardado, sin evaluar cortes.

    NOTA — CORRECCIÓN ETAPA 5 §36: la Etapa 4.6 interpretó la "regla
    fundamental" de §23 ("la actividad reinicia el período") de forma
    amplia, dejando que actividad real dentro del período también
    levantara una SUSPENDIDA. La Etapa 5 §36 e ítem 31 de sus notas
    obligatorias corrigen esto explícitamente: "la actividad del Operador
    NO puede reactivar automáticamente CAMARA después de una suspensión"
    y "SOLO ADMIN puede reactivar CAMARA". Este es un cambio de
    comportamiento deliberado respecto a la Etapa 4.6, no una regresión:
    ver el informe de la Etapa 5 para el detalle y las pruebas que se
    actualizaron en consecuencia.
    """
    ahora = ahora or datetime.now()
    if usuario.get("rol") != "Operador":
        return usuario.get("estado_camara") or "ACTIVA"

    estado_actual = usuario.get("estado_camara") or "ACTIVA"

    if estado_actual == "SUSPENDIDA":
        return "SUSPENDIDA"

    ultima_actividad_str = usuario.get("ultima_actividad_camara")
    inicio_periodo, corte = limites_periodo(ahora.date())
    alerta_desde = corte - timedelta(days=1)

    if ultima_actividad_str:
        ultima_actividad = _a_fecha(ultima_actividad_str)
        if inicio_periodo <= ultima_actividad <= ahora.date():
            return "ACTIVA"

    if ahora.date() >= corte:
        return "SUSPENDIDA"
    if ahora.date() >= alerta_desde:
        return "EN_ALERTA"
    return "ACTIVA"


def sincronizar_estado_camara(usuario_id: int, ahora: Optional[datetime] = None) -> Optional[dict]:
    """
    Recalcula el estado CAMARA de un Operador y, si cambió respecto al
    valor guardado, lo persiste, lo audita y encola el evento de
    notificación correspondiente (para el futuro consumidor de Telegram).

    Pensada para invocarse de forma perezosa cada vez que se muestra el
    estado de un usuario (lista/ficha de Usuarios): como
    evaluar_estado_camara es una función pura de la fecha actual, no hace
    falta ningún proceso en segundo plano para que el corte "ocurra" en el
    momento correcto.
    """
    usuario = db.obtener_usuario_por_id(usuario_id)
    if usuario is None or usuario["rol"] != "Operador":
        return usuario

    estado_anterior = usuario["estado_camara"] or "ACTIVA"
    nuevo_estado = evaluar_estado_camara(usuario, ahora)

    if nuevo_estado != estado_anterior:
        db.actualizar_estado_camara(usuario_id, nuevo_estado)
        evento = {
            "EN_ALERTA": "CAMERA_WARNING",
            "SUSPENDIDA": "CAMERA_SUSPENDED",
            "ACTIVA": "CAMERA_REACTIVATED",
        }[nuevo_estado]
        db.crear_evento_notificacion(usuario_id, evento)
        db.registrar_log(
            usuario_id,
            f"CAMARA_{nuevo_estado}",
            observaciones=(
                f"Cambio automático de estado CAMARA por corte de calendario: "
                f"{estado_anterior} -> {nuevo_estado}."
            ),
        )
        usuario["estado_camara"] = nuevo_estado

    return usuario


# ---------------------------------------------------------------------------
# Actividad real de CAMARA y reactivación manual
# ---------------------------------------------------------------------------

def registrar_actividad_camara(usuario_id: int, ahora: Optional[datetime] = None) -> dict:
    """
    Punto de entrada para cuando CAMARA (todavía no implementada) reporte
    una detección/evento real de un Operador. Etapa 4.6 §16: iniciar
    sesión en la web, abrir Telegram, abrir Dashboard, consultar
    Inventario o enviar mensajes administrativos NO cuentan como
    actividad — esta función solo debe invocarse desde el futuro CAMARA.

    Etapa 5 §36: si CAMARA está SUSPENDIDA, la actividad se REGISTRA (el
    dato de última actividad se guarda igual, para trazabilidad) pero NO
    reactiva el servicio — eso ahora requiere reactivar_camara_manual.
    Solo resuelve un EN_ALERTA de vuelta a ACTIVA.
    """
    ahora = ahora or datetime.now()
    usuario = db.obtener_usuario_por_id(usuario_id)
    if usuario is None:
        raise db.RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")

    estado_anterior = usuario["estado_camara"] or "ACTIVA"
    momento = ahora.strftime("%Y-%m-%d %H:%M:%S")

    db.registrar_actividad_camara_db(usuario_id, momento)
    db.crear_evento_notificacion(usuario_id, "CAMERA_ACTIVITY_REGISTERED")

    detalle = f"Actividad CAMARA registrada en {momento}."
    if estado_anterior == "SUSPENDIDA":
        detalle += " CAMARA sigue SUSPENDIDA: la actividad no la reactiva, requiere reactivación manual del Admin."
    db.registrar_log(usuario_id, "CAMARA_ACTIVIDAD_REGISTRADA", observaciones=detalle)

    if estado_anterior == "EN_ALERTA":
        db.crear_evento_notificacion(usuario_id, "CAMERA_REACTIVATED")
        db.registrar_log(
            usuario_id, "CAMARA_ACTIVA",
            observaciones="CAMARA reactivada automáticamente por actividad real: EN_ALERTA -> ACTIVA.",
        )

    return db.obtener_usuario_por_id(usuario_id)


def reactivar_camara_manual(usuario_id: int, admin_id: int, motivo: Optional[str] = None) -> dict:
    """
    Reactivación MANUAL de CAMARA por un Admin (Etapa 4.6 §26),
    independiente de la reactivación del usuario y de la actividad real
    (registrar_actividad_camara). Deja auditoría con estado anterior,
    estado nuevo y motivo.
    """
    usuario = db.obtener_usuario_por_id(usuario_id)
    if usuario is None:
        raise db.RegistroNoEncontradoError(f"Usuario id={usuario_id} no existe")

    estado_anterior = usuario["estado_camara"] or "ACTIVA"
    db.actualizar_estado_camara(usuario_id, "ACTIVA")
    db.crear_evento_notificacion(usuario_id, "CAMERA_REACTIVATED")

    detalle = (
        f"Usuario objetivo: {usuario['nombre']} (id={usuario_id}). "
        f"Estado anterior: {estado_anterior} -> ACTIVA."
    )
    if motivo:
        detalle += f" Motivo: {motivo}"
    db.registrar_log(admin_id, "CAMARA_REACTIVADA_MANUAL", observaciones=detalle)

    return db.obtener_usuario_por_id(usuario_id)
