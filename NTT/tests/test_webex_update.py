"""Webex_Update: arma el mensaje de cada bloque y lo empareja con lo que ya se mandó hoy."""

import pytest

import Webex_Update as wx
from conftest import DAILY_ENVS_SAMPLES


def message(markdown, created="2026-09-22T15:00:00.000Z", msg_id="m1"):
    return {"id": msg_id, "markdown": markdown, "created": created}


@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_blocks_become_the_same_webex_messages_as_before(sample, daily_envs, golden):
    path = daily_envs(sample)
    blocks = wx.parse_blocks(str(path))

    golden(
        f"webex_blocks__{sample.replace('.txt', '.json')}",
        [{"header": b["header"], "envs": sorted(b["envs"]), "webex_md": b["webex_md"]} for b in blocks],
    )


def test_env_lists_and_sd_paths_never_reach_webex(daily_envs):
    blocks = wx.parse_blocks(str(daily_envs("multi_block.txt")))

    for block in blocks:
        assert "Environment lists" not in block["webex_md"]
        assert "file://" not in block["webex_md"]
        assert block["webex_md"].startswith("## ") and "\n---\n" in block["webex_md"]


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("  Bearer abc.def  ", "abc.def"),
        ('"abc\ndef"', "abcdef"),
        ("", ""),
    ],
)
def test_token_cleanup(raw, clean):
    assert wx._clean(raw) == clean


def test_markdown_is_ignored_when_comparing():
    assert wx.strip_md("[**06QA**](https://x/y) (T2.3.49)") == "06QA (T2.3.49)"
    assert wx.normalize("## **Title**\n---\n\n✅ **PASSED**:") == ["Title", "✅ PASSED:"]
    assert wx.env_set("[**06QA**](u) [**COM12QA**](u) nttcw101_qa 101QA") == frozenset({6, 12, 101})


def test_pairing_prefers_env_sets_then_headers_then_order():
    blocks = [
        wx.build_block(["## **A**", "---", "✅ **PASSED**:", "[**06QA**](u) (T2.3.49)"]),
        wx.build_block(["## **B**", "---", "🔄 **IN PROGRESS**:", "[**11QA**](u) (T2.3.49)"]),
        wx.build_block(["## **C**", "---", "🔄 **IN PROGRESS**:", "[**80QA**](u) (T2.3.49)"]),
    ]
    messages = [
        message("## **Viejo**\n---\n🔄 **IN PROGRESS**:\n[**06QA**](u) (T2.3.49)", msg_id="by-envs"),
        message("## **B**\n---\n🔄 **IN PROGRESS**:\n[**12QA**](u) (T2.3.49)", msg_id="by-header"),
    ]

    pairs, loose_blocks, loose_messages = wx.match_blocks_to_messages(blocks, messages)

    assert [(b["header"], m["id"], how) for b, m, how in pairs] == [
        ("A", "by-envs", "envs"),
        ("B", "by-header", "encabezado"),
    ]
    assert [b["header"] for b in loose_blocks] == ["C"]
    assert loose_messages == []


def test_pairing_by_order_only_when_both_sides_have_the_same_count():
    blocks = [wx.build_block(["## **Nuevo**", "---", "[**06QA**](u) (T2.3.49)"])]
    messages = [message("## **Otro**\n---\n(sin envs)")]

    pairs, _, _ = wx.match_blocks_to_messages(blocks, messages)

    assert pairs[0][2] == "orden (menos seguro)"
