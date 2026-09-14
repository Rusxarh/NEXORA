"""
auditoria.py
Módulo de auditoría.

Responsabilidades:
- Centralizar los nombres de acción que se escriben en logs_auditoria,
  para que el resto de la app no use strings mágicos sueltos.
- Registrar eventos de auditoría en su propia transacción, para casos
  que NO formen parte de una operación ya atómica de database.py.
  (El alta y la baja de placas ya registran su log dentro de la misma
  transacción que inserta/actualiza el vehículo, en
  database.insertar_placa / database.cambiar_estado_a_baja, para que
  vehículo y log nunca queden desincronizados; ese código no se duplica
  aquí, se sigue reutilizando tal cual desde vehiculos.py).
- Proveer los filtros (por placa, por usuario_id o por límite) para la
  vista del historial.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request

import database as db
import usuarios
from templates import templates

router = APIRouter()

ACCION_ALTA_PLACA = "ALTA_PLACA"
ACCION_BAJA_PLACA = "BAJA_PLACA"


def registrar_evento(usuario_id: int, accion: str, placa_afectada: str = None) -> int:
    """
    Registra un evento de auditoría en una transacción propia.

    Úsala solo para eventos sueltos (fuera de una operación que ya se
    registra de forma atómica en database.py, como el alta/baja de
    placas). Devuelve el id de la fila insertada en logs_auditoria.
    """
    return db.registrar_log(usuario_id, accion, placa_afectada)


def listar_logs_filtrados(
    placa: Optional[str] = None,
    usuario_id: Optional[int] = None,
    limite: int = 100,
):
    """Aplica los tres filtros soportados por la vista de auditoría:
    por placa, por usuario_id y por límite de resultados."""
    return db.listar_logs(placa=placa, usuario_id=usuario_id, limite=limite)


@router.get("/auditoria")
def auditoria_vista(
    request: Request,
    # Etapa 4.5 §7: el módulo administrativo completo de auditoría (todos
    # los eventos, con filtros) queda restringido a Admin, para que un
    # Operador no pueda consultar indiscriminadamente toda la actividad
    # del sistema. Antes de esta etapa cualquier usuario autenticado podía
    # verlo; cambio de comportamiento deliberado. Las acciones del
    # Operador se siguen registrando igual (eso no cambia): solo cambia
    # quién puede *consultar* el registro completo.
    usuario_actual: dict = Depends(usuarios.requerir_admin),
    placa: str = "",
    usuario_id: str = "",
    limite: int = 100,
):
    usuario_id_filtro = int(usuario_id) if usuario_id.strip().isdigit() else None

    logs = listar_logs_filtrados(
        placa=placa.strip() or None,
        usuario_id=usuario_id_filtro,
        limite=limite,
    )

    return templates.TemplateResponse(
        request,
        "auditoria.html",
        {
            "request": request,
            "logs": logs,
            "placa": placa,
            "usuario_id": usuario_id,
            "limite": limite,
            "usuario_actual": usuario_actual,
            "ruta_activa": "auditoria",
        },
    )
