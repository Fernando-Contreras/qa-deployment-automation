"""
Credenciales cifradas para los scripts de NTT.

- No hay contraseñas en el código.
- Se guardan cifradas con DPAPI de Windows (la misma API que usa Chrome).
  Solo tu usuario de Windows, en esta máquina, puede descifrarlas.
- Sin librerías extra: funciona igual en Python 3.10, 3.12 y 3.14.

Uso normal:
    from nc_credentials import get_credentials
    USERNAME, PASSWORD = get_credentials("jenkins")

La primera vez te las pide y las guarda. Después ya no pregunta.
Para cambiarlas: py Setup_Credentials.py

Override por variables de entorno (útil para CI / tareas programadas):
    NTT_JENKINS_USER / NTT_JENKINS_PASSWORD
"""

import ctypes
import getpass
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

STORE_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NTT" / "credentials.dat"


class LoginError(Exception):
    """El servidor rechazó el usuario/contraseña (típicamente: ya caducó)."""

    def __init__(self, account="jenkins", detail=""):
        self.account = account
        message = (
            f"\n{'=' * 60}\n"
            f"🔐 LOGIN RECHAZADO{f' — {detail}' if detail else ''}\n"
            f"   ¿Ya cambió tu contraseña? Suele pasar cada 3-4 meses.\n\n"
            f"   Corre esto y vuelve a intentar:\n"
            f"       py Setup_Credentials.py {account}\n"
            f"{'=' * 60}"
        )
        super().__init__(message)


# -------------------------
# DPAPI (cifrado de Windows)
# -------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    # Se regresa el buffer para que no lo recolecte el GC mientras se usa el blob.
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _from_blob(blob: _Blob) -> bytes:
    out = ctypes.create_string_buffer(blob.cbData)
    ctypes.memmove(out, blob.pbData, blob.cbData)
    return out.raw


def _encrypt(data: bytes) -> bytes:
    blob_in, _keep_alive = _to_blob(data)
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "NTT scripts", None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptProtectData falló (no se pudo cifrar).")
    try:
        return _from_blob(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _decrypt(data: bytes) -> bytes:
    blob_in, _keep_alive = _to_blob(data)
    blob_out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptUnprotectData falló (no se pudo descifrar).")
    try:
        return _from_blob(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# -------------------------
# STORE
# -------------------------
def _load_store() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(_decrypt(STORE_PATH.read_bytes()).decode("utf-8"))
    except Exception as exc:
        print(f"⚠ No se pudo leer {STORE_PATH}: {exc}")
        return {}


def _save_store(store: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_bytes(_encrypt(json.dumps(store).encode("utf-8")))


def save_credentials(name: str, username: str, password: str) -> None:
    store = _load_store()
    store[name] = {"username": username, "password": password}
    _save_store(store)
    print(f"🔐 Credenciales de '{name}' guardadas cifradas en {STORE_PATH}")


def list_credentials():
    return sorted(_load_store().keys())


def describe_credentials():
    """Nombre + usuario de cada credencial guardada. La contraseña NUNCA se muestra."""
    store = _load_store()
    return [
        (name, store[name].get("username", "?"), "•" * 8)
        for name in sorted(store)
    ]


def delete_credentials(name: str) -> bool:
    store = _load_store()
    if name not in store:
        return False
    del store[name]
    _save_store(store)
    return True


# -------------------------
# SESIONES DEL NAVEGADOR (storage_state de Playwright)
# -------------------------
# Las cookies de sesión equivalen a una contraseña mientras duren, así que se
# guardan igual que las credenciales: cifradas con DPAPI, nunca en texto plano.
SESSION_MAX_AGE_HOURS = 24


def _session_path(name: str) -> Path:
    # Se lee STORE_PATH al llamar (no al importar) para que sea fácil de probar.
    return STORE_PATH.parent / f"session_{name}.dat"


def save_session_state(name: str, state: dict) -> None:
    """Guarda (cifrado) el storage_state de un contexto de Playwright."""
    path = _session_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {"saved_at": time.time(), "state": state}
    encrypted = _encrypt(json.dumps(payload).encode("utf-8"))

    # Se escribe a un temporal y se reemplaza: si una corrida manual y la
    # programada guardan al mismo tiempo, nadie lee un archivo a medias.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(encrypted)
    tmp.replace(path)


def load_session_state(name: str, max_age_hours: float = SESSION_MAX_AGE_HOURS):
    """
    Regresa el storage_state guardado, o None si no hay, está viejo, o no se
    pudo leer. Nunca truena: sin sesión simplemente se hace login normal.
    """
    path = _session_path(name)
    if not path.exists():
        return None

    try:
        payload = json.loads(_decrypt(path.read_bytes()).decode("utf-8"))
        age_hours = (time.time() - float(payload.get("saved_at", 0))) / 3600
        if age_hours > max_age_hours:
            return None
        state = payload.get("state")
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def forget_session_state(name: str) -> bool:
    path = _session_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def _from_env(name: str):
    prefix = f"NTT_{name.upper().replace('-', '_')}"
    user = os.environ.get(f"{prefix}_USER")
    password = os.environ.get(f"{prefix}_PASSWORD")
    return (user, password) if user and password else None


def get_credentials(name: str = "jenkins", default_username: str = None, prompt: bool = True):
    """Regresa (usuario, contraseña) para 'name'. Los pide una sola vez."""
    from_env = _from_env(name)
    if from_env:
        return from_env

    entry = _load_store().get(name)
    if entry and entry.get("username") and entry.get("password"):
        return entry["username"], entry["password"]

    # NTT_UNATTENDED=1 = corrida sin nadie enfrente: pedirlas colgaría el
    # proceso para siempre (es justo lo que llenaba la compu de zombies).
    unattended = os.environ.get("NTT_UNATTENDED") == "1"

    if unattended or not prompt or not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError(
            f"No hay credenciales guardadas para '{name}'.\n"
            f"Corre:  py Setup_Credentials.py {name}"
        )

    print(f"\n🔐 Primera vez: credenciales para '{name}' (se guardan cifradas, no se vuelven a pedir)")
    username = input(f"   Usuario{f' [{default_username}]' if default_username else ''}: ").strip()
    if not username and default_username:
        username = default_username
    password = getpass.getpass("   Contraseña (no se ve al escribir): ")

    if not username or not password:
        raise RuntimeError("Usuario o contraseña vacíos.")

    save_credentials(name, username, password)
    return username, password


if __name__ == "__main__":
    saved = list_credentials()
    print(f"Archivo: {STORE_PATH}")
    print(f"Guardadas: {', '.join(saved) if saved else '(ninguna)'}")
