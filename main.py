"""
main.py
Dashboard Web Admin para Master_Placas.

Punto de entrada de la aplicación FastAPI. Este archivo NO contiene
lógica de negocio: inicializa la base de datos, une los routers de
cada módulo de dominio (usuarios, vehiculos, auditoria) mediante
FastAPI APIRouter, y registra los exception_handler globales que
traducen los fallos de autenticación/autorización en redirects.

La única ruta que vive directamente aquí es /dashboard, porque combina
datos de los tres módulos a la vez (vehículos, usuarios y logs) y no
pertenece exclusivamente a ninguno de ellos.
"""

from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_303_SEE_OTHER

import auditoria
import database as db
import gestion_masiva
import usuarios
import vehiculos
from templates import templates

app = FastAPI(title="Master_Placas - Dashboard Admin")

# Sirve static/ (hoy solo static/css/app.css) en /static, para que los
# estilos se carguen desde el propio servidor y no de un CDN externo.
app.mount("/static", StaticFiles(directory="static"), name="static")

# Garantiza que la base de datos, las tablas y las columnas de login
# existan al arrancar (migración aditiva, no destructiva).
db.inicializar_db()

app.include_router(usuarios.router)
app.include_router(vehiculos.router)
app.include_router(auditoria.router)
app.include_router(gestion_masiva.router)


# ---------------------------------------------------------------------------
# Traducción de errores de autenticación/autorización a redirects.
#
# Las Dependencies usuarios.requerir_autenticacion / requerir_admin lanzan
# estas excepciones en vez de devolver un 401/403 crudo, para que la
# experiencia siga el mismo patrón de "mensaje flash" que el resto de la
# app. Centralizarlo aquí evita repetir un try/except de permisos en cada
# una de las rutas protegidas.
# ---------------------------------------------------------------------------

@app.exception_handler(usuarios.NoAutenticadoError)
def _redirigir_a_login(request: Request, exc: usuarios.NoAutenticadoError):
    siguiente = quote(request.url.path)
    mensaje = quote(str(exc))
    return RedirectResponse(
        url=f"/login?siguiente={siguiente}&mensaje={mensaje}&tipo=error",
        status_code=HTTP_303_SEE_OTHER,
    )


@app.exception_handler(usuarios.PermisoDenegadoError)
def _redirigir_por_permiso(request: Request, exc: usuarios.PermisoDenegadoError):
    mensaje = quote(str(exc))
    return RedirectResponse(
        url=f"/?mensaje={mensaje}&tipo=error",
        status_code=HTTP_303_SEE_OTHER,
    )


@app.get("/dashboard")
def dashboard(
    request: Request,
    usuario_actual: dict = Depends(usuarios.requerir_autenticacion),
):
    # Conteos agregados en SQL (una consulta por tabla) en vez de traer
    # todas las filas y contarlas en Python: más rápido y evita repetir
    # la misma consulta grande cada vez que se recarga el Dashboard.
    resumen_vehiculos = db.obtener_resumen_vehiculos()
    resumen_usuarios = db.obtener_resumen_usuarios()
    logs_recientes = auditoria.listar_logs_filtrados(limite=8)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "total_vehiculos": resumen_vehiculos["total"],
            "total_activos": resumen_vehiculos["activos"],
            "total_bajas": resumen_vehiculos["bajas"],
            "total_carros_activos": resumen_vehiculos["carros_activos"],
            "total_motos_activas": resumen_vehiculos["motos_activas"],
            "total_usuarios": resumen_usuarios["total"],
            "total_usuarios_activos": resumen_usuarios["activos"],
            "logs_recientes": logs_recientes,
            "usuario_actual": usuario_actual,
            "ruta_activa": "dashboard",
        },
    )


def _detectar_ip_red_local() -> str:
    """
    Descubre la IP de la interfaz de red local (LAN) del equipo SIN
    hardcodear ninguna dirección: abre un socket UDP hacia una IP pública
    conocida (no se envía ningún dato, solo sirve para que el sistema
    operativo elija qué interfaz de salida usar) y lee la IP local que
    quedó asignada a ese socket. Si no hay red disponible, cae a
    127.0.0.1. Esto es necesario porque la IP de la red local puede
    cambiar (otra red Wi-Fi, otro router, etc.) y no debe quedar fija en
    el código.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    # Ejecutar con `python main.py` arranca el servidor escuchando en
    # 0.0.0.0:8000 (todas las interfaces), lo que permite tanto el acceso
    # local (127.0.0.1:8000) como el acceso desde otros dispositivos de la
    # misma red local (por ejemplo, un teléfono conectado al mismo Wi-Fi)
    # usando la IP LAN del equipo. No cambia nada de la seguridad de la
    # aplicación: sigue siendo exactamente el mismo login/sesiones/roles,
    # solo cambia qué interfaz de red acepta la conexión TCP.
    import uvicorn

    ip_local = _detectar_ip_red_local()
    print("MASTER_PLACAS iniciado correctamente")
    print()
    print("Acceso local:")
    print("  http://127.0.0.1:8000")
    print()
    print("Acceso desde la red local:")
    print(f"  http://{ip_local}:8000")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000)
