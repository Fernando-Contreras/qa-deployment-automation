"""Deployment_Defect: del build fallido al Defect en Jira (sin tocar Jira aquí)."""

import argparse

import pytest

import Deployment_Defect as dd
from conftest import DAILY_ENVS_SAMPLES

JOB = "https://sdci.example.com/job/SD.PROJECT/job/nttcw06_qa/job/Deploy-Parallel"


def info_for(log_text, env_job="nttcw06_qa", build="163", stage="Check & Clean Up SHARED", version="T2.3.49"):
    """Lo mismo que arma analyze(), pero a partir de un log ya bajado."""
    block, _source = dd.extract_error_block(log_text)
    meta = dd.extract_meta(block)
    info = {
        "console_url": f"https://sdci.example.com/job/SD.PROJECT/job/{env_job}/job/Deploy-Parallel/{build}/console",
        "env_job": env_job,
        "pipeline": "Deploy-Parallel",
        "build": build,
        "env_tag": dd.env_tag(env_job),
        "region_tags": dd.region_tags(block, env_job),
        "block": block,
        "meta": meta,
        "stage": stage,
        "version": version,
        "label": env_job,
    }
    info["failed_item"] = dd.guess_failed_item(block, meta, stage)
    info["component"] = "DevOps" if dd.is_sql_problem(block) else "IT"
    info["signature"] = dd.error_signature(block)
    return info


# -------------------------
# DAILY_ENVS
# -------------------------
@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_failed_entries_match_previous_behaviour(sample, fixture_text, golden):
    entries = dd.parse_failed_entries(fixture_text(f"daily_envs/{sample}"))
    golden(f"defect_failed_entries__{sample.replace('.txt', '.json')}", entries)


def test_lines_that_already_have_a_ticket_are_recognised(fixture_text):
    entries = dd.parse_failed_entries(fixture_text("daily_envs/messy_real_world.txt"))
    by_label = {e["label"]: e for e in entries}

    assert by_label["06QA"]["jira_key"] == "QAPRJ-189455"
    assert by_label["06QA"]["stage"] == "Check & Clean Up SHARED"
    assert by_label["81QA"]["jira_key"] is None
    assert by_label["81QA"]["stage"] == "Collections WEST, Collections EAST"


@pytest.mark.parametrize("header", ["FAILED:", "❌ **failed**:", "## ❌ **FAILED**"])
def test_failed_section_is_found_whatever_way_the_header_is_written(header):
    """Regresión: con --no-organize un encabezado escrito a mano se ignoraba."""
    text = "\n".join([
        "## **T2.3.49**",
        "---",
        header,
        f"[**06QA**]({JOB}/) (T2.3.49) - Check & Clean Up SHARED",
    ])

    assert [e["label"] for e in dd.parse_failed_entries(text)] == ["06QA"]


def test_jira_link_is_appended_only_once(fixture_text):
    text = fixture_text("daily_envs/multi_block.txt")
    entries = dd.parse_failed_entries(text)

    once = dd.append_jira_links(text, [(entries[0], "QAPRJ-125975")])
    twice = dd.append_jira_links(once, [(entries[0], "QAPRJ-159569")])

    line = once.splitlines()[entries[0]["line_no"]]
    assert line.endswith("(46CI) - Check & Clean Up SHARED: https://jira.example.com/browse/QAPRJ-125975")
    assert twice == once


# -------------------------
# URLs DE JENKINS
# -------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"{JOB}/163/console", f"{JOB}/163"),
        (f"{JOB}/163/consoleFull", f"{JOB}/163"),
        (f"'{JOB}/'", JOB),
    ],
)
def test_strip_console(raw, expected):
    assert dd.strip_console(raw) == expected


def test_strip_console_rejects_things_that_are_not_links():
    with pytest.raises(ValueError):
        dd.strip_console("nttcw06_qa")


def test_build_url_helpers():
    assert dd.has_build_number(f"{JOB}/163") is True
    assert dd.has_build_number(f"{JOB}/lastFailedBuild") is True
    assert dd.has_build_number(f"{JOB}/") is False
    assert dd.to_console_full(f"{JOB}/163/console") == f"{JOB}/163/consoleFull"
    assert dd.parse_build_url(f"{JOB}/163/console") == {
        "server": "sdci",
        "env_job": "nttcw06_qa",
        "pipeline": "Deploy-Parallel",
        "build": "163",
    }


# -------------------------
# SACAR EL ERROR DEL LOG
# -------------------------
def test_installation_failed_block_goes_from_the_step_to_the_log_directory(fixture_text):
    block, source = dd.extract_error_block(fixture_text("console/installation_failed_sql.txt"))

    assert source == "INSTALLATION FAILED"
    assert block.splitlines()[0].endswith("step3 (3/7) Installing patch nrm_custom_2.3.49.zip on EAST")
    assert "Log directory:" in block.splitlines()[-1]


def test_ansible_fatal_block(fixture_text):
    block, source = dd.extract_error_block(fixture_text("console/ansible_fatal.txt"))

    assert source == "fatal: FAILED!"
    assert block.startswith("09:40:12  fatal: [nttcw81app2]: FAILED!")
    assert "PLAY RECAP" not in block


def test_log_without_known_pattern_falls_back_to_its_tail():
    log = "\n".join(f"linea {i}" for i in range(200))
    block, source = dd.extract_error_block(log)

    assert source.startswith("cola del log")
    assert block.splitlines()[0] == "linea 140"


def test_meta_extracted_from_the_block(fixture_text):
    block, _ = dd.extract_error_block(fixture_text("console/installation_failed_sql.txt"))

    assert dd.extract_meta(block) == {
        "patch": "nrm_custom_2.3.49.zip",
        "step": "3",
        "log_dir": "/u02/app/NTTEAST/autoinstaller/logs/2026.09.22_14-02-15",
        # El mensaje va del "Error message:" hasta "Total time:", así que
        # arrastra el INSTALLATION FAILED que viene en medio.
        "message": (
            "Exception in SQL process: ORA-00942: table or view does not exist "
            "at line 12 of 042_alter_billing_tables.sql INSTALLATION FAILED"
        ),
    }


def test_sql_errors_go_to_devops(fixture_text):
    info = info_for(fixture_text("console/installation_failed_sql.txt"))
    assert info["component"] == "DevOps"
    assert info["failed_item"] == "NRM Custom patches"


def test_region_and_env_tags(fixture_text):
    info = info_for(fixture_text("console/ansible_fatal.txt"), env_job="nttcw81_qa")

    assert info["env_tag"] == "[81QA]"
    assert info["region_tags"] == "[WEST]"
    assert dd.env_tag("nttcom2_qa") == "[2QA]"
    assert dd.region_tags("sin rutas", "nttcom2_qa") == "[COM]"


# -------------------------
# JUNTAR ENVS CON EL MISMO ERROR
# -------------------------
def test_same_error_on_two_envs_goes_in_one_ticket(fixture_text):
    log = fixture_text("console/installation_failed_sql.txt")
    other_env = log.replace("NTTEAST", "NTTWEST").replace("nttcw06app1", "nttcw27app1").replace("2026.09.22_14-02-15", "2026.09.22_15-44-01")

    a = info_for(log, env_job="nttcw06_qa")
    b = info_for(other_env, env_job="nttcw27_qa", build="88", stage="Custom NRM WEST", version="99CI")

    groups = dd.group_by_problem([("06", a), ("27", b)])

    assert len(groups) == 1
    assert dd.build_summary([a, b]) == "[06QA][27QA][EAST][WEST] | Deployment failed on NRM Custom patches"


def test_different_component_never_merges(fixture_text):
    sql = info_for(fixture_text("console/installation_failed_sql.txt"))
    timeout = info_for(fixture_text("console/ansible_fatal.txt"), env_job="nttcw81_qa")

    assert len(dd.group_by_problem([("06", sql), ("81", timeout)])) == 2


def test_ticket_text_for_a_merged_defect(fixture_text, golden):
    log = fixture_text("console/installation_failed_sql.txt")
    a = info_for(log, env_job="nttcw06_qa")
    b = info_for(log.replace("NTTEAST", "NTTWEST"), env_job="nttcw27_qa", build="88", stage="Custom NRM WEST")

    golden("defect_description_merged.txt", dd.build_description([a, b]))


def test_ticket_fields(fixture_text):
    info = info_for(fixture_text("console/installation_failed_sql.txt"))
    args = argparse.Namespace(priority="Critical")

    fields = dd.build_fields([info], args, assignees={"DevOps": "asamanta"})

    assert fields["summary"] == "[06QA][EAST] | Deployment failed on NRM Custom patches"
    assert fields["priority"] == {"name": "Critical"}
    assert fields["components"] == [{"id": dd.COMPONENT_IDS["DevOps"]}]
    assert fields["assignee"] == {"name": "asamanta"}
    assert fields[dd.FOUND_IN_FIELD] == {"id": dd.FOUND_IN_BUILD}
    assert fields["description"].startswith("Hi team, could you please check?")
