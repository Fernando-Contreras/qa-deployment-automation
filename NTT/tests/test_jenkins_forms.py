"""
Llenado del formulario de Jenkins (QA_Envs para nttcw, COM_Envs para nttcom).

Se corre contra FakePage: la "bitácora" de clicks, esperas y textos quedó
grabada antes del refactor. Si cambia un solo paso, esto lo detecta.
"""

import asyncio

import pytest

import COM_Envs as com
import QA_Envs as qa
from fakes import FakePage

MODULES = {"qa": qa, "com": com}
CHECKBOX = 'div.jenkins-form-item:has-text("{}") >> input[type="checkbox"]'


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_sd_file_flow(name, golden, capsys):
    module = MODULES[name]
    page = FakePage()

    run(module.configure_sd_file(page, "file://storage/sd.yaml", "06", 2, 5))

    golden(f"jenkins_form_sd_file__{name}.json", {"calls": page.calls, "output": capsys.readouterr().out})


@pytest.mark.parametrize("name", sorted(MODULES))
def test_login(name, golden, capsys):
    page = FakePage()

    run(MODULES[name].do_login(page))

    golden(f"jenkins_form_login__{name}.json", {"calls": page.calls, "output": capsys.readouterr().out})


@pytest.mark.parametrize("name", sorted(MODULES))
def test_checkbox_already_checked_is_left_alone(name, capsys):
    page = FakePage(checked={CHECKBOX.format("CLEANUP_FS"): True})

    run(MODULES[name].click_checkbox_by_label(page, "06", "CLEANUP_FS"))

    assert not [c for c in page.calls if c[1] in ("check", "click")]
    assert "✅ CLEANUP_FS clicked." in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(MODULES))
def test_checkbox_falls_back_to_clicking_the_label(name):
    page = FakePage(fail_check={CHECKBOX.format("START_APPS")})

    run(MODULES[name].click_checkbox_by_label(page, "06", "START_APPS"))

    methods = [(c[0], c[1]) for c in page.calls if c[1] in ("check", "click")]
    assert methods == [
        ('div.jenkins-form-item:has-text("START_APPS").first >> input[type="checkbox"].first', "check"),
        ('div.jenkins-form-item:has-text("START_APPS").first >> label.first', "click"),
    ]


@pytest.mark.parametrize("name", sorted(MODULES))
def test_checkbox_that_silently_fails_to_check_gets_retried_then_raises(name):
    """El bug real: check()/click() no avientan excepcion, pero el checkbox
    nunca queda marcado (overlay, elemento que se mueve, etc). Sin esto el
    script seguia como si nada -- justo lo que en headless nadie nota."""
    module = MODULES[name]
    page = FakePage(stays_unchecked={CHECKBOX.format("SD_FILE")})

    # Deadline chico para que la prueba no espere minutos reales.
    module._form.checkbox_deadline_ms = 50
    try:
        with pytest.raises(RuntimeError, match="No pude dejar marcado"):
            run(module.click_checkbox_by_label(page, "06", "SD_FILE"))
    finally:
        module._form.checkbox_deadline_ms = 180000

    # Se intento tanto el check() directo como el click() al label, mas de
    # una vez (reintento), nunca se dio por vencido sin agotar el deadline.
    checks = [c for c in page.calls if c[1] == "check"]
    clicks = [c for c in page.calls if c[1] == "click"]
    assert len(checks) > 1
    assert len(clicks) > 1


@pytest.mark.parametrize("name", sorted(MODULES))
def test_sd_file_that_never_fills_raises_instead_of_building(name):
    """Si SD_FILE se queda vacio, mejor que truene a que dispare el build
    con el campo en blanco."""
    module = MODULES[name]
    # get_editable_value_field busca en la pagina entera (no en el
    # contenedor), asi que la clave del selector va sin el prefijo del div.
    page = FakePage(stays_empty={'input[name="value"][type="text"]:not([disabled])'})

    with pytest.raises(RuntimeError, match="SD_FILE"):
        run(module.configure_sd_file(page, "file://storage/sd.yaml", "06", 1, 1))

    # Nunca debio llegar a apretar Build con el campo vacio.
    assert not [c for c in page.calls if c[0].startswith("role=button[Build]")]


@pytest.mark.parametrize("name", sorted(MODULES))
def test_build_uses_submit_when_there_is_no_build_button(name, capsys):
    page = FakePage(counts={"role=button[Build]": 0})

    run(MODULES[name].click_build(page, "06", 1, 1))

    clicks = [c[0] for c in page.calls if c[1] == "click"]
    assert clicks == ['button[type="submit"].first']
    assert "Build submitted" in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(MODULES))
def test_build_warns_when_jenkins_does_not_redirect(name, capsys):
    page = FakePage(url_after_submit="https://jenkins/job/x/build?delay=0sec")

    run(MODULES[name].click_build(page, "06", 1, 1))

    assert "No pude confirmar la redireccion" in capsys.readouterr().out
