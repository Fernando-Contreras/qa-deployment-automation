"""Guardián anti-fugas: este repo es público, así que ningún archivo puede traer
datos de infraestructura, personas o secretos reales.

Corre en CI en cada push. Si truena, NO lo "arregles" subiendo el patrón:
quita el dato del archivo.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

BINARY = {".png", ".jpg", ".xlsx", ".docx", ".pyc"}

# La marca de la empresa se arma en dos mitades para que ESTE archivo no la contenga.
BRAND = "net" + "cracker"

PATTERNS = {
    "IP privada": r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
    "ruta de usuario real": r"C:\\{1,2}Users\\{1,2}(?!\\*(?:you\b|\.\.\.))",
    "contraseña/token escritos en el código":
        r"(?i)(passw(?:or)?d|token|secret|api[_-]?key)\w*\s*=\s*[\"'](?!https?://)[^\"']{3,}[\"']",
    # Literal con mayúscula, minúscula, dígito Y un símbolo: lo que parece una
    # contraseña aunque la variable no se llame "password" (así se coló una).
    "literal con pinta de contraseña":
        r"""["'](?=[A-Za-z0-9!@#$%^&*._+~\-]*[A-Z])(?=[A-Za-z0-9!@#$%^&*._+~\-]*[a-z])"""
        r"""(?=[A-Za-z0-9!@#$%^&*._+~\-]*\d)(?=[A-Za-z0-9!@#$%^&*._+~\-]*[!@#$%^&*+~])"""
        r"""[A-Za-z0-9!@#$%^&*._+~\-]{8,40}["']""",
    "Bearer token": r"Bearer\s+[A-Za-z0-9._\-]{20,}",
    # Mezcla de letras y dígitos, 40+ caracteres: una rutas con puras letras no cuenta.
    "cadena larga tipo llave": r"\b(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{40,}={0,2}\b",
    "correo real": r"[\w.+-]+@(?!example\.(?:com|org))[\w-]+\.[a-z]{2,}",
    "marca de la empresa": BRAND,
}

# Falsos positivos conocidos y revisados a mano (archivo, patrón).
ALLOWED = {
    ("NTT/Webex_Update.py", "contraseña/token escritos en el código"),  # URL pública de la API de Webex
    ("NTT/List_Envs.py", "literal con pinta de contraseña"),  # comentario con un ejemplo de URL: "46_qa+...+ADM+CI"
}


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "_archive"}


def tracked_text_files():
    """Todo archivo de texto del repo (se recorre la carpeta: las pruebas no lanzan procesos)."""
    paths = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.suffix in BINARY or path.name == Path(__file__).name:
            continue
        paths.append(path.relative_to(ROOT).as_posix())
    return sorted(paths)


@pytest.mark.parametrize("label, pattern", sorted(PATTERNS.items()))
def test_repo_has_no_sensitive_data(label, pattern):
    regex = re.compile(pattern)
    hits = []
    for rel in tracked_text_files():
        if (rel, label) in ALLOWED:
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:100]}")
    assert not hits, f"{label}:\n  " + "\n  ".join(hits[:15])
