"""
templates.py
Instancia compartida de Jinja2Templates.

Existe solo para que usuarios.py, vehiculos.py, auditoria.py y main.py
reutilicen el mismo motor de plantillas (y su mismo caché) en vez de
crear un jinja2.Environment distinto por módulo.
"""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
