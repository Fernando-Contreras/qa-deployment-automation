"""MailQAs: de Daily_Envs + Jenkins al Excel del Quality Gate y al correo.

Antes corría todo en cuanto se importaba, así que no se podía probar. Ahora
la lógica vive en main() y aquí se prueba lo que no necesita Excel/Outlook.
"""

import pytest

import MailQAs as mfb
from conftest import DAILY_ENVS_SAMPLES

SDCI = "https://sdci.example.com/job/SD.PROJECT/job"


@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_rows_for_the_excel(sample, daily_envs, golden, monkeypatch):
    path = daily_envs(sample)
    monkeypatch.delenv(mfb.BATCH_ENV_VAR, raising=False)

    golden(f"mail_rows__{sample.replace('.txt', '.json')}", mfb.parse_daily_env_rows(path))


def test_batches_follow_the_block_headers(fixture_text):
    batches = mfb.split_daily_env_batches(fixture_text("daily_envs/messy_real_world.txt"))

    assert [b.splitlines()[0] for b in batches] == [
        "**46CI — 25AugBuild**",
        "**CI_23 — Build & CI_07 — Build**",
        "**T2.3.49 — 99CI**",
        "**46CI — 25AugBuild**",
    ]


def test_only_the_chosen_batch_is_read(daily_envs, monkeypatch):
    path = daily_envs("messy_real_world.txt")
    monkeypatch.setenv(mfb.BATCH_ENV_VAR, "3")

    rows = mfb.parse_daily_env_rows(path)

    assert [(env, status) for _, env, _, status in rows] == [
        ("02QA", "Passed"), ("06QA", "Failed"), ("11QA", "In Progress"), ("80QA", "Passed"),
    ]


def test_mailed_batch_is_removed_from_the_file(daily_envs):
    path = daily_envs("messy_real_world.txt")

    mfb.remove_batch_from_daily_envs(path, 2)

    text = path.read_text(encoding="utf-8")
    assert "CI_23 — Build" not in text
    assert text.count("46CI — 25AugBuild") == 2


def test_failed_line_details_with_and_without_ticket():
    with_ticket = (
        f"[**06QA**]({SDCI}/nttcw06_qa/job/Deploy-Parallel/) (T2.3.49) - "
        "Check & Clean Up SHARED: https://jira.example.com/browse/QAPRJ-189455"
    )
    without = f"[**81QA**]({SDCI}/nttcw81_qa/job/Deploy-Parallel/) (46CI) - Collections WEST"

    assert mfb.extract_failed_details(with_ticket) == {
        "env": "06QA",
        "version": "T2.3.49",
        "patch": "Check & Clean Up SHARED",
        "ticket": "QAPRJ-189455",
        "ticket_url": "https://jira.example.com/browse/QAPRJ-189455",
    }
    assert mfb.extract_failed_details(without)["ticket"] is None


def test_env_details_pick_up_the_accounts():
    line = f"[**42QA**]({SDCI}/nttcw42_qa/job/Deploy-Parallel/) (T2.3.49) EAST: 101869073534 WEST: 201634941596"

    details = mfb.extract_env_details(line)

    assert (details["num"], details["east"], details["west"]) == (42, "101869073534", "201634941596")


@pytest.mark.parametrize(
    "raw, key",
    [("32QA", "32QA"), ("6QA", "06QA"), ("COM12QA", "COM12QA"), ("com2qa", "COM02QA")],
)
def test_env_keys_match_between_file_and_urls(raw, key):
    assert mfb.normalize_env_key(raw) == key


def test_env_label_from_url():
    assert mfb.extract_env_label(f"{SDCI}/nttcw06_qa/job/Deploy-Parallel/") == "06QA"
    assert mfb.extract_env_label(f"{SDCI}/nttcom2_qa/job/Deploy-Parallel/") == "COM02QA"


def test_stage_statuses_while_the_deployment_is_still_running():
    stages = [
        {"name": "Check & Clean Up", "status": "SUCCESS"},
        {"name": "Custom NRM EAST", "status": "IN_PROGRESS"},
        {"name": "Custom NRM WEST", "status": "NOT_EXECUTED"},
        {"name": "Branch WEST", "status": "ABORTED"},
    ]

    passed, failed, other = mfb.split_stage_statuses(stages, deployment_in_progress=True)

    assert passed == ["Check & Clean Up"]
    assert failed == []
    assert other == [("Custom NRM EAST", "IN_PROGRESS"), ("Custom NRM WEST", "IN_PROGRESS"), ("Branch WEST", "ABORTED")]


def test_patch_log_round_trip(tmp_path):
    lines = mfb.format_result(
        "06QA", "https://x", ["Check & Clean Up EAST"], ["Custom NRM WEST"], [("Start APPs", "NOT_EXECUTED")]
    )
    log = tmp_path / "patch_status_log.txt"
    log.write_text("\n".join(lines), encoding="utf-8")

    assert mfb.parse_patch_status_log(log) == {
        "06QA": {"Check & Clean Up": ["Passed"], "Custom NRM": ["Failed"], "Start APPs": ["Passed"]},
    }


def test_column_matching_tolerates_small_differences():
    keys = ["CHECKCLEANUP", "CUSTOMNRM", "COLLECTIONS"]

    assert mfb.find_matching_column_key("CUSTOMNRM", keys) == "CUSTOMNRM"
    assert mfb.find_matching_column_key("COLLECTION", keys) == "COLLECTIONS"
    assert mfb.find_matching_column_key("ALGOMAS", keys) is None
    assert mfb.combine_statuses(["Passed", "Failed", "In Progress"]) == "Failed"


def test_patch_references_skip_jenkins_links():
    lines = [
        "[**COM26QA**](https://snapci.example.com/job/SD.PROJECT/job/nttcom26_qa/job/Restore-Eatlon-SnapShot/) (CI_23 SS)",
        "nttcom23_ci_49469/nttcom23_ci_nitro_snapshot_ci_20260909_T5",
        "file://storage/G3.1.1.yaml",
        "file://storage/G3.1.1.yaml",
    ]

    assert mfb.collect_patch_references(lines) == [
        "nttcom23_ci_49469/nttcom23_ci_nitro_snapshot_ci_20260909_T5",
        "file://storage/G3.1.1.yaml",
    ]
