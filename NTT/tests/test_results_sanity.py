"""Results_Sanity: lee las cuentas EAST/WEST de las pestañas de ATP y las pega en Daily_Envs."""

import asyncio

import pytest

import Results_Sanity as rs
from conftest import DAILY_ENVS_SAMPLES


@pytest.mark.parametrize(
    "text, account",
    [
        ('{"account_number": "101142587690", "x": 1}', "101142587690"),
        ("ACCOUNT_NUM\tSTATUS\n201206358994\tOK", "201206358994"),
        ("ACCOUNT_NUM\n(sin valor)", None),
        ("nada", None),
    ],
)
def test_extract_account_from_text(text, account):
    assert rs.extract_account_from_text(text) == account


def test_env_to_tag():
    assert rs.env_to_tag("nttcw6_qa") == ("06QA", "06")
    assert rs.env_to_tag("NTTCW101_QA") == ("101QA", "101")
    assert rs.env_to_tag("nttcom5_qa") is None


@pytest.mark.parametrize("sample", DAILY_ENVS_SAMPLES)
def test_passed_envs_with_accounts(sample, daily_envs, golden):
    daily_envs(sample)
    golden(f"results_passed__{sample.replace('.txt', '.json')}", rs.get_passed_envs_with_accounts())


def test_update_daily_envs_rewrites_east_and_west_in_place(daily_envs):
    path = daily_envs("multi_block.txt")

    tags = rs.update_daily_envs({"44": {"EAST": "101597160543", "WEST": "201105522757"}})
    tags_again = rs.update_daily_envs({"44": {"EAST": "101111283095", "WEST": "201105522757"}})

    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if "[**44QA**]" in l)
    assert tags == tags_again == {"44QA"}
    assert line.endswith("(T2.3.49) EAST: 101111283095 WEST: 201105522757")


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [type("Context", (), {"pages": pages})()]


class FakePlaywright:
    def __init__(self, pages):
        self.chromium = type("Chromium", (), {})()

        async def connect_over_cdp(url):
            return FakeBrowser(pages)

        self.chromium.connect_over_cdp = connect_over_cdp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_one_odd_tab_does_not_stop_the_rest(daily_envs, monkeypatch):
    """Regresión: una pestaña con un env que no es nttcw tronaba main() y las demás no se leían."""
    daily_envs("multi_block.txt")
    tabs = {
        "tab-com": ("nttcom5_qa", "101238551912"),
        "tab-44-east": ("nttcw44_qa", "101977692811"),
        "tab-44-west": ("nttcw44_qa", "201369949957"),
    }

    async def fake_extract(page):
        return tabs[page]

    written = {}
    monkeypatch.setattr(rs, "async_playwright", lambda: FakePlaywright(list(tabs)))
    monkeypatch.setattr(rs, "extract_env_and_account", fake_extract)
    monkeypatch.setattr(rs, "update_daily_envs", lambda results: written.update(results) or {"44QA"})

    asyncio.run(rs.main())

    assert written == {"44": {"EAST": "101977692811", "WEST": "201369949957"}}
