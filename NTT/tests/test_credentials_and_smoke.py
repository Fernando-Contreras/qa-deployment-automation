"""nc_credentials (DPAPI) y una revisión rápida de que TODOS los scripts cargan."""

import importlib
import py_compile
import sys
from pathlib import Path

import pytest

import nc_credentials

NTT_DIR = Path(nc_credentials.__file__).resolve().parent
GCI_DIR = NTT_DIR.parent / "GCI"

# Scripts que, por diseño, arrancan solos al abrirse (esperan teclas/input).
# De éstos sólo se revisa que compilen.
RUN_ON_IMPORT = {"GCIBilling"}

SCRIPTS = sorted(p for p in NTT_DIR.glob("*.py")) + sorted(p for p in GCI_DIR.glob("*.py"))


# -------------------------
# CREDENCIALES
# -------------------------
def test_environment_variables_win(monkeypatch):
    monkeypatch.setenv("NTT_SOME_ACCOUNT_USER", "u")
    monkeypatch.setenv("NTT_SOME_ACCOUNT_PASSWORD", "p")

    assert nc_credentials.get_credentials("some-account") == ("u", "p")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI sólo existe en Windows")
def test_credentials_round_trip_encrypted(tmp_path, monkeypatch):
    store = tmp_path / "credentials.dat"
    monkeypatch.setattr(nc_credentials, "STORE_PATH", store)

    nc_credentials.save_credentials("demo", "alex", "s3cr3t")

    assert b"s3cr3t" not in store.read_bytes(), "Se guarda cifrado, nunca en texto plano"
    assert nc_credentials.get_credentials("demo", prompt=False) == ("alex", "s3cr3t")
    assert nc_credentials.describe_credentials() == [("demo", "alex", "•" * 8)]
    assert nc_credentials.delete_credentials("demo") is True
    assert nc_credentials.list_credentials() == []


def test_missing_credentials_never_hang_an_unattended_run(tmp_path, monkeypatch):
    monkeypatch.setattr(nc_credentials, "STORE_PATH", tmp_path / "vacio.dat")

    with pytest.raises(RuntimeError, match="Setup_Credentials.py nadie"):
        nc_credentials.get_credentials("nadie")


# -------------------------
# SMOKE
# -------------------------
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_script_compiles(script, tmp_path):
    py_compile.compile(str(script), cfile=str(tmp_path / "x.pyc"), doraise=True)


@pytest.mark.parametrize(
    "script",
    [p for p in SCRIPTS if p.stem not in RUN_ON_IMPORT],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_every_script_imports_without_side_effects(script, monkeypatch):
    """Importar un script no debe correr nada: ni abrir navegador, ni Excel, ni pedir datos."""
    monkeypatch.syspath_prepend(str(script.parent))
    importlib.import_module(script.stem)
