"""
SBA automation: fill env/tenant lists and trigger NTT_SBA_Testing_Tool Jenkins builds.
"""

from __future__ import annotations

import asyncio
import difflib
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

import ntt_config
from jenkins_web import new_context_with_session, persist_session, saved_session
from nc_credentials import get_credentials

JENKINS_URL = (
    "https://taci.example.com/job/SD.NTT/job/TA/job/"
    "NTT_SBA_Testing_Tool/build?delay=0sec"
)
# Otro servidor (taci): su sesión va en su propio archivo, para no pisar la
# de sdci/snapci que comparten los demás scripts.
SBA_SESSION_NAME = "jenkins_sba"
# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
JENKINS_USER, JENKINS_PASSWORD = get_credentials(
    "jenkins", default_username=ntt_config.JENKINS_DEFAULT_USER
)

JOB_BASE = JENKINS_URL.replace("/build?delay=0sec", "")
BUILD_NUMBER_RE = re.compile(r"NTT_SBA_Testing_Tool/(\d+)(?:/|$)")
BUILD_HASH_RE = re.compile(r"#(\d+)")

REPORT_LINK_RE = re.compile(
    r"http://192\.0\.2\.6:9312/sheet-based\?report=[^\s'\"<>&]+",
    re.IGNORECASE,
)
NULL_REPORT_MARKER = "report=null"
BUILD_FAILURE_RE = re.compile(r"Finished:\s*FAILURE\b", re.IGNORECASE)
BUILD_FINISHED_RE = re.compile(
    r"Finished:\s*(SUCCESS|FAILURE|ABORTED|UNSTABLE)\b",
    re.IGNORECASE,
)

# 🔥 NEW: marker that always appears in the console log, right before
# the report link (either report=null on failure, or the real .xlsx link).
EXECUTION_URL_RE = re.compile(r"execution_url", re.IGNORECASE)
EXECUTION_URL_WINDOW = 1000  # chars to scan after the marker for the report link

LINE_RE = re.compile(
    r"^(?:(\d+)\s*qa\s+(East|West|COM|EAST|WEST)\s+)?(.+)$",
    re.IGNORECASE,
)

EXCEL_MATCH_MIN_SCORE = 0.82
DEFAULT_ENV_POOL = [10, 7, 73, 78, 101]
DEFAULT_TENANTS = ("East", "West")
QG_SCOPE_ANALYSIS_PATH = ntt_config.SBA_QG_SCOPE_ANALYSIS_PATH
QG_SCOPE_SHEET = "SBA"
EXCEL_COL_ENV_INFO = 3
EXCEL_COL_TEST_INFO = 4
EXCEL_COL_RESULT_LINK = 9
EXCEL_COL_IN_PROGRESS = 11
EXCEL_COL_COULD_NOT_TRIGGER = 12
XL_UP = -4162

_EXCEL_OVERRIDE_LABELS = {
    "IF29-int main scenarios_v2 (IF29 L3 checks)": "IF29_int_main_scenarios_v2.xlsx",
    "IF29-int main scenarios_v2": "IF29_int_main_scenarios_v2.xlsx",
    "PrePT_SBA_Scenario_combination_1_IF29-int_test_2": (
        "SBA_Scenario_combination_1_IF29_int_test_2.xlsx"
    ),
    "PrePT_SBA_Scenario_combination_1_IF29-int_test_1": (
        "SBA_Scenario_combination_1_IF29_int_test_1.xlsx"
    ),
}


class ExcelNotFoundError(Exception):
    def __init__(self, label: str, expected: str, options: list[str]):
        self.label = label
        self.expected = expected
        self.options = options
        super().__init__(label)


def normalize_for_excel_match(name: str) -> str:
    name = name.strip().lower()
    if name.endswith(".xlsx"):
        name = name[:-5]
    name = re.sub(r"\s+", "_", name)
    name = name.replace("-", "_")
    name = re.sub(r"[()]", "", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


EXCEL_OVERRIDES = {
    normalize_for_excel_match(label): target
    for label, target in _EXCEL_OVERRIDE_LABELS.items()
}


def expected_excel_filename(label: str) -> str:
    label = label.strip()
    if label.lower().endswith(".xlsx"):
        return label
    return f"{label}.xlsx"


def excel_match_score(label: str, option: str) -> float:
    left = normalize_for_excel_match(label)
    right = normalize_for_excel_match(option)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def excel_label_to_option(label: str, options: list[str]) -> str | None:
    """Pick the closest excel option above the similarity threshold."""
    label = label.strip()
    if not label:
        return None

    override = EXCEL_OVERRIDES.get(normalize_for_excel_match(label))
    if override and override in options:
        return override

    expected = expected_excel_filename(label)
    if expected in options:
        return expected

    if label in options:
        return label

    expected_lower = expected.lower()
    label_lower = label.lower()
    for option in options:
        option_lower = option.lower()
        if option_lower in {expected_lower, label_lower}:
            return option

    best_option: str | None = None
    best_score = 0.0
    for option in options:
        score = excel_match_score(label, option)
        if score > best_score:
            best_score = score
            best_option = option

    if best_option and best_score >= EXCEL_MATCH_MIN_SCORE:
        return best_option

    return None


def find_missing_excel_labels(entries: list[SBAEntry], options: list[str]) -> list[tuple[SBAEntry, str]]:
    missing: list[tuple[SBAEntry, str]] = []
    for entry in entries:
        if not excel_label_to_option(entry.excel_label, options):
            missing.append((entry, expected_excel_filename(entry.excel_label)))
    return missing


def print_excel_not_found_error(missing: list[tuple[SBAEntry, str]], options: list[str]) -> None:
    print("\n" + "=" * 60)
    print("ERROR: Excel file not found in Jenkins (no close match)")
    print("=" * 60)
    for entry, expected in missing:
        print(f"  Line: {entry.log_label}")
        print(f"  Expected in dropdown: {expected}")
        print()
    print("Available excel options in Jenkins:")
    for option in options:
        print(f"  - {option}")
    print("\nFix the excel name in your list or add an override in SBA.py.")


@dataclass
class BuildResult:
    console_url: str | None = None
    report_link: str | None = None
    could_not_trigger: bool = False

    @property
    def log_line(self) -> str:
        if self.report_link:
            return self.report_link
        if self.could_not_trigger and self.console_url:
            return f"Could not trigger → {self.console_url}"
        if self.console_url:
            return f"(report link not found) → {self.console_url}"
        return "N/A"

    @property
    def output_link(self) -> str:
        return self.report_link or self.console_url or ""

    @property
    def has_report_xlsx(self) -> bool:
        return bool(self.report_link and is_valid_report_link(self.report_link))


def is_valid_report_link(link: str) -> bool:
    if NULL_REPORT_MARKER in link.lower():
        return False
    return ".xlsx" in link.lower()


def extract_report_from_log(text: str) -> tuple[str | None, bool, bool]:
    """Return (valid_report_link, could_not_trigger, build_finished).

    🔥 FAST PATH: Jenkins always prints an "execution_url" marker, and right
    after it, either the null report (failure) or the real .xlsx report link.
    We check that first so we don't have to wait for "Finished: ..." to show up.
    """
    valid_link: str | None = None
    could_not_trigger = False

    marker_match = EXECUTION_URL_RE.search(text)
    if marker_match:
        window = text[marker_match.end(): marker_match.end() + EXECUTION_URL_WINDOW]
        link_match = REPORT_LINK_RE.search(window)
        if link_match:
            link = link_match.group(0)
            if is_valid_report_link(link):
                valid_link = link
            elif NULL_REPORT_MARKER in link.lower():
                could_not_trigger = True

    build_finished = bool(BUILD_FINISHED_RE.search(text))

    # 🔙 FALLBACK: only used if the fast path above found nothing yet
    # (e.g. marker not present yet, or link appears further down the log).
    if not valid_link and not could_not_trigger:
        saw_null = False
        for match in REPORT_LINK_RE.finditer(text):
            link = match.group(0)
            if is_valid_report_link(link):
                valid_link = link
            elif NULL_REPORT_MARKER in link.lower():
                saw_null = True

        build_failed = bool(BUILD_FAILURE_RE.search(text))
        if build_finished and not valid_link and (saw_null or build_failed):
            could_not_trigger = True

    return valid_link, could_not_trigger, build_finished


@dataclass
class SBAEntry:
    env_num: int
    tenant: str
    excel_label: str

    @property
    def display_env(self) -> str:
        return f"{self.env_num:02d}qa"

    @property
    def tenant_upper(self) -> str:
        return self.tenant.upper()

    @property
    def log_label(self) -> str:
        return f"{self.display_env} {self.tenant_upper}  {self.excel_label}"

    def jenkins_env_id(self) -> str:
        env_id = str(self.env_num) if self.env_num >= 100 else f"{self.env_num:02d}"
        prefix = "nttcom" if self.tenant_upper == "EAST" else "nttcw"
        return f"{prefix}{env_id}_qa"


def read_multiline_input(prompt: str) -> str:
    print(prompt)
    print("Finish with triple Enter:\n")

    lines = []
    empty = 0

    while True:
        line = input()
        if line.strip() == "":
            empty += 1
            if empty == 3:
                break
        else:
            empty = 0
        lines.append(line)

    return "\n".join(lines)


def normalize_tenant(tenant: str) -> str:
    tenant = tenant.strip().upper()
    if tenant == "COM":
        return "COM"
    return tenant.capitalize()


def random_env_tenant_pairs(env_pool: list[int]) -> Iterator[tuple[int, str]]:
    pairs = [(env_num, tenant) for env_num in env_pool for tenant in DEFAULT_TENANTS]
    if not pairs:
        raise ValueError("Env pool cannot be empty.")

    while True:
        shuffled_pairs = pairs[:]
        random.shuffle(shuffled_pairs)
        yield from shuffled_pairs


def fill_env_list_randomly(
    raw_text: str,
    env_pool: list[int] | None = None,
) -> list[SBAEntry]:
    entries: list[SBAEntry] = []
    assignments = random_env_tenant_pairs(env_pool or DEFAULT_ENV_POOL)

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = LINE_RE.match(line)
        if not match:
            continue

        env_str, tenant_str, excel_label = match.groups()
        excel_label = excel_label.strip()
        if not excel_label:
            continue

        random_env, random_tenant = next(assignments)
        env_num = int(env_str) if env_str else random_env
        tenant = normalize_tenant(tenant_str) if tenant_str else random_tenant

        entries.append(
            SBAEntry(
                env_num=env_num,
                tenant=tenant,
                excel_label=excel_label,
            )
        )

    return entries


def format_env_list(entries: list[SBAEntry]) -> str:
    return "\n".join(
        f"{entry.display_env} {entry.tenant.capitalize()}  {entry.excel_label}"
        for entry in entries
    )


def parse_env_list(raw_text: str) -> list[SBAEntry]:
    entries: list[SBAEntry] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = LINE_RE.match(line)
        if not match:
            raise ValueError(f"Could not parse line: {line}")

        env_str, tenant_str, excel_label = match.groups()
        if not env_str or not tenant_str:
            raise ValueError(f"Each line must include env and tenant: {line}")

        tenant = tenant_str.upper()
        if tenant == "COM":
            tenant_label = "COM"
        else:
            tenant_label = tenant.capitalize()

        entries.append(
            SBAEntry(
                env_num=int(env_str),
                tenant=tenant_label,
                excel_label=excel_label.strip(),
            )
        )

    return entries


def copy_to_clipboard(text: str, label: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
        print(f"{label} copied to clipboard.")
    except Exception as exc:
        print(f"Warning: could not copy {label.lower()} to clipboard: {exc}")


def format_summary(results: list[tuple[SBAEntry, BuildResult]]) -> str:
    lines = ["=" * 60, "SUMMARY", "=" * 60]
    lines.extend(f"{entry.log_label}: {result.log_line}" for entry, result in results)
    return "\n".join(lines)


def excel_cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_cell_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def entry_env_tokens(entry: SBAEntry) -> set[str]:
    return {entry.display_env.lower(), f"{entry.env_num}qa".lower()}


def row_has_entry_env_and_tenant(row_text: str, entry: SBAEntry) -> bool:
    normalized = normalize_cell_text(row_text)
    has_env = any(env_token in normalized for env_token in entry_env_tokens(entry))
    has_tenant = entry.tenant_upper.lower() in normalized
    return has_env and has_tenant


def strip_entry_prefix(text: str, entry: SBAEntry) -> str:
    candidate = text.split(":", 1)[0]
    for env_token in entry_env_tokens(entry):
        candidate = re.sub(re.escape(env_token), "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(EAST|WEST|COM)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate.strip(" -:\t")


def row_label_matches_entry(c_value: str, d_value: str, entry: SBAEntry) -> bool:
    target = normalize_for_excel_match(entry.excel_label)
    sources = (c_value, d_value, f"{c_value} {d_value}")

    for source in sources:
        candidates = (source, strip_entry_prefix(source, entry))
        for candidate in candidates:
            if normalize_for_excel_match(candidate) == target:
                return True

    return False


def find_matching_qg_rows(sheet, last_row: int, entry: SBAEntry) -> list[int]:
    matches: list[int] = []

    for row in range(1, last_row + 1):
        c_value = excel_cell_text(sheet.Cells(row, EXCEL_COL_ENV_INFO).Value)
        d_value = excel_cell_text(sheet.Cells(row, EXCEL_COL_TEST_INFO).Value)
        row_text = f"{c_value} {d_value}"

        if not row_has_entry_env_and_tenant(row_text, entry):
            continue
        if row_label_matches_entry(c_value, d_value, entry):
            matches.append(row)

    return matches


def get_last_qg_row(sheet) -> int:
    last_c = sheet.Cells(sheet.Rows.Count, EXCEL_COL_ENV_INFO).End(XL_UP).Row
    last_d = sheet.Cells(sheet.Rows.Count, EXCEL_COL_TEST_INFO).End(XL_UP).Row
    return max(last_c, last_d)


def get_qg_workbook(excel):
    target = str(QG_SCOPE_ANALYSIS_PATH.resolve()).casefold()

    for workbook in excel.Workbooks:
        try:
            workbook_path = str(Path(workbook.FullName).resolve()).casefold()
        except Exception:
            continue
        if workbook_path == target:
            return workbook, False

    return excel.Workbooks.Open(str(QG_SCOPE_ANALYSIS_PATH)), True


def update_qg_scope_analysis(results: list[tuple[SBAEntry, BuildResult]]) -> None:
    if not results:
        return

    if not QG_SCOPE_ANALYSIS_PATH.exists():
        print(f"Excel update skipped: workbook not found: {QG_SCOPE_ANALYSIS_PATH}")
        return

    try:
        import pythoncom
        import win32com.client
    except Exception as exc:
        print(f"Excel update skipped: win32com is not available: {exc}")
        return

    pythoncom.CoInitialize()
    excel = None
    workbook = None
    opened_excel = False
    workbook_opened_by_script = False
    previous_display_alerts = None

    try:
        try:
            excel = win32com.client.GetActiveObject("Excel.Application")
        except Exception:
            excel = win32com.client.DispatchEx("Excel.Application")
            opened_excel = True
            excel.Visible = False

        previous_display_alerts = excel.DisplayAlerts
        excel.DisplayAlerts = False

        workbook, workbook_opened_by_script = get_qg_workbook(excel)
        sheet = workbook.Worksheets(QG_SCOPE_SHEET)
        last_row = get_last_qg_row(sheet)

        updated = 0
        missing: list[str] = []
        ambiguous: list[str] = []

        for entry, result in results:
            matching_rows = find_matching_qg_rows(sheet, last_row, entry)
            if not matching_rows:
                missing.append(entry.log_label)
                continue
            if len(matching_rows) > 1:
                ambiguous.append(
                    f"{entry.log_label} (rows {', '.join(map(str, matching_rows))})"
                )
                continue

            row = matching_rows[0]
            sheet.Cells(row, EXCEL_COL_RESULT_LINK).Value = result.output_link
            if result.has_report_xlsx:
                sheet.Cells(row, EXCEL_COL_IN_PROGRESS).Value = "In Progress"
                sheet.Cells(row, EXCEL_COL_COULD_NOT_TRIGGER).ClearContents()
            else:
                sheet.Cells(row, EXCEL_COL_IN_PROGRESS).ClearContents()
                sheet.Cells(row, EXCEL_COL_COULD_NOT_TRIGGER).Value = (
                    "Could not trigger"
                )
            updated += 1

        workbook.Save()
        print(f"Excel updated: {updated} row(s) in '{QG_SCOPE_SHEET}'.")

        if missing:
            print("Warning: these results were not found in Excel:")
            for item in missing:
                print(f"  - {item}")

        if ambiguous:
            print("Warning: these results matched multiple Excel rows and were skipped:")
            for item in ambiguous:
                print(f"  - {item}")

    except Exception as exc:
        print(f"Excel update failed: {exc}")
    finally:
        if excel is not None and previous_display_alerts is not None:
            try:
                excel.DisplayAlerts = previous_display_alerts
            except Exception:
                pass
        if workbook is not None and workbook_opened_by_script:
            try:
                workbook.Close(SaveChanges=False)
            except Exception:
                pass
        if excel is not None and opened_excel:
            try:
                excel.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


async def wait_for_job_form(page: Page):
    await page.wait_for_load_state("domcontentloaded", timeout=180000)
    await page.wait_for_selector(
        'div.jenkins-form-item, form[name="parameters"], button[type="submit"]',
        timeout=180000,
    )


async def ensure_logged_in(page: Page):
    username = page.locator('input[name="j_username"]')
    if await username.count() == 0:
        return

    print("Logging in to Jenkins...")
    await username.fill(JENKINS_USER)
    await page.locator('input[name="j_password"]').fill(JENKINS_PASSWORD)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("domcontentloaded", timeout=180000)


async def get_select_option_names(select) -> list[str]:
    option_rows = await select.locator("option").evaluate_all(
        """options => options.map(o => ({
            value: o.value || '',
            label: (o.textContent || '').trim()
        }))"""
    )
    names: list[str] = []
    for option in option_rows:
        for key in ("value", "label"):
            name = option.get(key, "").strip()
            if name and name not in names:
                names.append(name)
    return names


async def get_excel_select(page: Page, expected_label: str | None = None):
    selectors = (
        'select.setting-input[name="value"]',
        'select[name="value"]',
        "select",
    )

    fallback = page.locator('select.setting-input[name="value"]').nth(1)

    for selector in selectors:
        selects = page.locator(selector)
        count = await selects.count()
        for idx in range(count):
            candidate = selects.nth(idx)
            options = await get_select_option_names(candidate)
            if expected_label and excel_label_to_option(expected_label, options):
                return candidate
            if any(".xlsx" in option.lower() for option in options):
                return candidate

    return fallback


async def get_excel_options(page: Page, expected_label: str | None = None) -> list[str]:
    excel_select = await get_excel_select(page, expected_label)
    if await excel_select.count() == 0:
        return []
    return await get_select_option_names(excel_select)


async def wait_for_excel_options(
    page: Page,
    expected_label: str | None = None,
    timeout_seconds: int = 15,
) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        options = await get_excel_options(page, expected_label)
        if options:
            return options
        await page.wait_for_timeout(500)

    return []


async def get_env_combobox(page: Page):
    return page.locator('input.setting-input.staticCombobox[name="value"]').first


async def get_tenant_select(page: Page):
    return page.locator('select.setting-input[name="value"]').first


async def read_combobox_options(page: Page) -> list[str]:
    env_input = await get_env_combobox(page)
    await env_input.click()
    await page.wait_for_timeout(500)

    options = await page.locator(".jenkins-dropdown-list .dropdown-item").evaluate_all(
        "items => items.map(i => (i.textContent || '').trim()).filter(Boolean)"
    )
    if options:
        return options

    datalist_id = await env_input.get_attribute("data-dropdown-id")
    if datalist_id:
        return await page.locator(f"#{datalist_id} option").evaluate_all(
            "options => options.map(o => o.value).filter(Boolean)"
        )

    current = await env_input.input_value()
    return [current] if current else []


async def set_env_value(page: Page, env_id: str) -> tuple[bool, str]:
    env_input = await get_env_combobox(page)
    await env_input.click()
    await env_input.fill("")
    await env_input.fill(env_id)
    await page.wait_for_timeout(400)
    await env_input.press("Tab")
    await page.wait_for_timeout(300)

    actual = (await env_input.input_value()).strip()
    if actual.lower() == env_id.lower():
        return True, actual

    options = await read_combobox_options(page)
    for option in options:
        if option.lower() == env_id.lower():
            await env_input.click()
            await env_input.fill(option)
            await env_input.press("Tab")
            return True, option

    fallback = actual or (options[0] if options else "nttcw17_qa")
    await env_input.click()
    await env_input.fill(fallback)
    await env_input.press("Tab")
    return False, fallback


async def set_tenant(page: Page, tenant: str):
    tenant_select = await get_tenant_select(page)
    value = tenant.upper()
    await tenant_select.select_option(value=value)


async def set_excel(page: Page, excel_option: str):
    excel_select = await get_excel_select(page, excel_option)
    try:
        await excel_select.select_option(value=excel_option)
    except Exception:
        await excel_select.select_option(label=excel_option)

    selected = await excel_select.input_value()
    selected_label = await excel_select.evaluate(
        "select => select.selectedOptions[0] ? select.selectedOptions[0].textContent.trim() : ''"
    )
    if selected != excel_option and selected_label != excel_option:
        raise RuntimeError(
            f"Excel select mismatch: requested '{excel_option}', "
            f"got value '{selected}' / label '{selected_label}'"
        )


async def click_build(page: Page):
    build_btn = page.get_by_role("button", name="Build").first
    if await build_btn.count() > 0:
        await build_btn.click()
        return

    await page.locator('button[type="submit"]').first.click()


async def wait_for_build_navigation(page: Page, timeout_seconds: int = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        current_url = page.url
        if BUILD_NUMBER_RE.search(current_url):
            return
        if "/queue/item/" in current_url:
            await page.wait_for_timeout(1000)
            continue
        if (
            "NTT_SBA_Testing_Tool" in current_url
            and "build?delay=0sec" not in current_url
        ):
            return
        await page.wait_for_timeout(500)


async def read_newest_build_number(page: Page) -> int | None:
    selectors = (
        "a.model-link.inside.build-link.display-name",
        "a.build-link.display-name",
        "a.build-link",
    )

    for selector in selectors:
        links = page.locator(selector)
        count = await links.count()
        for idx in range(count):
            link = links.nth(idx)
            href = await link.get_attribute("href") or ""
            match = BUILD_NUMBER_RE.search(href)
            if match:
                return int(match.group(1))

            text = (await link.inner_text()).strip()
            match = BUILD_HASH_RE.search(text)
            if match:
                return int(match.group(1))

    match = BUILD_NUMBER_RE.search(page.url)
    if match:
        return int(match.group(1))

    return None


async def capture_new_build_number(
    page: Page,
    after_number: int | None = None,
    timeout_seconds: int = 60,
) -> int:
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        match = BUILD_NUMBER_RE.search(page.url)
        if match:
            build_num = int(match.group(1))
            if after_number is None or build_num > after_number:
                return build_num

        if "NTT_SBA_Testing_Tool" in page.url:
            newest = await read_newest_build_number(page)
            if newest and (after_number is None or newest > after_number):
                return newest

        if "/queue/item/" in page.url:
            await page.wait_for_timeout(1000)
            continue

        if (
            "NTT_SBA_Testing_Tool" in page.url
            and "build?delay=0sec" not in page.url
            and not BUILD_NUMBER_RE.search(page.url)
        ):
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            continue

        await page.wait_for_timeout(1000)

    raise RuntimeError(
        f"Could not capture build number"
        f"{f' after #{after_number}' if after_number else ''}."
    )


def build_console_url(build_number: int) -> str:
    return f"{JOB_BASE}/{build_number}/console"


async def fetch_console_log_text(page: Page, console_url: str) -> str:
    text_url = console_url.replace("/console", "/consoleText")
    await page.goto(text_url, wait_until="domcontentloaded", timeout=60000)
    await ensure_logged_in(page)

    chunks: list[str] = []
    for selector in ("pre", "body"):
        locator = page.locator(selector)
        if await locator.count() > 0:
            try:
                chunks.append(await locator.first.inner_text())
            except Exception:
                pass

    try:
        chunks.append(await page.content())
    except Exception:
        pass

    return "\n".join(chunks)


async def wait_for_build_result(
    page: Page,
    console_url: str,
    timeout_seconds: int = 180,
) -> BuildResult:
    if "lastBuild" in console_url:
        raise ValueError(f"Refusing to poll lastBuild console URL: {console_url}")

    console_url = console_url.replace("/consoleText", "/console").split("#")[0]
    if not console_url.endswith("/console"):
        console_url = console_url.rstrip("/") + "/console"

    match = BUILD_NUMBER_RE.search(console_url)
    if not match:
        raise ValueError(f"Console URL must include build number: {console_url}")

    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        log_text = await fetch_console_log_text(page, console_url)
        valid_link, could_not_trigger, build_finished = extract_report_from_log(log_text)

        if valid_link:
            return BuildResult(console_url=console_url, report_link=valid_link)
        if could_not_trigger:
            return BuildResult(console_url=console_url, could_not_trigger=True)

        # execution_url marker not seen yet, or link not there yet — keep waiting
        await page.wait_for_timeout(2000)

    return BuildResult(console_url=console_url)


async def rebuild_with_env(page: Page, env_id: str, decoy_build_number: int):
    await page.goto(
        f"{JOB_BASE}/{decoy_build_number}/",
        wait_until="domcontentloaded",
        timeout=180000,
    )
    await ensure_logged_in(page)

    rebuild_link = page.locator('a.task-link:has-text("Rebuild")').first
    if await rebuild_link.count() == 0:
        rebuild_link = page.get_by_role("link", name="Rebuild").first
    await rebuild_link.click()
    await wait_for_job_form(page)

    env_inputs = page.locator('input[name="value"][type="text"]')
    count = await env_inputs.count()
    for idx in range(count):
        candidate = env_inputs.nth(idx)
        value = (await candidate.input_value()).strip().lower()
        if value.startswith("nttcom") or value.startswith("nttcw"):
            await candidate.fill(env_id)
            break

    rebuild_btn = page.get_by_role("button", name="Rebuild").first
    if await rebuild_btn.count() > 0:
        await rebuild_btn.click()
    else:
        await page.locator('button[type="submit"]').first.click()

    await wait_for_build_navigation(page)


async def trigger_build_for_entry(
    page: Page,
    entry: SBAEntry,
    excel_options: list[str],
    index: int,
    total: int,
) -> BuildResult:
    env_id = entry.jenkins_env_id()
    await page.goto(JENKINS_URL, wait_until="domcontentloaded", timeout=180000)
    await ensure_logged_in(page)
    await wait_for_job_form(page)

    current_excel_options = excel_options or await wait_for_excel_options(
        page,
        entry.excel_label,
    )
    excel_option = excel_label_to_option(entry.excel_label, current_excel_options)
    if not excel_option:
        expected = expected_excel_filename(entry.excel_label)
        if current_excel_options:
            raise ExcelNotFoundError(entry.excel_label, expected, current_excel_options)
        excel_option = expected
        print(
            f"[{index}/{total}] Jenkins excel options were not readable; "
            f"trying expected option directly: {excel_option}"
        )

    env_ok, used_env = await set_env_value(page, env_id)
    await set_tenant(page, entry.tenant_upper)
    await set_excel(page, excel_option)

    print(f"[{index}/{total}] Excel selected: {excel_option}")
    print(f"[{index}/{total}] Triggering build: {entry.log_label}")

    before_build = await read_newest_build_number(page)
    await click_build(page)
    build_number = await capture_new_build_number(page, after_number=before_build)
    console_url = build_console_url(build_number)
    print(f"[{index}/{total}] Build tracked as #{build_number} → {console_url}")

    if not env_ok:
        print(
            f"[{index}/{total}] Env {env_id} not in list; "
            f"used {used_env}, rebuilding with correct env..."
        )
        await rebuild_with_env(page, env_id, build_number)
        build_number = await capture_new_build_number(page, after_number=build_number)
        console_url = build_console_url(build_number)
        print(f"[{index}/{total}] Rebuild tracked as #{build_number} → {console_url}")

    print(f"[{index}/{total}] Checking console for report link...")
    result = await wait_for_build_result(page, console_url)
    print(f"{entry.log_label}: {result.log_line}")
    return result


async def run_jenkins(entries: list[SBAEntry]):
    if not entries:
        print("No entries to run.")
        return

    results: list[tuple[SBAEntry, BuildResult]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await new_context_with_session(browser, saved_session(SBA_SESSION_NAME))
        page = await context.new_page()

        await page.goto(JENKINS_URL, wait_until="domcontentloaded", timeout=180000)
        await ensure_logged_in(page)
        await wait_for_job_form(page)
        # Se guarda hasta que el formulario del job ya cargó (prueba de que sí
        # entró): ensure_logged_in sólo espera domcontentloaded y podría
        # resolver antes de que el login termine.
        await persist_session(context, SBA_SESSION_NAME)
        excel_options = await wait_for_excel_options(page)

        if excel_options:
            missing = find_missing_excel_labels(entries, excel_options)
            if missing:
                print_excel_not_found_error(missing, excel_options)
                await asyncio.sleep(999999)
                return
        else:
            print(
                "Warning: Jenkins excel options were not readable during "
                "pre-check; each build will be validated on its own page."
            )

        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            try:
                result = await trigger_build_for_entry(
                    page, entry, excel_options, index, total
                )
            except ExcelNotFoundError as exc:
                print_excel_not_found_error(
                    [(entry, exc.expected)],
                    exc.options,
                )
                await asyncio.sleep(999999)
                return
            results.append((entry, result))

        summary = format_summary(results)
        print("\n" + summary)
        copy_to_clipboard(summary, "Summary")
        update_qg_scope_analysis(results)

        await context.close()
        await browser.close()
        print("\nBrowser closed.")


def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = input(f"{prompt} (y/n): ").strip().lower()
        if answer in {"y", "yes", "s", "si"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def main():
    entries: list[SBAEntry]

    if ask_yes_no("\nDo you want to create/fill the env list first?"):
        raw_text = read_multiline_input("\nPaste excel lines:")
        entries = fill_env_list_randomly(raw_text, DEFAULT_ENV_POOL)
        filled = format_env_list(entries)
        filled_output = f"Filled list:\n\n{filled}"
        print(f"\n{filled_output}\n")
        copy_to_clipboard(filled_output, "Filled list")

        if not ask_yes_no("Continue with this list?"):
            print("Cancelled.")
            return
    else:
        raw_text = read_multiline_input(
            "\nPaste the final env list (env tenant excel on each line):"
        )
        entries = parse_env_list(raw_text)

    if not entries:
        print("No entries found.")
        return

    print(f"\n{len(entries)} builds to trigger.")
    asyncio.run(run_jenkins(entries))


if __name__ == "__main__":
    main()
