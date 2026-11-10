"""
Sesión de Jenkins guardada entre corridas (storage_state de Playwright).

Track_Deployments corre cada 10 min y se logueaba en cada corrida (520 de 702
corridas medidas pagaban 1-2 logins). Ahora la primera guarda la sesión,
cifrada, y las siguientes —y QA_Envs, COM_Envs, MailQAs, SBA— la reusan.

Lo que más importa de estas pruebas:

  * La sesión nunca queda en texto plano (equivale a una contraseña).
  * Si la sesión no existe, está vieja, corrupta o Playwright la rechaza, se
    cae a login normal: nunca rompe una corrida.
  * login_if_needed NO espera los 60 s de do_login cuando ya hay sesión. Sin
    eso, adoptar la sesión en QA_Envs/COM_Envs los habría vuelto 60 s MÁS
    lentos, justo lo contrario de lo que se busca.
"""

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import COM_Envs as com
import QA_Envs as qa
import Track_Deployments as td
import jenkins_web as jw
import nc_credentials as nc
from fakes import FakePage

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI sólo existe en Windows")

COOKIE = "valor-secreto-de-la-cookie-123"
STATE = {
    "cookies": [
        {
            "name": "JSESSIONID",
            "value": COOKIE,
            "domain": "sdci.example.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}
LOGIN_INPUT = 'input[name="j_username"]'


def run(coroutine):
    return asyncio.run(coroutine)


# -------------------------
# Dobles de Playwright
# -------------------------
class FakeContext:
    def __init__(self, state=STATE, boom=False):
        self.state = state
        self.boom = boom

    async def storage_state(self):
        if self.boom:
            raise RuntimeError("storage_state no disponible")
        return self.state


class FakeBrowser:
    """Anota con qué argumentos le piden cada contexto."""

    def __init__(self, log, reject_state=False, context=None):
        self.log = log
        self.reject_state = reject_state
        self.context = context or FakeContext()
        self.closed = False

    async def new_context(self, **kwargs):
        if self.reject_state and "storage_state" in kwargs:
            raise ValueError("state inválido")
        self.log.append(kwargs)
        return self.context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, log, context=None):
        self.log = log
        self.context = context

    async def launch(self, **kwargs):
        return FakeBrowser(self.log, context=self.context)


class FakePlaywright:
    def __init__(self, log, context=None):
        self.chromium = FakeChromium(log, context)


class FakePlaywrightManager:
    """Reemplaza async_playwright(): `async with async_playwright() as pw`."""

    def __init__(self, log, context=None):
        self.log = log
        self.context = context

    async def __aenter__(self):
        return FakePlaywright(self.log, self.context)

    async def __aexit__(self, *exc):
        return False


class BrowserPage(FakePage):
    """FakePage + lo que QA_Envs/COM_Envs le piden a la pestaña de login."""

    async def goto(self, url, **kwargs):
        self.calls.append(["page", "goto", [url], dict(sorted(kwargs.items()))])

    async def close(self):
        self.calls.append(["page", "close", [], {}])


class PageContext(FakeContext):
    """Contexto que reparte pestañas de login y guarda las que reparte."""

    def __init__(self, login_form_present):
        super().__init__()
        self.login_form_present = login_form_present
        self.pages = []

    async def new_page(self):
        page = BrowserPage(counts={LOGIN_INPUT: 1 if self.login_form_present else 0})
        self.pages.append(page)
        return page


def waited_for_login_form(page):
    """¿La pestaña esperó (los 60 s de do_login) a que apareciera el formulario?"""
    return [
        call
        for call in page.calls
        if call[1] == "wait_for_selector" and call[3].get("timeout") == 60000
    ]


# -------------------------
# nc_credentials: el almacén cifrado
# -------------------------
@windows_only
def test_session_round_trip_is_encrypted():
    assert nc.load_session_state("jenkins") is None

    nc.save_session_state("jenkins", STATE)

    assert nc.load_session_state("jenkins") == STATE
    raw = nc._session_path("jenkins").read_bytes()
    assert COOKIE.encode() not in raw
    assert b"JSESSIONID" not in raw and b"cookies" not in raw, "Nada de texto plano en disco"


@windows_only
def test_old_session_is_ignored():
    old = {"saved_at": time.time() - 30 * 3600, "state": STATE}
    nc._session_path("jenkins").parent.mkdir(parents=True, exist_ok=True)
    nc._session_path("jenkins").write_bytes(nc._encrypt(json.dumps(old).encode()))

    assert nc.load_session_state("jenkins") is None, "Con más de 24 h se hace login normal"


@windows_only
def test_corrupt_session_never_raises():
    nc._session_path("jenkins").parent.mkdir(parents=True, exist_ok=True)
    nc._session_path("jenkins").write_bytes(b"esto no es un blob de DPAPI")

    assert nc.load_session_state("jenkins") is None


@windows_only
def test_forget_session_is_idempotent():
    nc.save_session_state("jenkins", STATE)

    assert nc.forget_session_state("jenkins") is True
    assert nc.forget_session_state("jenkins") is False
    assert nc.load_session_state("jenkins") is None


@windows_only
def test_sessions_are_independent_per_name():
    """SBA vive en otro servidor (taci): su sesión no debe pisar la de sdci."""
    other = {"cookies": [], "origins": [{"origin": "https://taci", "localStorage": []}]}

    nc.save_session_state("jenkins", STATE)
    nc.save_session_state("jenkins_sba", other)

    assert nc.load_session_state("jenkins") == STATE
    assert nc.load_session_state("jenkins_sba") == other


@windows_only
def test_saving_leaves_no_temp_files_behind():
    nc.save_session_state("jenkins", STATE)
    nc.save_session_state("jenkins", STATE)

    leftovers = [p.name for p in nc._session_path("jenkins").parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# -------------------------
# jenkins_web: contexto con sesión
# -------------------------
def test_context_is_created_with_the_saved_state():
    log = []

    run(jw.new_context_with_session(FakeBrowser(log), STATE))

    assert log == [{"storage_state": STATE}]


def test_context_without_state_is_a_clean_one():
    log = []

    run(jw.new_context_with_session(FakeBrowser(log), None))

    assert log == [{}]


def test_a_rejected_state_falls_back_to_a_clean_context(capsys):
    log = []

    run(jw.new_context_with_session(FakeBrowser(log, reject_state=True), STATE))

    assert log == [{}]
    assert "login normal" in capsys.readouterr().out


@windows_only
def test_persist_session_saves_what_the_context_has():
    state = run(jw.persist_session(FakeContext()))

    assert state == STATE
    assert nc.load_session_state(jw.SESSION_NAME) == STATE


@windows_only
def test_persist_session_can_use_another_name():
    run(jw.persist_session(FakeContext(), "jenkins_sba"))

    assert nc.load_session_state("jenkins_sba") == STATE
    assert nc.load_session_state(jw.SESSION_NAME) is None


def test_a_failure_while_saving_never_breaks_the_run(capsys):
    assert run(jw.persist_session(FakeContext(boom=True))) is None
    assert "No pude guardar la sesión" in capsys.readouterr().out


@windows_only
def test_saved_session_reads_it_back():
    assert jw.saved_session() is None
    nc.save_session_state(jw.SESSION_NAME, STATE)

    assert jw.saved_session() == STATE


# -------------------------
# login_if_needed: sin la espera de 60 s
# -------------------------
@pytest.mark.parametrize("module", [qa, com], ids=["QA_Envs", "COM_Envs"])
def test_both_scripts_expose_login_if_needed(module):
    assert module.login_if_needed == module._form.login_if_needed


def test_login_if_needed_does_not_wait_when_there_is_a_session(capsys):
    page = FakePage(counts={LOGIN_INPUT: 0})

    logged_in_now = run(qa.login_if_needed(page))

    assert logged_in_now is False
    assert not waited_for_login_form(page), "Esperar 60 s a un formulario que no va a salir"
    assert not [c for c in page.calls if c[1] in ("fill", "press")]
    assert "Ya hay sesión" in capsys.readouterr().out


def test_login_if_needed_logs_in_when_the_form_is_there():
    page = FakePage(counts={LOGIN_INPUT: 1})

    logged_in_now = run(qa.login_if_needed(page))

    assert logged_in_now is True
    fills = [c for c in page.calls if c[1] == "fill"]
    assert [c[2] for c in fills] == [["test-user"], ["test-password"]]
    assert [c for c in page.calls if c[1] == "press"], "Se aprieta Enter para entrar"


def test_do_login_keeps_its_wait_so_the_golden_files_stay_valid():
    """do_login no cambia (sus archivos golden siguen valiendo): sólo se le
    puso un atajo, login_if_needed, para cuando ya hay sesión."""
    page = FakePage()

    run(qa.do_login(page))

    assert len(waited_for_login_form(page)) == 1


# -------------------------
# Track_Deployments: las corridas reusan la sesión
# -------------------------
IN_PROGRESS_DAILY = (
    "## **T2.3.49 — 99CI**\n---\n🔄 **IN PROGRESS**:\n"
    "[**42QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw42_qa/job/Deploy-Parallel/) (T2.3.49)\n"
)


async def _fake_check_all(context, deployments):
    for deployment in deployments:
        deployment.update({"status": "IN_PROGRESS", "started": None})
    return ["IN_PROGRESS"] * len(deployments)


@windows_only
def test_track_deployments_saves_then_reuses_the_session(monkeypatch, daily_envs):
    daily_envs(text=IN_PROGRESS_DAILY)
    contexts = []
    monkeypatch.setattr(td, "async_playwright", lambda: FakePlaywrightManager(contexts))
    monkeypatch.setattr(td, "check_all_deployments", _fake_check_all)

    # Corrida 1: no hay sesión -> contexto limpio, y la deja guardada.
    run(td.main())
    assert contexts == [{}]
    assert nc.load_session_state(jw.SESSION_NAME) == STATE

    # Corrida 2: ya la usa. Este es el ahorro.
    contexts.clear()
    run(td.main())
    assert contexts == [{"storage_state": STATE}]


@windows_only
def test_track_deployments_recovers_from_a_corrupt_session(monkeypatch, daily_envs):
    daily_envs(text=IN_PROGRESS_DAILY)
    contexts = []
    monkeypatch.setattr(td, "async_playwright", lambda: FakePlaywrightManager(contexts))
    monkeypatch.setattr(td, "check_all_deployments", _fake_check_all)
    nc._session_path(jw.SESSION_NAME).parent.mkdir(parents=True, exist_ok=True)
    nc._session_path(jw.SESSION_NAME).write_bytes(b"corrupto")

    run(td.main())

    assert contexts == [{}], "No truena: hace login normal"
    assert nc.load_session_state(jw.SESSION_NAME) == STATE, "Y la repara para la siguiente"


def test_track_deployments_uses_the_shared_helpers():
    assert td.new_context_with_session is jw.new_context_with_session
    assert td.persist_session is jw.persist_session
    assert td.SESSION_NAME == jw.SESSION_NAME


# -------------------------
# QA_Envs / COM_Envs: chequeo y trigger
# -------------------------
@windows_only
def test_qa_envs_check_phase_loads_and_saves_the_session(monkeypatch, daily_envs):
    daily_envs(text=IN_PROGRESS_DAILY)
    nc.save_session_state(jw.SESSION_NAME, {"cookies": [], "origins": []})
    contexts = []

    async def check(context, deployments):
        for deployment in deployments:
            deployment.update({"status": "IN_PROGRESS", "started": datetime.now()})

    monkeypatch.setattr(td, "check_all_deployments", check)

    result = run(qa.auto_targets(FakePlaywright(contexts), ask=False))

    assert result == [], "Lo que está corriendo ahorita no se re-triggerea"
    assert contexts == [{"storage_state": {"cookies": [], "origins": []}}]
    assert nc.load_session_state(jw.SESSION_NAME) == STATE, "Tras chequear, deja la sesión fresca"


def _qa_trigger(monkeypatch, context):
    """Corre QA_Envs.main() en modo manual con un solo env y Playwright de mentira."""
    contexts = []
    monkeypatch.setattr(sys, "argv", ["QA_Envs.py", "--m"])
    monkeypatch.setattr(qa, "async_playwright", lambda: FakePlaywrightManager(contexts, context))
    monkeypatch.setattr(qa, "manual_targets", lambda: [{"env": "06"}])

    async def run_env(ctx, semaphore, target, index, total):
        return target["env"], True, None

    monkeypatch.setattr(qa, "run_env", run_env)
    run(qa.main())
    return contexts


@windows_only
def test_qa_envs_with_a_live_session_does_not_log_in_nor_wait(monkeypatch):
    nc.save_session_state(jw.SESSION_NAME, {"cookies": [], "origins": []})
    context = PageContext(login_form_present=False)

    contexts = _qa_trigger(monkeypatch, context)

    assert contexts == [{"storage_state": {"cookies": [], "origins": []}}]
    (login_page,) = context.pages
    assert not waited_for_login_form(login_page), "Con sesión no se esperan los 60 s"
    assert not [c for c in login_page.calls if c[1] == "fill"], "Y no se teclea el login"
    assert nc.load_session_state(jw.SESSION_NAME) == {"cookies": [], "origins": []}, "Sin login nuevo no se re-guarda"


@windows_only
def test_qa_envs_without_a_session_logs_in_and_saves_it(monkeypatch):
    context = PageContext(login_form_present=True)

    contexts = _qa_trigger(monkeypatch, context)

    assert contexts == [{}]
    (login_page,) = context.pages
    assert [c[2] for c in login_page.calls if c[1] == "fill"] == [["test-user"], ["test-password"]]
    assert nc.load_session_state(jw.SESSION_NAME) == STATE, "Se loguea y la deja para la próxima"


@windows_only
def test_com_envs_logs_in_per_host_only_where_needed_and_saves_once(monkeypatch):
    contexts = []
    context = PageContext(login_form_present=True)
    monkeypatch.setattr(com, "SD_HOST", "https://sdci.example")
    monkeypatch.setattr(com, "SNAPSHOT_HOST", "https://snapci.example")

    async def run_env(ctx, semaphore, target, index, total):
        return target["env"], True, None

    monkeypatch.setattr(com, "run_env", run_env)
    targets = [{"env": "12", "snapshot_mode": False}, {"env": "15", "snapshot_mode": True}]

    run(com.trigger_targets(FakePlaywright(contexts, context), targets, True, 2))

    assert contexts == [{}]
    assert len(context.pages) == 2, "Una pestaña de login por host (sdci y snapci)"
    assert nc.load_session_state(jw.SESSION_NAME) == STATE


@windows_only
def test_com_envs_with_a_live_session_skips_both_logins(monkeypatch):
    nc.save_session_state(jw.SESSION_NAME, {"cookies": [], "origins": []})
    contexts = []
    context = PageContext(login_form_present=False)
    monkeypatch.setattr(com, "SD_HOST", "https://sdci.example")
    monkeypatch.setattr(com, "SNAPSHOT_HOST", "https://snapci.example")

    async def run_env(ctx, semaphore, target, index, total):
        return target["env"], True, None

    monkeypatch.setattr(com, "run_env", run_env)
    targets = [{"env": "12", "snapshot_mode": False}, {"env": "15", "snapshot_mode": True}]

    run(com.trigger_targets(FakePlaywright(contexts, context), targets, True, 2))

    assert contexts == [{"storage_state": {"cookies": [], "origins": []}}]
    for page in context.pages:
        assert not waited_for_login_form(page)
        assert not [c for c in page.calls if c[1] == "fill"]


# -------------------------
# MailQAs y SBA
# -------------------------
# Estos dos arrastran Excel/Outlook (MailQAs) o un formulario enorme (SBA);
# armar un Playwright de mentira para ellos costaría más que lo que cubre. Se
# revisa que el cableado esté, y que SBA use su propio archivo de sesión.
NTT_DIR = Path(nc.__file__).resolve().parent


def test_mailqas_starts_its_browser_with_the_shared_session():
    source = (NTT_DIR / "MailQAs.py").read_text(encoding="utf-8")

    assert "new_context_with_session(browser, saved_session())" in source
    assert "persist_session(context)" in source


def test_sba_has_its_own_session_file_because_it_is_another_server():
    import SBA

    source = (NTT_DIR / "SBA.py").read_text(encoding="utf-8")

    assert SBA.SBA_SESSION_NAME != jw.SESSION_NAME
    assert "new_context_with_session(browser, saved_session(SBA_SESSION_NAME))" in source
    # Se guarda hasta que el formulario del job cargó, no apenas se aprieta Enter.
    assert source.index("await wait_for_job_form(page)\n        # Se guarda") < source.index(
        "persist_session(context, SBA_SESSION_NAME)"
    )
