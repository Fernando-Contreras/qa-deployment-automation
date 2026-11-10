"""
Page Object de Jenkins (pages.JenkinsBuildPage).

La prueba importante compara contra la MISMA grabación que ya existía de
configure_sd_file (tests/fixtures/expected/jenkins_form_sd_file__*.json): si el
Page Object hace un solo click, espera o texto distinto, esa comparación truena.
"""

import asyncio

import pytest

import COM_Envs as com
import QA_Envs as qa
from fakes import FakePage
from pages import JenkinsBuildPage, JenkinsLoginPage

MODULES = {"qa": qa, "com": com}


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_page_object_deploys_exactly_like_the_original_flow(name, golden, capsys):
    page = FakePage()
    jenkins = JenkinsBuildPage(page, MODULES[name]._form, "06")

    run(jenkins.deploy_sd_file("file://storage/sd.yaml", 2, 5))

    # Mismo nombre de archivo que usa test_jenkins_forms: si no es idéntico, falla.
    golden(f"jenkins_form_sd_file__{name}.json", {"calls": page.calls, "output": capsys.readouterr().out})


USERNAME_FIELD = 'input[name="j_username"]'


@pytest.mark.parametrize("name", sorted(MODULES))
@pytest.mark.parametrize("session_saved", [False, True], ids=["sin_sesion", "con_sesion_guardada"])
def test_login_page_does_exactly_what_login_if_needed_does(name, session_saved, capsys):
    counts = {USERNAME_FIELD: 0} if session_saved else {}
    module = MODULES[name]

    old_page = FakePage(counts=dict(counts))
    old_result = run(module.login_if_needed(old_page))
    old_output = capsys.readouterr().out

    new_page = FakePage(counts=dict(counts))
    new_result = run(JenkinsLoginPage(new_page, module._form).sign_in())
    new_output = capsys.readouterr().out

    assert new_result == old_result == (not session_saved)
    assert new_page.calls == old_page.calls
    assert new_output == old_output


def test_login_page_with_a_saved_session_never_types_credentials():
    page = FakePage(counts={USERNAME_FIELD: 0})

    assert run(JenkinsLoginPage(page, qa._form).sign_in()) is False
    assert not [c for c in page.calls if c[1] == "fill"]


def test_login_page_locators_and_open():
    page = TabWithGoto()
    login = JenkinsLoginPage(page, qa._form)

    assert login.username_field.path == USERNAME_FIELD
    assert login.password_field.path == 'input[name="j_password"]'
    run(login.open("https://jenkins.example.com/job/"))
    assert page.calls[0] == [
        "page", "goto", ["https://jenkins.example.com/job/"],
        {"timeout": 1200000, "wait_until": "domcontentloaded"},
    ]


def test_the_page_object_keeps_the_tab_and_env_so_callers_do_not_repeat_them():
    page = FakePage()
    jenkins = JenkinsBuildPage(page, qa._form, "06")

    assert jenkins.page is page and jenkins.env == "06"


def test_locators_live_in_one_place():
    jenkins = JenkinsBuildPage(FakePage(), qa._form, "06")

    assert jenkins.parameter("SD_FILE").path == 'div.jenkins-form-item:has-text("SD_FILE")'
    assert jenkins.checkbox("CLEANUP_FS").path == (
        'div.jenkins-form-item:has-text("CLEANUP_FS").first >> input[type="checkbox"]'
    )
    assert jenkins.build_button.path == "role=button[Build].first"


def test_enable_a_checkbox_goes_through_the_verified_click(capsys):
    page = FakePage(checked={'div.jenkins-form-item:has-text("CLEANUP_FS") >> input[type="checkbox"]': True})
    jenkins = JenkinsBuildPage(page, qa._form, "06")

    run(jenkins.enable("CLEANUP_FS"))

    assert "✅ CLEANUP_FS clicked." in capsys.readouterr().out


class TabWithGoto(FakePage):
    async def goto(self, url, **kwargs):
        self.calls.append(["page", "goto", [url], dict(sorted(kwargs.items()))])


class ContextWithOneTab:
    def __init__(self):
        self.tab = TabWithGoto()

    async def new_page(self):
        return self.tab


@pytest.mark.parametrize("name", sorted(MODULES))
def test_run_env_with_an_sd_file_still_deploys_through_the_page_object(name, capsys):
    """El flujo completo de cada script (abrir, esperar, llenar, Build) con el Page Object."""
    module = MODULES[name]
    context = ContextWithOneTab()
    target = {
        "env": "06", "payload": "file://storage/sd.yaml", "snapshot_mode": False,
        "tenant": None, "source_url": "",
    }

    async def go():
        return await module.run_env(context, asyncio.Semaphore(1), target, 1, 1)

    assert run(go()) == ("06", True, None)

    calls = context.tab.calls
    assert calls[0][1] == "goto" and "Deploy-Parallel" in calls[0][2][0]
    fills = [c for c in calls if c[1] == "fill"]
    assert fills and fills[0][2] == ["file://storage/sd.yaml"]
    assert [c for c in calls if c[1] == "click"][-1][0] == "role=button[Build].first"
    assert "Build submitted" in capsys.readouterr().out


def test_open_goes_to_the_url_and_waits_for_the_form():
    class TabThatRemembersGoto(FakePage):
        async def goto(self, url, **kwargs):
            self.calls.append(["page", "goto", [url], dict(sorted(kwargs.items()))])

    page = TabThatRemembersGoto()
    jenkins = JenkinsBuildPage(page, qa._form, "06")

    run(jenkins.open("https://jenkins.example.com/job/x/build"))

    assert page.calls[0] == [
        "page", "goto", ["https://jenkins.example.com/job/x/build"],
        {"timeout": 1200000, "wait_until": "domcontentloaded"},
    ]
    assert any(c[1] == "wait_for_load_state" for c in page.calls[1:])
