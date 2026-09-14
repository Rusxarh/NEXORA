"""
crear_admin.py
Script de arranque para asignar credenciales web (username + contraseña)
a un usuario existente, o para crear uno nuevo con credenciales.

Se ejecuta manualmente en tu propia terminal (NO es parte del servidor
FastAPI). La contraseña se pide con getpass (no se muestra en pantalla
mientras se escribe) y solo se guarda su hash — nunca el texto plano —
así que nunca pasa por un chat, un log del servidor, ni queda en el
historial de ninguna conversación.

Uso:
    python crear_admin.py
"""

import getpass
import sqlite3
import sys

import database as db
from usuarios import hash_password


def _pedir_password() -> str:
    while True:
        password = getpass.getpass("Contraseña (mínimo 8 caracteres, no se mostrará): ")
        if len(password) < 8:
            print("La contraseña debe tener al menos 8 caracteres.\n")
            continue
        confirmacion = getpass.getpass("Confirma la contraseña: ")
        if password != confirmacion:
            print("Las contraseñas no coinciden, intenta de nuevo.\n")
            continue
        return password


def _asignar_a_usuario_existente():
    usuario_id_txt = input("id del usuario existente (ej. 1): ").strip()
    if not usuario_id_txt.isdigit():
        print("id inválido.")
        sys.exit(1)
    usuario_id = int(usuario_id_txt)

    usuario = db.obtener_usuario_por_id(usuario_id)
    if not usuario:
        print(f"No existe ningún usuario con id={usuario_id}.")
        sys.exit(1)

    print(f"Usuario encontrado: '{usuario['nombre']}' (rol actual: {usuario['rol']})")

    username = input("Nuevo username para iniciar sesión: ").strip()
    if not username:
        print("El username no puede estar vacío.")
        sys.exit(1)

    password = _pedir_password()

    try:
        db.establecer_credenciales(usuario_id, username, hash_password(password))
    except sqlite3.IntegrityError:
        print(f"El username '{username}' ya está en uso por otro usuario.")
        sys.exit(1)

    print(f"\nListo. '{usuario['nombre']}' ya puede iniciar sesión en /login como '{username}'.")


def _crear_usuario_nuevo():
    telegram_id_txt = input(
        "telegram_id (único; si no usa el bot de Telegram, cualquier número no usado sirve): "
    ).strip()
    if not telegram_id_txt.isdigit():
        print("telegram_id inválido.")
        sys.exit(1)

    nombre = input("Nombre completo: ").strip()
    rol = input(f"Rol {db.ROLES_USUARIO}: ").strip()
    username = input("Username para iniciar sesión: ").strip()

    if not nombre or not username:
        print("Nombre y username son obligatorios.")
        sys.exit(1)

    password = _pedir_password()

    try:
        usuario_id = db.crear_usuario(
            int(telegram_id_txt),
            nombre,
            rol,
            username=username,
            password_hash=hash_password(password),
        )
    except db.ValorInvalidoError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except sqlite3.IntegrityError:
        print("El telegram_id o el username ya están en uso por otro usuario.")
        sys.exit(1)

    print(f"\nListo. Usuario '{nombre}' creado (id={usuario_id}), rol={rol}, username='{username}'.")


def main():
    db.inicializar_db()

    print("=== Master_Placas: alta/actualización de credenciales web ===\n")
    print("1) Asignar usuario/contraseña a un usuario YA EXISTENTE (por id)")
    print("2) Crear un usuario NUEVO con usuario/contraseña")
    opcion = input("Elige 1 o 2: ").strip()

    if opcion == "1":
        _asignar_a_usuario_existente()
    elif opcion == "2":
        _crear_usuario_nuevo()
    else:
        print("Opción inválida.")
        sys.exit(1)


if __name__ == "__main__":
    main()
