"""
Fixtures compartidas por todas las pruebas.

Reglas de la casa, para que una prueba NUNCA toque el mundo real:

  - Credenciales falsas por variable de entorno. nc_credentials las lee antes
    que el archivo cifrado, y varios scripts las piden en cuanto se importan.
  - NTT_UNATTENDED=1: nada se queda esperando a que alguien teclee.
  - subprocess, la red e input() bloqueados. Si una prueba llegara a lanzar
    Webex_Update, QA_Envs o a pegarle a una API, truena en vez de publicar en
    la sala o disparar un deployment de verdad.

Los scripts leen y escriben Daily_Envs.txt en una ruta fija. El fixture
`daily_envs` copia un ejemplo a una carpeta temporal y apunta todos los
scripts que ya estén importados hacia esa copia.
"""

import builtins
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

# Esto tiene que pasar ANTES de que cualquier prueba importe un script.
for _account in ("JENKINS", "NTTESTS", "JIRA", "TOMS", "ATP_SIT_WEB", "PUTTY_SIT1", "PUTTY_ATP_SIT"):
    os.environ[f"NTT_{_account}_USER"] = "test-user"
    os.environ[f"NTT_{_account}_PASSWORD"] = "test-password"
os.environ["NTT_UNATTENDED"] = "1"

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = FIXTURES / "expected"
DAILY_ENVS_SAMPLES = sorted(p.name for p in (FIXTURES / "daily_envs").glob("*.txt"))

# Dónde guarda cada script la ruta del Daily_Envs. Se respeta el tipo original
# (str o Path) para que la prueba no cambie cómo se comporta el script.
DAILY_ENVS_ATTRS = {
    "OrganizeResults": ("FILE_PATH",),
    "Track_Deployments": ("FILE_PATH",),
    "Results_Sanity": ("DAILY_ENVS_PATH",),
    "Deployment_Defect": ("DAILY_ENVS",),
    "List_Envs": ("DAILY_ENVS_PATH",),
    "Sanity_COM": ("DAILY_ENVS_PATH",),
    "MailQAs": ("DAILY_ENVS_PATH", "txt_path"),
    "Webex_Update": ("DEFAULT_DAILY_ENVS",),
}


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="Reescribe los resultados esperados (tests/fixtures/expected) con lo que sale hoy.",
    )


# -------------------------
# RED DE SEGURIDAD
# -------------------------
# En Python 3.10 la librería estándar corre "ver" para saber la versión de
# Windows (platform.uname), y pyautogui la pide al importarse. Se calcula
# aquí una vez, antes de bloquear procesos; después ya sale del caché.
platform.uname()


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    def blocked_process(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        raise AssertionError(f"Una prueba intentó lanzar un proceso: {command!r}")

    def blocked_network(*args, **kwargs):
        raise AssertionError("Una prueba intentó salir a la red")

    def blocked_input(prompt=""):
        raise AssertionError(f"Una prueba se quedó esperando input(): {prompt!r}")

    monkeypatch.setattr(subprocess, "run", blocked_process)
    monkeypatch.setattr(subprocess, "Popen", blocked_process)
    monkeypatch.setattr(urllib.request, "urlopen", blocked_network)
    monkeypatch.setattr(builtins, "input", blocked_input)


@pytest.fixture(autouse=True)
def isolated_secret_store(tmp_path, monkeypatch):
    """
    Credenciales y sesiones de Jenkins viven cifradas en %LOCALAPPDATA%\\NTT.
    Una prueba nunca debe leer ni escribir ahí: cargaría TU sesión real de
    Jenkins (y se volvería distinta según el día), o la pisaría con una de
    mentira. Todo va a una carpeta temporal.
    """
    import nc_credentials

    monkeypatch.setattr(nc_credentials, "STORE_PATH", tmp_path / "secretos" / "credentials.dat")


# -------------------------
# DATOS DE EJEMPLO
# -------------------------
@pytest.fixture
def fixture_text():
    """Lee un archivo de tests/fixtures como texto (saltos de línea ya normalizados)."""

    def read(relative):
        return (FIXTURES / relative).read_text(encoding="utf-8")

    return read


@pytest.fixture
def daily_envs(tmp_path, monkeypatch, fixture_text):
    """
    Crea un Daily_Envs.txt temporal y apunta los scripts hacia él.

        path = daily_envs("multi_block.txt")          # uno de los ejemplos
        path = daily_envs(text="## **X**\\n---\\n...")  # o texto a la medida
    """

    def make(sample=None, text=None):
        if text is None:
            text = fixture_text(f"daily_envs/{sample}")

        path = tmp_path / "Daily_Envs.txt"
        path.write_text(text, encoding="utf-8")

        for module_name, attrs in DAILY_ENVS_ATTRS.items():
            module = sys.modules.get(module_name)
            if module is None:
                continue
            for attr in attrs:
                if hasattr(module, attr):
                    original = getattr(module, attr)
                    monkeypatch.setattr(module, attr, path if isinstance(original, Path) else str(path))

        organize = sys.modules.get("OrganizeResults")
        if organize is not None:
            monkeypatch.setattr(organize, "BACKUP_PATH", path.with_suffix(".txt.bak"))

        return path

    return make


# -------------------------
# GOLDEN MASTER
# -------------------------
@pytest.fixture
def golden(request):
    """
    Compara un resultado contra tests/fixtures/expected/<nombre>.

    Los esperados se generaron corriendo los scripts ANTES de refactorizarlos,
    así que si algo cambia de comportamiento, esta prueba lo grita. Si el
    cambio es a propósito:  pytest --update-golden
    """
    update = request.config.getoption("--update-golden")

    def check(name, actual):
        path = EXPECTED / name
        if not isinstance(actual, str):
            actual = json.dumps(actual, indent=2, ensure_ascii=False, default=str) + "\n"

        if update or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
            if not update:
                pytest.fail(f"No existía {path.name}; lo creé. Revísalo y vuelve a correr.")
            return

        expected = path.read_text(encoding="utf-8")
        assert actual == expected, f"Cambió el resultado de {name}"

    return check
