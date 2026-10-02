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


def test_update_daily_envs_appends_or_replaces(tmp_path, monkeypatch):
    path = tmp_path / "Daily_Envs.txt"
    monkeypatch.setattr(le, "DAILY_ENVS_PATH", path)

    le.update_daily_envs("## **A**", "v")
    le.update_daily_envs("## **B**\n\n", "v")
    assert path.read_text(encoding="utf-8") == "## **A**\n\n## **B**\n"

    le.update_daily_envs("## **C**", "r")
    assert path.read_text(encoding="utf-8") == "## **C**\n"
