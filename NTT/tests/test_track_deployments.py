"""Track_Deployments: lee Daily_Envs, revisa Jenkins, rebuildea o marca PASSED/FAILED."""

from datetime import datetime, timedelta

import pytest

import Track_Deployments as td
from conftest import DAILY_ENVS_SAMPLES

SDCI = "https://sdci.example.com/job/SD.PROJECT/job"
SNAPCI = "https://snapci.example.com/job/SD.PROJECT/job"
CW06 = f"{SDCI}/nttcw06_qa/job/Deploy-Parallel/"
CW101 = f"{SDCI}/nttcw101_qa/job/Deploy-Parallel/"
COM2 = f"{SDCI}/nttcom2_qa/job/Deploy-Parallel/"
COM26_SS = f"{SNAPCI}/nttcom26_qa/job/Restore-Eatlon-SnapShot/"

NOW = datetime(2026, 9, 22, 18, 0)


def stages(*pairs):
    """wfapi/describe de mentira: stages(("Check & Clean Up", "SUCCESS"), ...)."""
    return {"stages": [{"name": name, "status": status} for name, status in pairs]}


# -------------------------
# LEER EL DAILY_ENVS
# -------------------------
@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_in_progress_deployments_match_previous_behaviour(sample, fixture_text, golden):
    deployments = td.extract_in_progress_deployments(fixture_text(f"daily_envs/{sample}"))
    golden(f"track_in_progress__{sample.replace('.txt', '.json')}", deployments)


def test_each_env_gets_the_sd_of_its_own_group(fixture_text):
    """Un bloque con varias rutas: cada env se va con la de SU lista, no con la última."""
    deployments = td.extract_in_progress_deployments(fixture_text("daily_envs/multi_block.txt"))
    by_label = {td.extract_env_label(d["url"]): d for d in deployments}

    com35 = by_label["COM35QA"]
    assert com35["patch"] == "nttcom23_ci_48590/nttcom23_ci_nitro_snapshot_ci_20260831_T5"
    assert com35["patch_source"] == "env_list"


def test_lines_that_already_have_a_failure_reason_are_not_checked_again():
    text = "\n".join([
        "## **T2.3.49 — 99CI**",
        "---",
        "🔄 **IN PROGRESS**:",
        f"[**06QA**]({CW06}) (T2.3.49) - Check & Clean Up SHARED",
        f"[**101QA**]({CW101}) (T2.3.49)",
    ])

    urls = [d["url"] for d in td.extract_in_progress_deployments(text)]

    assert urls == [CW101]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("file://storage/Releases/T2.3.49/SD/sd-release.yaml", "file://storage/Releases/T2.3.49/SD/sd-release.yaml"),
        ("(file://storage/sd.yaml).", "file://storage/sd.yaml"),
        ("Snapshot: nttcw46_qa_47982/snap_T4", "nttcw46_qa_47982/snap_T4"),
        ("nttcom23_ci_48590/nttcom23_ci_nitro_snapshot_ci_20260831_T5", "nttcom23_ci_48590/nttcom23_ci_nitro_snapshot_ci_20260831_T5"),
        (f"[**COM26QA**]({COM26_SS}) (CI_23 SS)", None),
        ("", None),
        ("- CI_23 SS → COM2, COM15", None),
    ],
)
def test_extract_patch_reference(line, expected):
    assert td.extract_patch_reference(line) == expected


def test_parse_env_group_payloads_maps_each_env_to_the_path_below_its_group():
    lines = [
        "- CI_23 SS → COM28, COM36",
        "nttcom23_ci_48981/nttcom23_snapshot_ci_20260904_T5",
        "- CI_23 → COM2, COM15",
        "file://storage/G3.1.1.yaml",
        "- 88CI → 74, 81",
    ]

    payloads = td.parse_env_group_payloads(lines)

    assert payloads == {
        ("COM", 28): "nttcom23_ci_48981/nttcom23_snapshot_ci_20260904_T5",
        ("COM", 36): "nttcom23_ci_48981/nttcom23_snapshot_ci_20260904_T5",
        ("COM", 2): "file://storage/G3.1.1.yaml",
        ("COM", 15): "file://storage/G3.1.1.yaml",
    }


@pytest.mark.parametrize(
    "url, label, number, is_com",
    [
        (CW06, "06QA", "06", False),
        (CW101, "101QA", "101", False),
        (COM2, "COM2QA", "2", True),
        (COM26_SS, "COM26QA", "26", True),
        ("https://example.com/otra/cosa", "UNKNOWN", None, False),
    ],
)
def test_env_naming(url, label, number, is_com):
    assert td.extract_env_label(url) == label
    assert td.extract_env_number(url) == number
    assert td.is_com_url(url) is is_com


def test_com_and_cw_with_same_number_are_different_keys():
    assert td.env_key_from_url(COM2) == ("COM", 2)
    assert td.env_key_from_url(f"{SDCI}/nttcw02_qa/job/Deploy-Parallel/") == ("CW", 2)


def test_all_com_envs_passed_reads_the_whole_file():
    passed = f"✅ **PASSED**:\n[**COM2QA**]({COM2}) (CI_23)\n"
    running = f"🔄 **IN PROGRESS**:\n[**COM26QA**]({COM26_SS}) (CI_23 SS)\n"

    assert td.all_com_envs_passed(passed) is True
    assert td.all_com_envs_passed(passed + running) is False
    assert td.all_com_envs_passed(f"✅ **PASSED**:\n[**06QA**]({CW06}) (T2.3.49)\n") is False


# -------------------------
# DECISIONES DEL REBUILD
# -------------------------
@pytest.mark.parametrize(
    "failed_stage, expected",
    [
        ("Check & Clean Up SHARED", {td.CLEANUP_FS}),
        ("Stop APPs EAST", {td.CLEANUP_FS, td.STOP_APPS_BEFORE}),
        ("Start APPs COM", set()),
        ("Custom NRM WEST", set()),
    ],
)
def test_rebuild_only_skips_what_failed(failed_stage, expected):
    build = td.stages_from_data(stages(("Check & Clean Up SHARED", "SUCCESS"), (failed_stage, "FAILED")))
    if failed_stage == "Check & Clean Up SHARED":
        build = td.stages_from_data(stages((failed_stage, "FAILED")))

    assert td.params_to_uncheck(build) == expected


def test_failed_signature_only_counts_broken_stages():
    build = td.stages_from_data(stages(("A", "SUCCESS"), ("Collections WEST", "FAILED"), ("B", "ABORTED")))
    assert td.failed_signature(build) == frozenset({"COLLECTIONS WEST", "B"})


@pytest.mark.parametrize(
    "build, expected",
    [
        ({"actions": [{"causes": [{"userName": "Alex Rivera"}]}]}, "Alex Rivera"),
        ({"actions": [{"causes": [{"userId": "your_user"}]}]}, "your_user"),
        ({"actions": [{}, {"causes": [{"userName": "Pat Novak"}]}]}, "Pat Novak"),
        ({"actions": []}, None),
        ({}, None),
    ],
)
def test_build_deployer_reads_the_jenkins_cause(build, expected):
    assert td.build_deployer(build) == expected


@pytest.mark.parametrize(
    "name, allowed",
    [
        ("Alex Rivera", True),
        ("alex rivera", True),
        ("  Sam Ortega  ", True),
        ("Pat Novak", False),
        (None, False),
        ("", False),
    ],
)
def test_is_chronic_deployer_only_alex_or_sam(name, allowed):
    assert td.is_chronic_deployer(name) is allowed


class FakeApiPage:
    """Contesta fetch_json_from_browser con lo que se le pida, por URL exacta."""

    def __init__(self, responses):
        self.responses = responses

    async def evaluate(self, script, url):
        import json

        return json.dumps(self.responses[url])


def make_build(number, hours_ago, deployer, result="FAILURE"):
    # Real "ahora", no NOW (fijo en el pasado): is_today() lo compara contra
    # datetime.now() de verdad y aqui no vale la pena mockear el reloj.
    started = datetime.now() - timedelta(hours=hours_ago)
    causes = [{"causes": [{"userName": deployer}]}] if deployer else []
    return {
        "number": number,
        "timestamp": started.timestamp() * 1000,
        "result": result,
        "actions": causes,
    }


def test_count_todays_failures_ignores_builds_fired_by_someone_else():
    job_url = f"{CW06}"
    builds_url = job_url.rstrip("/") + (
        "/api/json?tree=builds[number,timestamp,result,actions[causes[userId,userName]]]{0,25}"
    )
    page = FakeApiPage({
        builds_url: {
            "builds": [
                make_build(3, hours_ago=1, deployer="Alex Rivera"),
                make_build(2, hours_ago=2, deployer="Pat Novak"),
                make_build(1, hours_ago=3, deployer="Sam Ortega"),
            ]
        }
    })

    import asyncio

    count = asyncio.run(td.count_todays_failures(page, job_url))

    # Pat Novak fallo tambien, pero no cuenta: no es Alex ni Sam.
    assert count == 2


def test_count_todays_failures_stays_below_threshold_when_others_fired_the_rest():
    """El escenario que pidio Alex: 3 fallos hoy, pero 2 no son de el ni de Sam
    -> no llega a CHRONIC_FAILURE_THRESHOLD, asi que se reintenta el rebuild."""
    job_url = f"{CW06}"
    builds_url = job_url.rstrip("/") + (
        "/api/json?tree=builds[number,timestamp,result,actions[causes[userId,userName]]]{0,25}"
    )
    page = FakeApiPage({
        builds_url: {
            "builds": [
                make_build(3, hours_ago=1, deployer="Pat Novak"),
                make_build(2, hours_ago=2, deployer="Pat Novak"),
                make_build(1, hours_ago=3, deployer="Alex Rivera"),
            ]
        }
    })

    import asyncio

    count = asyncio.run(td.count_todays_failures(page, job_url))

    assert count == 1
    assert count < td.CHRONIC_FAILURE_THRESHOLD


@pytest.mark.parametrize(
    "patches, unlimited",
    [
        (["Check & Clean Up SHARED"], True),
        (["check & clean up shared "], True),
        (["Check & Clean Up SHARED", "Start APPs COM"], False),
        ([], False),
    ],
)
def test_cleanup_shared_is_rebuilt_without_limit(patches, unlimited):
    assert td.is_unlimited_rebuild({"failed_patches": patches}) is unlimited


@pytest.mark.parametrize(
    "api, status",
    [
        ({"building": True, "result": None}, "IN_PROGRESS"),
        ({"result": "SUCCESS"}, "PASSED"),
        ({"result": "FAILURE"}, "FAILED"),
        ({"result": "ABORTED"}, "ABORTED"),
        ({"result": None}, "UNKNOWN"),
    ],
)
def test_status_from_api(api, status):
    assert td.status_from_api(api) == status


def test_current_stage_and_how_long_it_has_been_running():
    started = int((NOW - timedelta(minutes=45)).timestamp() * 1000)
    data = {"stages": [
        {"name": "Check & Clean Up", "status": "SUCCESS"},
        {"name": "Stop APPs EAST", "status": "IN_PROGRESS", "startTimeMillis": started},
    ]}

    name, minutes = td.current_stage_info_from_stages(data, now=NOW)

    assert name == "Stop APPs EAST"
    assert round(minutes) == 45
    assert minutes > td.stuck_threshold_minutes(name)  # Stop/Start se atoran a los 30 min


def test_failed_patches_and_reason_text():
    data = stages(("Check & Clean Up", "SUCCESS"), ("Collections WEST", "FAILED"), ("Collections EAST", "FAILURE"))
    patches = td.failed_patches_from_stages(data)

    assert patches == ["Collections WEST", "Collections EAST"]
    assert td.build_failed_reason({"failed_patches": patches}) == "Collections WEST, Collections EAST"
    assert td.build_failed_reason({"status": "ABORTED"}) == "ABORTED"
    assert td.build_failed_reason({"tenant_issue": True}) == td.TENANT_ISSUE_REASON
    assert td.build_console_link({"url": CW06, "build": 163}) == CW06.rstrip("/") + "/163/console"


# -------------------------
# FECHAS
# -------------------------
@pytest.mark.parametrize(
    "delta, text",
    [
        (timedelta(seconds=20), "hace segundos"),
        (timedelta(minutes=5), "hace 5 min"),
        (timedelta(hours=2), "hace 2 h"),
        (timedelta(hours=2, minutes=15), "hace 2 h 15 min"),
        (timedelta(days=1, hours=1), "hace 1 día"),
        (timedelta(days=3), "hace 3 días"),
        (timedelta(minutes=-5), "en el futuro"),
    ],
)
def test_format_age(delta, text):
    assert td.format_age(NOW - delta, now=NOW) == text


def test_jenkins_date_formats():
    assert td.parse_jenkins_date("Aug 24, 2026, 2:59 PM") == datetime(2026, 8, 24, 14, 59)
    assert td.parse_jenkins_date("basura") is None
    assert td.format_datetime(datetime(2026, 8, 24, 14, 59)) == "Aug 24, 2026 2:59 PM"
    assert td.format_duration(94 * 60000) == "1 hr 34 min"
    assert td.format_duration(30000) == "<1 min"


# -------------------------
# ESCRIBIR EL DAILY_ENVS
# -------------------------
@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_mark_passed_moves_the_env_under_passed(sample, daily_envs, golden):
    path = daily_envs(sample)
    deployments = td.extract_in_progress_deployments(path.read_text(encoding="utf-8"))
    passed = [d["url"] for d in deployments][:2]

    td.mark_passed_envs_in_file(passed)

    golden(f"track_mark_passed__{sample}", path.read_text(encoding="utf-8"))


def test_failure_reason_is_written_once(daily_envs):
    path = daily_envs("multi_block.txt")
    url = f"{SDCI}/nttcw44_qa/job/Deploy-Parallel/"
    failed = [{"url": url, "failed_patches": ["Custom NRM WEST"]}]

    assert td.append_failed_envs_to_file(failed) == 1
    assert td.append_failed_envs_to_file(failed) == 0

    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if url in l)
    assert line.endswith("(T2.3.49) - Custom NRM WEST")


def test_empty_status_headers_are_dropped():
    body = [
        "✅ **PASSED**:",
        f"[**06QA**]({CW06}) (T2.3.49)",
        "",
        "🔄 **IN PROGRESS**:",
        "",
        "---",
        "Environment lists:",
    ]

    assert td.drop_empty_status_sections(body) == [
        "✅ **PASSED**:",
        f"[**06QA**]({CW06}) (T2.3.49)",
        "",
        "---",
        "Environment lists:",
    ]
