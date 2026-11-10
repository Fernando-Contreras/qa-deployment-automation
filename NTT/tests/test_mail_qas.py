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


ON_HOLD_WITH_NOTE = f"""## **99CI — 5OctBuild**
---
🔄 **IN PROGRESS**:
[**74QA**]({SDCI}/nttcw74_qa/job/Deploy-Parallel/) (99CI)

🔒 **ON HOLD** till QA team finishes:
[**80QA**]({SDCI}/nttcw80_qa/job/Deploy-Parallel/) (99CI)
"""


def test_on_hold_header_with_text_after_it_still_counts(daily_envs, monkeypatch):
    """'**ON HOLD** till QA team finishes:' no traia ':' pegado al '**', asi que
    se ignoraba y 80QA heredaba 'In Progress' del encabezado de arriba."""
    path = daily_envs(text=ON_HOLD_WITH_NOTE)
    monkeypatch.delenv(mfb.BATCH_ENV_VAR, raising=False)

    rows = mfb.parse_daily_env_rows(path)

    assert [(env, status) for _, env, _, status in rows] == [
        ("74QA", "In Progress"), ("80QA", "On Hold"),
    ]
    assert mfb.parse_daily_env_statuses(path) == {"74QA": "In Progress", "80QA": "On Hold"}
    # Un env On Hold no se revisa en Jenkins: solo los In Progress.
    assert mfb.get_checkable_envs(ON_HOLD_WITH_NOTE.splitlines()) == {"74QA"}


@pytest.mark.parametrize(
    "line, status",
    [
        ("🔒 **ON HOLD**:", "On Hold"),
        ("🔒 **ON HOLD** till both snapshots are created:", "On Hold"),
        ("🔄 **IN PROGRESS** - T3 copy:", "In Progress"),
        ("❌ **FAILED**:", "Failed"),
        ("[**74QA**](https://x/job/nttcw74_qa/job/Deploy-Parallel/) (99CI)", None),
        ("Environment lists:", None),
    ],
)
def test_status_header_detection(line, status):
    assert mfb.match_status_header(line) == status


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


class _Cell:
    def __init__(self, value=None):
        self.Value = value

    @property
    def Text(self):
        return "" if self.Value is None else str(self.Value)


class _Sheet:
    """Mini hoja de Excel: celdas por (fila, columna), crea las que le pidan."""

    def __init__(self, headers, env_text):
        self.cells = {}
        for col, header in headers.items():
            self.cells[(4, col)] = _Cell(header)
        self.cells[(5, 7)] = _Cell(env_text)
        self.UsedRange = type("U", (), {
            "Columns": type("C", (), {"Count": max(headers)})(),
            "Rows": type("R", (), {"Count": 5})(),
        })()

    def Cells(self, row, col):
        return self.cells.setdefault((row, col), _Cell())

    def status_of(self, col):
        return self.cells[(5, col)].Value


HEADERS = {8: "Init", 9: "Get Versions from SD", 10: "Custom CM UI", 11: "Custom AUTH",
           12: "Sanity", 13: "Overall Status"}


def _run_patch_update(monkeypatch, tmp_path, passed, other=()):
    log = tmp_path / "patch_status_log.txt"
    log.write_text("\n".join(mfb.format_result("43QA", "https://x", list(passed), [], list(other))),
                   encoding="utf-8")
    sheet = _Sheet(HEADERS, "43QA (46CI)")
    book = type("B", (), {"Worksheets": lambda self, name: sheet, "Save": lambda self: None})()
    excel = type("E", (), {"ScreenUpdating": True, "DisplayAlerts": True})()
    monkeypatch.setattr(mfb, "get_excel_app", lambda: excel)
    monkeypatch.setattr(mfb, "find_workbook", lambda app, path: (book, False))
    monkeypatch.setattr(mfb, "get_status_formats", lambda ws: {})
    monkeypatch.setattr(mfb, "update_excel_env_rows", lambda ws: 5)
    monkeypatch.setattr(mfb, "parse_daily_env_statuses", lambda path: {"43QA": "In Progress"})
    mfb.update_excel_with_patch_results(log)
    return sheet


def test_in_progress_env_without_stage_info_is_never_left_passed(monkeypatch, tmp_path):
    """Jenkins no regreso ningun stage para 43QA (build recien arrancado): nada
    puede quedar en Passed, y Sanity espera en On Hold."""
    sheet = _run_patch_update(monkeypatch, tmp_path, passed=[])

    assert [sheet.status_of(c) for c in range(8, 12)] == ["In Progress"] * 4
    assert sheet.status_of(12) == "On Hold"
    assert sheet.status_of(13) == "In Progress"


def test_in_progress_starts_right_after_the_last_confirmed_passed(monkeypatch, tmp_path):
    sheet = _run_patch_update(monkeypatch, tmp_path, passed=["Init", "Get Versions from SD"])

    assert [sheet.status_of(c) for c in range(8, 12)] == [
        "Passed", "Passed", "In Progress", "In Progress",
    ]
    assert sheet.status_of(12) == "On Hold"
    assert sheet.status_of(13) == "In Progress"


def test_running_stage_pushes_in_progress_to_everything_on_its_right(monkeypatch, tmp_path):
    sheet = _run_patch_update(
        monkeypatch, tmp_path,
        passed=["Init", "Get Versions from SD"], other=[("Custom CM UI", "IN_PROGRESS")],
    )

    assert [sheet.status_of(c) for c in range(8, 12)] == [
        "Passed", "Passed", "In Progress", "In Progress",
    ]
    assert sheet.status_of(12) == "On Hold"


def test_env_rows_never_write_into_a_patch_column(monkeypatch, daily_envs):
    """La columna 24 fue de 'status'; hoy es 'Custom CM UI' y no se debe tocar."""
    monkeypatch.setattr(mfb, "DAILY_ENVS_PATH", daily_envs(text=ON_HOLD_WITH_NOTE))
    monkeypatch.delenv(mfb.BATCH_ENV_VAR, raising=False)
    sheet = _Sheet({8: "Init"}, "")
    sheet.cells.pop((5, 7))

    mfb.update_excel_env_rows(sheet)

    assert {col for (_, col) in sheet.cells if col != 8} <= {7}


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
