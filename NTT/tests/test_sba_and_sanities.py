"""SBA (Jenkins de pruebas SBA), Sanity_COM y E2E: la parte que no necesita navegador."""

import random

import pytest

import E2E_Flow1_Till_Billing as e2e
import SBA as sba
import Sanity_COM as sanity_com
from conftest import DAILY_ENVS_SAMPLES

REPORT = "http://192.0.2.3:9312/sheet-based?report=IF29_int_main_scenarios_v2_20260922.xlsx"
NULL_REPORT = "http://192.0.2.3:9312/sheet-based?report=null"


# -------------------------
# SBA
# -------------------------
@pytest.mark.parametrize(
    "log, expected",
    [
        (f"...\nexecution_url\n{REPORT}\n", (REPORT, False, False)),
        (f"execution_url\n{NULL_REPORT}\nFinished: FAILURE", (None, True, True)),
        ("todavia corriendo...", (None, False, False)),
        ("Finished: FAILURE", (None, True, True)),
        (f"sin marcador {REPORT}\nFinished: SUCCESS", (REPORT, False, True)),
    ],
)
def test_report_link_is_read_from_the_console(log, expected):
    assert sba.extract_report_from_log(log) == expected


def test_excel_option_matching():
    options = ["IF29_int_main_scenarios_v2.xlsx", "SBA_Scenario_combination_1_IF29_int_test_1.xlsx", "Other.xlsx"]

    assert sba.excel_label_to_option("IF29-int main scenarios_v2 (IF29 L3 checks)", options) == options[0]
    assert sba.excel_label_to_option("PrePT_SBA_Scenario_combination_1_IF29-int_test_1", options) == options[1]
    assert sba.excel_label_to_option("other", options) == "Other.xlsx"
    assert sba.excel_label_to_option("Algo totalmente distinto", options) is None


def test_random_env_list_is_reproducible_with_a_seed():
    random.seed(7)
    entries = sba.fill_env_list_randomly("IF29-int main scenarios_v2\n78qa West Other\n\nPrePT_A")

    assert [(e.env_num, e.tenant, e.excel_label) for e in entries][1] == (78, "West", "Other")
    assert len(entries) == 3
    assert all(e.env_num in sba.DEFAULT_ENV_POOL for e in (entries[0], entries[2]))


def test_parse_env_list_requires_env_and_tenant():
    entries = sba.parse_env_list("07qa East IF29\n101qa com Other")

    assert [(e.jenkins_env_id(), e.tenant) for e in entries] == [("nttcom07_qa", "East"), ("nttcw101_qa", "COM")]
    with pytest.raises(ValueError):
        sba.parse_env_list("solo excel sin env")


def test_build_result_text():
    assert sba.BuildResult(report_link=REPORT).log_line == REPORT
    assert sba.BuildResult(console_url="c", could_not_trigger=True).log_line == "Could not trigger → c"
    assert sba.BuildResult().log_line == "N/A"


# -------------------------
# Sanity_COM
# -------------------------
@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_sanity_com_reads_passed_com_envs(sample, daily_envs, golden):
    daily_envs(sample)
    golden(f"sanity_com_passed__{sample.replace('.txt', '.json')}", sanity_com.leer_envs_passed())


# -------------------------
# E2E
# -------------------------
def test_e2e_envs_come_from_argv_then_from_the_pending_file(tmp_path, monkeypatch):
    pending = tmp_path / "pending_sanity_envs.txt"
    monkeypatch.setattr(e2e, "PENDING_SANITY_ENVS_FILE", pending)

    monkeypatch.setattr(e2e.sys, "argv", ["E2E", "06,", "74", "--parallel=4"])
    assert e2e.get_envs_from_args() == "06,, 74"
    assert e2e.get_parallel_from_args() == 4

    monkeypatch.setattr(e2e.sys, "argv", ["E2E"])
    pending.write_text("06, 74", encoding="utf-8")
    assert e2e.get_envs_from_args() == "06, 74"
    assert not pending.exists(), "El archivo se consume una sola vez"
    assert e2e.get_envs_from_args() == ""


def test_e2e_helpers():
    assert e2e.normalize_envs("6, 74,101") == ["06", "74", "101"]
    assert e2e.extract_account_from_text('{"account_number": "201206358994"}') == "201206358994"
