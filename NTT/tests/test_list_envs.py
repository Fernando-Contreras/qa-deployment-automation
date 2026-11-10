"""List_Envs: del mail de "builds de hoy" al bloque de Daily_Envs.txt."""

import pytest

import List_Envs as le

MAILS = {
    "t_version_in_parentheses": """Hi team,
Please deploy today's build (T2.3.49) on:
Nttcw42_qa
Nttcw06_qa
Nttcw101_qa snapshot
Thanks""",
    "sd_file_location": """Hello,
SD File Location: file://storage/Releases/T2.3.50/SD/sd-release-p2-2024.2-adm-r20260909144230-T2.3.50.yaml
Envs: 32_qa, 67_qa, 115_qa""",
    "com_ci_builds": """Hi,
CI_23 build for NTTCOM2_QA and NTTCOM15_QA (yaml)
CI_23 snapshot for COM26 QA and COM28 QA""",
    "plain_text_version": """Deploy with T2.3.31 on 41qa, 43 qa and 44_qa please""",
    "ci_tag_group": """Latest 88 ADM CI SD for 74qa and 81qa
Snapshot of .46 below""",
}


@pytest.mark.parametrize("name", sorted(MAILS))
def test_mail_to_block_matches_previous_behaviour(name, golden):
    golden(f"list_envs__{name}.txt", le.build_webex_box(MAILS[name]) + "\n")


def test_quoted_reply_is_cut_off():
    body = "\n".join([
        "Today: Nttcw42_qa (T2.3.49)",
        "",
        "From: Someone",
        "Sent: Monday",
        "Yesterday: Nttcw06_qa (T2.3.48)",
    ])

    assert le.strip_quoted_reply(body) == "Today: Nttcw42_qa (T2.3.49)"


def test_webex_version_has_no_env_lists():
    full = le.build_blocks([("mail 1", MAILS["t_version_in_parentheses"]), ("mail 2", MAILS["com_ci_builds"])])
    webex = le.strip_env_lists(full)

    assert "Environment lists" in full
    assert "Environment lists" not in webex
    assert webex.count("## **") == 2


@pytest.mark.parametrize(
    "text, tag",
    [
        ("88 CI", "88CI"),
        ("46QA ADM CI", "46CI"),
        ("Latest 88 ADM CI SD", "88CI"),
        ("nada", None),
    ],
)
def test_find_ci_tag(text, tag):
    assert le.find_ci_tag(text) == tag


# --- Quien arranca primero -------------------------------------------------
MAIL_ONE_THEN_REST = """Hello Alex, Sam, Vivekanand.

Please update the below Env tonight, with the latest Snapshot – ADM R3/T2.3.55.

Bass page: https://wiki.example.com/display/QAPRJ/46_qa+Deployment+Changelog+-+ADM+CI

1.\tNttcw43_qa (ADM Testing R3)  Latest 46 ADM CI SD
Note: If above Env will upgrade successfully, then only upgrade the below Envs after Sanity passed in 46_CI.
2.\tNttcw44_qa (ADM Testing R3)  Latest 46 ADM CI SD
"""

MAIL_ONE_THEN_THREE = """Hello Alex, Sam, Vivekanand.

Please update the below Env tonight, with the latest SD – ADM R3/T2.3.54.

Bass page: https://wiki.example.com/pages/viewpage.action?spaceKey=QAPRJ&title=99CI+Deployments+Changelog+-+CRT2+R3

1.           Nttcw67_qa (ADM Testing R3)  Latest 99 ADM CI SD
Note: If above Env will upgrade successfully, then only upgrade the below Envs after Sanity passed in 99_CI.
2.           Nttcw42_ qa (ADM Testing R3) SD
3.           Nttcw101_ qa (ADM Testing R3) SD
4.           Nttcw94_ qa (ADM Testing R3) SD
"""

MAIL_COM_NO_FIRST = """Hi Sam/Alex,

Please update the below environments tonight (2026-04-11) with the specified CI_23 and CI_07 only if got the working snapshot for both CI’s

* nttcom42_qa via Yaml of CI23
* NTTCOM30_QA via Yaml of CI23
* NTTCOM26_QA via snapshot of CI23
"""

AFTER_ORGANIZE = """## **T2.3.54 — 99CI**
---
\U0001F512 **ON HOLD**:
[**42QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw42_qa/job/Deploy-Parallel/) (T2.3.54)
[**67QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw67_qa/job/Deploy-Parallel/) (T2.3.54)
[**94QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw94_qa/job/Deploy-Parallel/) (T2.3.54)

---
Environment lists:
- T2.3.54 → 42, 67, 94


---

## **T2.3.55 — 46CI**
---
\U0001F512 **ON HOLD**:
[**43QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw43_qa/job/Deploy-Parallel/) (T2.3.55)
[**44QA**](https://sdci.example.com/job/SD.PROJECT/job/nttcw44_qa/job/Deploy-Parallel/) (T2.3.55)

---
Environment lists:
- T2.3.55 → 43, 44
"""


def start_plan(text):
    return le.plan_start_envs(text, le.build_webex_box(text))


def plan_labels(plan):
    return sorted((label, version) for _, label, version in plan)


def test_only_the_env_before_the_note_starts():
    assert plan_labels(start_plan(MAIL_ONE_THEN_REST)) == [("43QA", "T2.3.55")]
    assert plan_labels(start_plan(MAIL_ONE_THEN_THREE)) == [("67QA", "T2.3.54")]


def test_mail_without_note_starts_every_env():
    labels = [label for label, _ in plan_labels(start_plan(MAIL_COM_NO_FIRST))]

    assert labels == ["COM26QA", "COM30QA", "COM42QA"]


def test_the_ci_group_number_never_counts_as_first_env():
    # "46_qa" aparece en la URL del bass page, antes de la nota, pero 46 es el
    # nombre del CI y no un env del bloque.
    text = le.build_webex_box(MAIL_ONE_THEN_REST)

    assert "46QA" not in text
    assert le.find_first_envs(MAIL_ONE_THEN_REST, {43, 44}) == {43}


def test_note_with_nothing_above_it_means_everyone_starts():
    mail = "Note: If above Env will upgrade successfully, then only upgrade the below Envs\nNttcw10_qa SD\nNttcw11_qa SD"

    assert le.find_first_envs(mail, {10, 11}) is None


def test_build_blocks_with_plan_keeps_the_same_text():
    mails = [("a", MAIL_ONE_THEN_REST), ("b", MAIL_COM_NO_FIRST)]
    text, plan = le.build_blocks_with_plan(mails)

    assert text == le.build_blocks(mails)
    assert {title for title, _, _ in plan} == {"T2.3.55 — 46CI", "CI_23 — Build"}


def test_promote_moves_only_the_first_env_to_in_progress(tmp_path):
    import OrganizeResults as org

    path = tmp_path / "Daily_Envs.txt"
    path.write_text(AFTER_ORGANIZE, encoding="utf-8")
    plan = start_plan(MAIL_ONE_THEN_THREE) + start_plan(MAIL_ONE_THEN_REST)

    assert le.promote_envs_to_in_progress(path, plan) == 2

    blocks, _, _ = org.split_blocks(path.read_text(encoding="utf-8"))
    final = [org.parse_block(block) for block in blocks]

    assert "IN PROGRESS**:\n[**67QA**]" in final[0]
    assert "ON HOLD**:\n[**42QA**]" in final[0] and "[**94QA**]" in final[0]
    assert "IN PROGRESS**:\n[**43QA**]" in final[1]
    assert "ON HOLD**:\n[**44QA**]" in final[1]
    assert final[1].count("[**4") == 2          # ninguno se duplico ni se perdio


def test_promote_leaves_the_file_alone_when_nothing_matches(tmp_path):
    path = tmp_path / "Daily_Envs.txt"
    path.write_text(AFTER_ORGANIZE, encoding="utf-8")

    assert le.promote_envs_to_in_progress(path, [("Otro bloque", "99QA", "T9.9.9")]) == 0
    assert path.read_text(encoding="utf-8") == AFTER_ORGANIZE


def test_start_first_envs_reorganizes_only_after_moving_something(tmp_path, monkeypatch):
    path = tmp_path / "Daily_Envs.txt"
    path.write_text(AFTER_ORGANIZE, encoding="utf-8")
    monkeypatch.setattr(le, "DAILY_ENVS_PATH", path)
    calls = []
    monkeypatch.setattr(le, "run_organize_results", lambda webex=True: calls.append(webex))

    le.start_first_envs(start_plan(MAIL_ONE_THEN_REST))
    # Se reordena el archivo, pero SIN Webex: el chat se queda en ON HOLD.
    assert calls == [False]

    le.start_first_envs([("Otro bloque", "99QA", "T9.9.9")])
    assert calls == [False]


def test_run_organize_results_only_skips_webex_when_asked(monkeypatch):
    commands = []

    class Done:
        returncode = 0

    monkeypatch.setattr(le.subprocess, "run", lambda command, **kw: commands.append(command) or Done())

    le.run_organize_results()
    le.run_organize_results(webex=False)

    assert "--no-webex" not in commands[0]
    assert commands[1][-1] == "--no-webex"


def test_update_daily_envs_appends_or_replaces(tmp_path, monkeypatch):
    path = tmp_path / "Daily_Envs.txt"
    monkeypatch.setattr(le, "DAILY_ENVS_PATH", path)

    le.update_daily_envs("## **A**", "v")
    le.update_daily_envs("## **B**\n\n", "v")
    assert path.read_text(encoding="utf-8") == "## **A**\n\n## **B**\n"

    le.update_daily_envs("## **C**", "r")
    assert path.read_text(encoding="utf-8") == "## **C**\n"
