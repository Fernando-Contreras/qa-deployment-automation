"""
Divided_SBA: split-file SBA runs.

Pick a folder under Documents\\NTT\\SBA (ntt_config.SBA_ROOT) and this script opens
one browser tab per file inside it, fills the same parameters on
http://192.0.2.3:9312/ (environment / tenant / execution), attaches the file to
the upload area and hits Execute. Every file in the folder is executed, one tab
each, and the tabs are left open so you can watch them.

Usage:
    python Divided_SBA.py
    python Divided_SBA.py --folder IF29_int_main_scenarios_v2
    python Divided_SBA.py --env 78          # force an environment instead of random
    python Divided_SBA.py --dump            # print what the script can see (debug)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Locator, Page, async_playwright

import ntt_config

APP_URL = "http://192.0.2.3:9312/"
SBA_ROOT = ntt_config.SBA_ROOT
LOG_DIR = Path(__file__).with_name("Divided_SBA_logs")

# Environments look like nttcw07_qa / nttcom10_qa; only these numbers are allowed.
ALLOWED_ENV_NUMBERS = ("07", "10", "78", "73")
ENV_SUFFIX = "_qa"
ALLOWED_ENV_RE = re.compile(
    rf"(?:^|\D)({'|'.join(ALLOWED_ENV_NUMBERS)}){ENV_SUFFIX}$", re.IGNORECASE
)
TENANT = "EST"
EXECUTION = "Actions"

# Links the app produces after Execute (execution page / xlsx report).
RESULT_LINK_RE = re.compile(r"https?://[^\s'\"<>]*:9312/[^\s'\"<>]+", re.IGNORECASE)
INTERESTING_LINK_HINTS = ("report=", "sheet-based", "execution", "result")
LINK_WAIT_SECONDS = 90

SKIP_FILE_PREFIXES = ("~$", ".")
SKIP_FILE_SUFFIXES = (".lnk", ".tmp")

# (keywords matched against the field's own text, positional fallback index)
ENV_FIELD = (["select environment", "environment"], 0)
TENANT_FIELD = (["select tenant", "tenant"], 1)
EXECUTION_FIELD = (["select execution", "execution", "actions"], 2)

FIELD_SELECTOR = (
    "label.ux-dropdown-field__label, .ux-dropdown-field__label, ux-dropdown-field, "
    'ux-select, select, [role="combobox"]'
)

VISIBLE_JS = """
const isVisible = el => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
};
const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
"""

FIND_FIELDS_JS = f"""
(selector) => {{
    {VISIBLE_JS}
    let nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
    // keep the innermost element of each nested group (the clickable label)
    nodes = nodes.filter(el => !nodes.some(other => other !== el && el.contains(other)));
    nodes.forEach((el, i) => el.setAttribute('data-sba-field', String(i)));
    return nodes.map((el, i) => ({{
        idx: i,
        tag: el.tagName.toLowerCase(),
        text: norm(el.innerText || el.value || ''),
    }}));
}}
"""

COLLECT_OPTIONS_JS = f"""
() => {{
    {VISIBLE_JS}
    const roots = Array.from(document.querySelectorAll(
        '.cdk-overlay-container, [role="listbox"], [class*="dropdown-list"], ' +
        '[class*="dropdown__list"], [class*="select__list"], [class*="option-list"]'
    )).filter(isVisible);

    const items = [];
    const seen = new Set();
    for (const root of roots) {{
        const candidates = Array.from(root.querySelectorAll(
            '[role="option"], li, [class*="option"], [class*="item"]'
        )).filter(isVisible);
        for (const el of candidates) {{
            if (candidates.some(other => other !== el && el.contains(other))) continue;
            const text = norm(el.innerText);
            if (!text || seen.has(text)) continue;
            seen.add(text);
            items.push(el);
        }}
    }}
    items.forEach((el, i) => el.setAttribute('data-sba-opt', String(i)));
    return items.map((el, i) => ({{ idx: i, text: norm(el.innerText) }}));
}}
"""

DROP_FILE_JS = """
async ([b64, name, mime]) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const file = new File([bytes], name, { type: mime });

    const transfer = new DataTransfer();
    transfer.items.add(file);

    const wrapper = document.querySelector('.ux-upload__drop-area-text-wrapper');
    const area =
        (wrapper && (wrapper.closest('.ux-upload__drop-area') || wrapper.parentElement)) ||
        document.querySelector('.ux-upload__drop-area') ||
        document.querySelector('ux-upload') ||
        document.querySelector('[class*="drop-area"]');
    if (!area) return false;

    for (const type of ['dragenter', 'dragover', 'drop']) {
        area.dispatchEvent(new DragEvent(type, {
            bubbles: true,
            cancelable: true,
            dataTransfer: transfer,
        }));
    }
    return true;
}
"""

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ControlNotFoundError(Exception):
    """Raised when a dropdown, an option or the upload area cannot be located."""


@dataclass
class Field:
    locator: Locator
    text: str
    is_native: bool


def natural_key(name: str) -> list:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    ]


def list_folders(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(f"SBA root folder not found: {root}")
    folders = [item for item in root.iterdir() if item.is_dir()]
    return sorted(folders, key=lambda item: natural_key(item.name))


def list_files(folder: Path) -> list[Path]:
    files = [
        item
        for item in folder.iterdir()
        if item.is_file()
        and not item.name.startswith(SKIP_FILE_PREFIXES)
        and item.suffix.lower() not in SKIP_FILE_SUFFIXES
    ]
    return sorted(files, key=lambda item: natural_key(item.name))


def choose_from_menu(title: str, options: list[str]) -> int:
    print(f"\n{title}")
    for index, option in enumerate(options, start=1):
        print(f"  {index:>2}. {option}")

    while True:
        answer = input("\nPick a number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1

        matches = [
            index
            for index, option in enumerate(options)
            if answer and answer.lower() in option.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        print("More than one match." if matches else "Not a valid choice.")


def is_allowed_env(option: str) -> bool:
    return bool(ALLOWED_ENV_RE.search(option.strip()))


def short_label(file_path: Path, folder_name: str) -> str:
    """'IF29_..._v2 - 11to20.xlsx' -> '11to20' (nice and short for Webex)."""
    stem = file_path.stem
    if " - " in stem:
        return stem.split(" - ", 1)[1].strip()
    if stem.lower().startswith(folder_name.lower()):
        trimmed = stem[len(folder_name):].strip(" -_")
        if trimmed:
            return trimmed
    return stem


def copy_to_clipboard(text: str, label: str = "Summary") -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
        print(f"\n{label} copied to clipboard.")
    except Exception as exc:
        print(f"\nWarning: could not copy {label.lower()} to clipboard: {exc}")


async def find_fields(page: Page) -> list[dict]:
    return await page.evaluate(FIND_FIELDS_JS, FIELD_SELECTOR)


async def get_field(page: Page, keywords: list[str], position: int, label: str) -> Field:
    fields = await find_fields(page)
    if not fields:
        raise ControlNotFoundError(
            f"No dropdown fields found on the page (looking for '{label}')."
        )

    chosen = None
    for keyword in keywords:
        for field in fields:
            if keyword in field["text"].lower():
                chosen = field
                break
        if chosen:
            break

    if chosen is None and position < len(fields):
        chosen = fields[position]
        print(f"    (no label matched '{label}', using dropdown #{position + 1})")

    if chosen is None:
        raise ControlNotFoundError(f"Could not locate the '{label}' dropdown.")

    return Field(
        locator=page.locator(f'[data-sba-field="{chosen["idx"]}"]').first,
        text=chosen["text"],
        is_native=chosen["tag"] == "select",
    )


async def click_field(page: Page, field: Field) -> None:
    try:
        await field.locator.click(timeout=8000)
    except Exception:
        await field.locator.evaluate("el => el.click()")
    await page.wait_for_timeout(300)


async def read_options(page: Page, timeout_seconds: float = 10.0) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        options = await page.evaluate(COLLECT_OPTIONS_JS)
        if options:
            return options
        await page.wait_for_timeout(250)

    return []


async def close_dropdown(page: Page) -> None:
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def open_field(page: Page, keywords: list[str], position: int, label: str):
    """Open a dropdown and return (field, options). Native selects stay closed."""
    field = await get_field(page, keywords, position, label)

    if field.is_native:
        rows = await field.locator.locator("option").evaluate_all(
            """options => options.map(o => (o.textContent || o.value || '').trim())"""
        )
        return field, [{"idx": i, "text": text} for i, text in enumerate(rows) if text]

    await click_field(page, field)
    options = await read_options(page)
    if not options:
        await close_dropdown(page)
        raise ControlNotFoundError(f"The '{label}' dropdown opened but showed no options.")
    return field, options


async def pick_option(page: Page, field: Field, options: list[dict], wanted: str) -> None:
    target = wanted.strip().lower()

    if field.is_native:
        await field.locator.select_option(label=wanted)
        return

    match = next((o for o in options if o["text"].strip().lower() == target), None)
    if match is None:
        match = next((o for o in options if target in o["text"].strip().lower()), None)
    if match is None:
        await close_dropdown(page)
        available = ", ".join(o["text"] for o in options[:15])
        raise ControlNotFoundError(f"Option '{wanted}' not found. Available: {available}")

    option = page.locator(f'[data-sba-opt="{match["idx"]}"]').first
    try:
        await option.click(timeout=8000)
    except Exception:
        await option.evaluate("el => el.click()")
    await page.wait_for_timeout(400)


async def select_value(page: Page, field_spec, wanted: str, label: str) -> None:
    keywords, position = field_spec

    current = await get_field(page, keywords, position, label)
    if current.text.strip().lower() == wanted.strip().lower():
        return  # already set (e.g. Actions preselected)

    field, options = await open_field(page, keywords, position, label)
    await pick_option(page, field, options, wanted)


async def upload_file(page: Page, file_path: Path) -> str:
    """Attach the file to the ux-upload area. Returns which strategy worked."""
    file_input = page.locator('input[type="file"]')
    if await file_input.count() > 0:
        try:
            await file_input.first.set_input_files(str(file_path))
            return "file input"
        except Exception:
            pass

    browse_selectors = (
        ".ux-upload__drop-area-button button",
        'ux-button:has-text("browse") button',
        'button:has-text("browse")',
    )
    for selector in browse_selectors:
        button = page.locator(selector).first
        if await button.count() == 0:
            continue
        try:
            async with page.expect_file_chooser(timeout=8000) as chooser_info:
                await button.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(file_path))
            return "browse dialog"
        except Exception:
            continue

    payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
    if await page.evaluate(DROP_FILE_JS, [payload, file_path.name, XLSX_MIME]):
        return "drag & drop"

    raise ControlNotFoundError(f"Could not attach {file_path.name} to the upload area.")


async def confirm_attachment(page: Page, file_name: str, timeout_seconds: float = 15.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    stem = Path(file_name).stem

    while asyncio.get_running_loop().time() < deadline:
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
        if file_name in body or stem in body:
            return True
        await page.wait_for_timeout(400)

    return False


async def click_execute(page: Page) -> None:
    button = page.get_by_role("button", name=re.compile(r"^\s*execute\s*$", re.I)).first
    if await button.count() > 0:
        await button.click()
        return

    for selector in (
        'ux-button:has-text("Execute") button',
        'button:has-text("Execute")',
        '[class*="button"]:has-text("Execute")',
    ):
        fallback = page.locator(selector).first
        if await fallback.count() > 0:
            await fallback.click()
            return

    raise ControlNotFoundError("Execute button not found.")


async def wait_for_form(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded", timeout=120000)
    try:
        await page.wait_for_selector(
            ".ux-dropdown-field__label, ux-dropdown-field, select", timeout=60000
        )
    except Exception:
        pass
    await page.wait_for_timeout(1200)


async def dump_page(page: Page) -> None:
    print("\n" + "=" * 60)
    print("DEBUG DUMP")
    print("=" * 60)

    fields = await find_fields(page)
    print(f"\nDropdown fields found: {len(fields)}")
    for field in fields:
        print(f"  #{field['idx'] + 1:<3} <{field['tag']}>  text='{field['text']}'")

    print(f"\nfile inputs:  {await page.locator('input[type=file]').count()}")
    print(f"drop areas:   {await page.locator('[class*=drop-area]').count()}")
    print(f"buttons:      {await page.locator('button').count()}")
    labels = await page.locator("button").evaluate_all(
        """items => items.map(i => (i.textContent || '').replace(/\\s+/g,' ').trim())
                        .filter(Boolean).slice(0, 20)"""
    )
    print(f"button texts: {labels}")


async def fill_parameters(page: Page, env_value: str, prefix: str = "") -> None:
    await select_value(page, ENV_FIELD, env_value, "environment")
    print(f"{prefix}   environment: {env_value}")

    await select_value(page, TENANT_FIELD, TENANT, "tenant")
    print(f"{prefix}   tenant: {TENANT}")

    await select_value(page, EXECUTION_FIELD, EXECUTION, "execution")
    print(f"{prefix}   execution: {EXECUTION}")


async def collect_page_links(page: Page) -> list[str]:
    hrefs = await page.locator("a[href]").evaluate_all(
        "items => items.map(i => i.href).filter(Boolean)"
    )
    try:
        body = await page.locator("body").inner_text()
    except Exception:
        body = ""

    found: list[str] = []
    for candidate in hrefs + RESULT_LINK_RE.findall(body):
        candidate = candidate.strip().rstrip(".,;)")
        if candidate and candidate not in found and RESULT_LINK_RE.match(candidate):
            found.append(candidate)
    return found


async def harvest_link(page: Page, timeout_seconds: float = LINK_WAIT_SECONDS) -> str:
    """Wait for the execution/report link the app shows after Execute."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    root = APP_URL.rstrip("/")
    fallback = ""

    while asyncio.get_running_loop().time() < deadline:
        current = page.url.strip()
        if current.rstrip("/") != root and RESULT_LINK_RE.match(current):
            if any(hint in current.lower() for hint in INTERESTING_LINK_HINTS):
                return current
            fallback = fallback or current

        for link in await collect_page_links(page):
            if link.rstrip("/") == root:
                continue
            if any(hint in link.lower() for hint in INTERESTING_LINK_HINTS):
                return link
            fallback = fallback or link

        await page.wait_for_timeout(2000)

    return fallback


async def run_file(context, file_path: Path, env_value: str, index: int, total: int):
    page = await context.new_page()
    prefix = f"[{index}/{total}]"
    print(f"\n{prefix} {file_path.name}")

    try:
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=120000)
        await wait_for_form(page)
        await fill_parameters(page, env_value, prefix)

        method = await upload_file(page, file_path)
        print(f"{prefix}   attached via {method}")

        if not await confirm_attachment(page, file_path.name):
            print(f"{prefix}   WARNING: the file name never showed up on the page.")

        await click_execute(page)
        await page.wait_for_timeout(2500)
        print(f"{prefix}   Execute clicked. Tab left open.")
        return page, True

    except Exception as exc:
        print(f"{prefix}   FAILED: {exc}")
        return page, False


def resolve_env(options: list[str], forced: str | None) -> str:
    allowed = [option for option in options if is_allowed_env(option)]

    if forced:
        pool = allowed or options
        needle = forced.strip().lower()
        matches = [option for option in pool if needle in option.lower()]
        if matches:
            return matches[0]
        print(f"'{forced}' did not match any environment; picking one at random.")

    if allowed:
        return random.choice(allowed)

    print(
        f"\nNo environment matches {ALLOWED_ENV_NUMBERS} + '{ENV_SUFFIX}' "
        f"(e.g. nttcw07_qa). Pick one manually:"
    )
    return options[choose_from_menu("Environments:", options)]


def build_report(
    folder_name: str,
    env_value: str,
    rows: list[tuple[Path, bool, str]],
) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    width = max((len(short_label(path, folder_name)) for path, _, _ in rows), default=0)

    lines = [
        f"SBA – {folder_name}",
        f"{env_value} | {TENANT} | {EXECUTION} | {stamp}",
        "",
    ]
    for path, ok, link in rows:
        label = short_label(path, folder_name).ljust(width)
        if link:
            lines.append(f"{label}  {link}")
        elif ok:
            lines.append(f"{label}  (triggered, no link yet)")
        else:
            lines.append(f"{label}  FAILED")

    failures = [path for path, ok, _ in rows if not ok]
    if failures:
        lines.append("")
        lines.append(f"{len(failures)} of {len(rows)} could not be triggered.")

    return "\n".join(lines)


def save_report(folder_name: str, report: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{folder_name}_{stamp}.txt"
    log_path.write_text(report, encoding="utf-8")
    return log_path


async def run(
    folder_name: str,
    files: list[Path],
    forced_env: str | None,
    dump: bool,
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        print(f"\nOpening {APP_URL} ...")
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=120000)
        await wait_for_form(page)

        if dump:
            await dump_page(page)

        keywords, position = ENV_FIELD
        field, options = await open_field(page, keywords, position, "environment")
        env_options = [option["text"] for option in options]
        if not field.is_native:
            await close_dropdown(page)

        env_value = resolve_env(env_options, forced_env)
        print(f"\nEnvironment for this run: {env_value}")
        await page.close()

        total = len(files)
        triggered: list[tuple[Path, bool, Page]] = []
        for index, file_path in enumerate(files, start=1):
            tab, ok = await run_file(context, file_path, env_value, index, total)
            triggered.append((file_path, ok, tab))

        print("\nCollecting execution links...")
        rows: list[tuple[Path, bool, str]] = []
        for file_path, ok, tab in triggered:
            link = ""
            if ok:
                try:
                    link = await harvest_link(tab)
                except Exception as exc:
                    print(f"  {file_path.name}: could not read the link ({exc})")
            if not link and ok:
                print(f"  {file_path.name}: no link found yet")
            rows.append((file_path, ok, link))

        report = build_report(folder_name, env_value, rows)
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)

        copy_to_clipboard(report, "Report")
        log_path = save_report(folder_name, report)
        print(f"Log saved to {log_path}")

        input("\nTabs are still open. Press Enter here to close the browser...")
        await context.close()
        await browser.close()
        print("Browser closed.")


def pick_folder(folders: list[Path], requested: str | None) -> Path:
    if requested:
        matches = [f for f in folders if f.name.lower() == requested.lower()]
        if not matches:
            matches = [f for f in folders if requested.lower() in f.name.lower()]
        if len(matches) == 1:
            return matches[0]
        print(f"'{requested}' matched {len(matches)} folders; pick from the menu.")

    return folders[choose_from_menu("Folders in SBA:", [f.name for f in folders])]


def main() -> None:
    parser = argparse.ArgumentParser(description="Trigger one SBA run per file in a folder.")
    parser.add_argument("--folder", help="Folder name under the SBA root (skips the menu).")
    parser.add_argument("--env", help="Force an environment, e.g. 78 (default: random).")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print the dropdowns/upload areas the script can see, for debugging.",
    )
    args = parser.parse_args()

    folders = list_folders(SBA_ROOT)
    if not folders:
        print(f"No folders inside {SBA_ROOT}")
        return

    folder = pick_folder(folders, args.folder)
    files = list_files(folder)
    if not files:
        print(f"No files inside {folder}")
        return

    print(f"\n{len(files)} file(s) in {folder.name} — one tab each:")
    for index, file_path in enumerate(files, start=1):
        print(f"  {index:>2}. {file_path.name}")

    asyncio.run(run(folder.name, files, args.env, args.dump))


if __name__ == "__main__":
    main()
