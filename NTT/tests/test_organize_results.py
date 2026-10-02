"""OrganizeResults: el que acomoda Daily_Envs.txt por estado y dispara QA_Envs / Webex."""

import pytest

import OrganizeResults as org
from conftest import DAILY_ENVS_SAMPLES


def block(text):
    """Quita la sangría de los bloques escritos dentro de las pruebas."""
    return "\n".join(line.strip() for line in text.strip().splitlines()) + "\n"


ENV_URL = "https://sdci.example.com/job/SD.PROJECT/job/nttcw{n}_qa/job/Deploy-Parallel/"


def env_line(n, version="T2.3.49", tail=""):
    return f"[**{n}QA**]({ENV_URL.format(n=n)}) ({version}){tail}"


# -------------------------
# ARCHIVO COMPLETO (golden master)
# -------------------------
@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_main_organizes_real_files_exactly_like_before(sample, daily_envs, golden, monkeypatch):
    path = daily_envs(sample)
    promoted = []
    monkeypatch.setattr(org, "run_qa_envs_trigger", promoted.extend)

    org.main(run_webex=False)

    golden(f"organize__{sample}", path.read_text(encoding="utf-8"))
    golden(f"organize_promoted__{sample.replace('.txt', '.json')}", promoted)
    assert path.with_suffix(".txt.bak").exists(), "Siempre deja respaldo antes de escribir"


@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_main_is_idempotent(sample, daily_envs, monkeypatch):
    path = daily_envs(sample)
    monkeypatch.setattr(org, "run_qa_envs_trigger", lambda lines: None)

    org.main(run_webex=False)
    once = path.read_text(encoding="utf-8")
    org.main(run_webex=False)

    assert path.read_text(encoding="utf-8") == once


def test_main_never_loses_an_env(daily_envs, monkeypatch):
    path = daily_envs("messy_real_world.txt")
    before = org.count_env_lines(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(org, "run_qa_envs_trigger", lambda lines: None)

    org.main(run_webex=False)

    assert org.count_env_lines(path.read_text(encoding="utf-8")) == before


def test_main_calls_webex_only_when_asked(daily_envs, monkeypatch):
    daily_envs("multi_block.txt")
    calls = []
    monkeypatch.setattr(org, "run_webex_update", lambda: calls.append("webex"))
    monkeypatch.setattr(org, "run_qa_envs_trigger", lambda lines: None)

    org.main(run_webex=False)
    assert calls == []

    org.main()
    assert calls == ["webex"]


def test_main_refuses_a_file_without_blocks(daily_envs):
    daily_envs(text="nada que ver aqui\n")
    with pytest.raises(RuntimeError, match="No se detecto ningun bloque"):
        org.main(run_webex=False)


# -------------------------
# UN BLOQUE
# -------------------------
def test_env_with_east_and_west_is_promoted_to_passed():
    text = block(f"""
        ## **T2.3.49 — 99CI**
        ---
        🔄 **IN PROGRESS**:
        {env_line('06', tail=' EAST: 101142587690 WEST: 201206358994')}
        {env_line('11')}
    """)

    rebuilt = org.parse_block(text)

    assert rebuilt.index("**PASSED**") < rebuilt.index("06QA") < rebuilt.index("**IN PROGRESS**") < rebuilt.index("11QA")


def test_failure_suffix_moves_env_to_failed():
    text = block(f"""
        **T2.3.49**
        ---
        🔄 **IN PROGRESS**:
        {env_line('06', tail=' - Check & Clean Up SHARED')}
    """)

    rebuilt = org.parse_block(text)

    assert "❌ **FAILED**:" in rebuilt
    assert "IN PROGRESS" not in rebuilt


def test_on_hold_and_blocked_are_sticky():
    """ON HOLD / BLOCKED los pones tú a mano: ni un '- motivo' ni EAST/WEST los mueve."""
    text = block(f"""
        **T2.3.49**
        ---
        🔒 **ON HOLD** - T3 copy In Progress:
        {env_line('06', tail=' - esperando copia')}
        ⚠️ **BLOCKED**:
        {env_line('11', tail=' EAST: 1 WEST: 2')}
    """)

    rebuilt = org.parse_block(text)

    assert "🔒 **ON HOLD** - T3 copy In Progress:" in rebuilt
    assert rebuilt.index("**ON HOLD**") < rebuilt.index("06QA") < rebuilt.index("**BLOCKED**") < rebuilt.index("11QA")
    assert "**FAILED**" not in rebuilt and "**PASSED**" not in rebuilt


def test_on_hold_without_reason_goes_back_to_in_progress_once_something_passed():
    text = block(f"""
        **T2.3.49**
        ---
        ✅ **PASSED**:
        {env_line('02', tail=' EAST: 1 WEST: 2')}
        🔒 **ON HOLD**:
        {env_line('43')}
        {env_line('44', tail=' - falta la copia T7')}
    """)
    promoted = []

    rebuilt = org.parse_block(text, promoted)

    assert [line.split("]")[0] for line in promoted] == ["[**43QA**"]
    assert rebuilt.index("**IN PROGRESS**") < rebuilt.index("43QA")
    assert rebuilt.index("**ON HOLD**") < rebuilt.index("44QA")


def test_group_note_on_on_hold_keeps_everyone_on_hold():
    text = block(f"""
        **T2.3.49**
        ---
        ✅ **PASSED**:
        {env_line('02', tail=' EAST: 1 WEST: 2')}
        🔒 **ON HOLD** till the T3 copy finishes:
        {env_line('43')}
    """)
    promoted = []

    org.parse_block(text, promoted)

    assert promoted == []


def test_envs_are_sorted_by_version_then_number_and_deduplicated():
    text = block(f"""
        **Mixto**
        ---
        🔄 **IN PROGRESS**:
        {env_line('80', 'T2.3.49')}
        {env_line('06', '99CI')}
        {env_line('11', 'T2.3.48')}
        {env_line('80', 'T2.3.49')}
    """)

    rebuilt = org.parse_block(text)
    order = [line.split("]")[0].strip("[*") for line in rebuilt.splitlines() if "QA**" in line]

    assert order == ["11QA", "80QA", "06QA"]


# -------------------------
# PIEZAS SUELTAS
# -------------------------
@pytest.mark.parametrize(
    "line, expected",
    [
        ("✅ **PASSED**:", ("PASSED", "")),
        ("🔒 **ON HOLD** - T3 & T7 copies In Progress:", ("ON HOLD", " - T3 & T7 copies In Progress")),
        ("## ❌ **FAILED**", ("FAILED", "")),
        ("FAILED:", ("FAILED", "")),
        ("blocked:", ("BLOCKED", "")),
        ("Deploy failed", None),
        ("[**06QA**](url) (T2.3.49)", None),
    ],
)
def test_match_status_header(line, expected):
    assert org.match_status_header(line) == expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("[**06QA**](u) (T2.3.49)", (0, 2, 3, 49, 0)),
        ("[**06QA**](u) (T2.3.49.1)", (0, 2, 3, 49, 1)),
        ("[**COM2QA**](u) (CI_23 SS)", (1, 23, 0, 0, 0)),
        ("[**32QA**](u) (46CI)", (1, 46, 0, 0, 0)),
        ("[**32QA**](u) (loquesea)", (99, 0, 0, 0, 0)),
    ],
)
def test_get_version_tuple(line, expected):
    assert org.get_version_tuple(line) == expected


def test_com_and_cw_envs_with_same_number_are_different_envs():
    assert org.env_identity("[**COM6QA**]") != org.env_identity("[**06QA**]")
    assert org.env_identity("[**6QA**]") == org.env_identity("[**06QA**]")


def test_find_duplicate_envs_reports_where_each_copy_lives():
    blocks = [
        block(f"**A**\n---\n✅ **PASSED**:\n{env_line('06')}"),
        block(f"**B**\n---\n🔄 **IN PROGRESS**:\n{env_line('6')}"),
    ]

    duplicates = org.find_duplicate_envs(blocks)

    assert list(duplicates) == [("CW", 6)]
    assert duplicates[("CW", 6)] == [("06QA", "PASSED", "A"), ("6QA", "IN PROGRESS", "B")]


def test_envs_dropped_in_the_footer_legend_are_rescued():
    footer = block(f"""
        file://storage/SD/release.yaml

        ✅ **PASSED**:
        {env_line('42')}
        🔄 **IN PROGRESS**:
    """)

    kept, recovered = org.rescue_envs_from_footer(footer)

    assert "42QA" not in kept
    assert recovered.startswith("✅ **PASSED**:\n[**42QA**]")
