import asyncio
import difflib
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import win32com.client as win32
from playwright.async_api import async_playwright

import ntt_config
from jenkins_web import (
    fetch_json_from_browser,
    new_context_with_session,
    persist_session,
    saved_session,
)
from nc_credentials import LoginError, get_credentials


txt_path = str(ntt_config.DAILY_ENVS_PATH)
excel_path = str(ntt_config.QG_SCOPE_ANALYSIS_PATH)
sheet_name = "BuildDeployments"

DAILY_ENVS_PATH = Path(txt_path)
NOTES_DIR = ntt_config.NOTES_DIR
LOG_PATH = ntt_config.PATCH_STATUS_LOG
EXCEL_PATH = excel_path
SHEET_NAME = sheet_name

# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
USERNAME, PASSWORD = get_credentials("jenkins", default_username=ntt_config.JENKINS_DEFAULT_USER)

# Cuántos envs se revisan al mismo tiempo en Jenkins.
CONCURRENCY = 8

wdStory = 6
xlHAlignCenter = -4108
wdColorWhite = 0xFFFFFF
wdAlignParagraphCenter = 1
PATCH_STATUS_FIRST_COL = 8  # H
PATCH_STATUS_LAST_COL = 42  # AP - AQ (43) holds the patch name, not a status
PATCH_NAME_COL = 43  # AQ - patch name, never a status column
BLOCK_HEADER_RE = re.compile(
    r"^#{0,6}\s*\*\*(?:T[\d.]+|CI_\d+|\d+CI\b).*?\*\*\s*$", re.MULTILINE
)
# Env labels are usually just digits ("32QA"), but some projects prefix them
# with a letter code ("COM12QA") - accept both.
ENV_LABEL_RE = re.compile(r"\[(?:\*\*)?([A-Za-z]*\d+QA)(?:\*\*)?\]", re.IGNORECASE)
ENV_LINK_RE = re.compile(
    r"\[(?:\*\*)?[A-Za-z]*\d+QA(?:\*\*)?\]\((https?://[^)]+)\)",
    re.IGNORECASE,
)
BATCH_ENV_VAR = "NTT_DAILY_ENVS_BATCH"

status_map = {
    "IN PROGRESS": "In Progress",
    "ON HOLD": "On Hold",
    "PASSED": "Passed",
    "FAILED": "Failed",
    "BLOCKED": "Blocked",
    "SKIPPED": "Skipped",
}


STATUS_HEADER_RE = re.compile(
    r"\*\*\s*(IN PROGRESS|ON HOLD|PASSED|FAILED|BLOCKED|SKIPPED)\s*\*\*",
    re.IGNORECASE,
)


def match_status_header(line):
    """Estado que abre una línea de encabezado, o None.

    Acepta texto después del estado ("**ON HOLD** till QA team finishes:"),
    no sólo "**ON HOLD**:". Una línea de env nunca cuenta como encabezado.
    """
    if ENV_LABEL_RE.search(line):
        return None
    match = STATUS_HEADER_RE.search(line)
    return status_map[match.group(1).upper()] if match else None


def version_key(version):
    nums = re.findall(r"\d+", version)
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums)


def format_env_key(prefix, num):
    """Canonical env key: uppercase letter prefix (if any) + zero-padded
    number + 'QA'. The literal 'cw' project code means no prefix at all -
    that's how the plain numeric envs ('32QA') are named in the URLs."""
    prefix = (prefix or "").upper()
    if prefix == "CW":
        prefix = ""
    return f"{prefix}{int(num):02d}QA"


def normalize_env_key(raw_label):
    """Turn an env label as written in Daily_Envs.txt brackets (e.g. '32QA',
    'COM12QA', '06QA') into the same canonical form extract_env_label(url)
    produces, so both sides can be compared."""
    match = re.match(r"([A-Za-z]*)(\d+)QA$", raw_label.strip(), re.IGNORECASE)
    if not match:
        return raw_label.strip().upper()
    return format_env_key(match.group(1), match.group(2))


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_daily_env_batches(text: str):
    text = normalize_newlines(text)
    matches = list(BLOCK_HEADER_RE.finditer(text))
    if not matches:
        return []

    blocks = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append(text[start:end].strip("\n"))

    return blocks


def count_env_lines(lines):
    return sum(1 for line in lines if ENV_LABEL_RE.search(line))


def trigger_webex_update(path):
    """Sync Daily_Envs.txt to Webex before we delete the batch block - once
    it's gone, Webex_Update can no longer see it to post/edit its message.
    Best-effort: a Webex failure shouldn't stop the batch from being cleaned up."""
    script = Path(__file__).with_name("Webex_Update.py")
    if not script.exists():
        print(f"No se encontro {script.name}; se omite la actualizacion de Webex.")
        return

    print("\nActualizando Webex...")
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--file", str(path)],
            check=False,
        )
        if result.returncode != 0:
            print(f"Webex_Update.py termino con codigo {result.returncode}; se continua de todos modos.")
    except Exception as exc:
        print(f"No se pudo correr Webex_Update.py: {exc}")


def remove_batch_from_daily_envs(path, batch_index):
    """Once a batch's mail has been written, delete that whole block
    (header, ---, envs, tail) from Daily_Envs.txt so it stops cluttering
    the file. Skips safely if the batch boundaries can't be found again."""
    raw_text = Path(path).read_text(encoding="utf-8")
    matches = list(BLOCK_HEADER_RE.finditer(raw_text))

    if not matches or not (1 <= batch_index <= len(matches)):
        print("No se pudo aislar el bloque para borrarlo; Daily_Envs.txt se deja igual.")
        return

    start = matches[batch_index - 1].start()
    end = matches[batch_index].start() if batch_index < len(matches) else len(raw_text)

    new_text = raw_text[:start] + raw_text[end:]
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)

    Path(path).write_text(new_text, encoding="utf-8")
    print("Bloque borrado de Daily_Envs.txt.")


def choose_daily_env_batch(text: str):
    batches = split_daily_env_batches(text)
    if not batches:
        return 1, text.splitlines()

    if len(batches) == 1:
        header = batches[0].splitlines()[0].strip()
        print(f"Usando unico batch: 1. {header}")
        return 1, batches[0].splitlines()

    print("\nBatches encontrados en Daily_Envs.txt:")
    for index, batch in enumerate(batches, start=1):
        batch_lines = batch.splitlines()
        header = batch_lines[0].strip() if batch_lines else "(sin header)"
        env_count = count_env_lines(batch_lines)
        print(f"{index}. {header} ({env_count} envs)")

    while True:
        answer = input(f"\nQue batch quieres usar? (1-{len(batches)}): ").strip()
        if answer.isdigit():
            batch_index = int(answer)
            if 1 <= batch_index <= len(batches):
                return batch_index, batches[batch_index - 1].splitlines()
        print(f"Seleccion invalida. Escribe un numero entre 1 y {len(batches)}.")


def extract_env_details(line):
    env_match = ENV_LABEL_RE.search(line)
    version_match = re.search(r"\]\([^)]+\)\s*\(([^)]+)\)", line)
    if not env_match or not version_match:
        return None

    env = env_match.group(1)
    version = version_match.group(1)
    num = int(re.sub(r"\D", "", env))

    link_match = ENV_LINK_RE.search(line)
    link = link_match.group(1) if link_match else None

    east = re.search(r"EAST:\s*(\d+)", line)
    west = re.search(r"WEST:\s*(\d+)", line)

    return {
        "num": num,
        "env": env,
        "version": version,
        "link": link,
        "east": east.group(1) if east else None,
        "west": west.group(1) if west else None,
    }


def deployment_key(env, version):
    return (env.upper(), version.upper())


def get_checkable_envs(lines):
    """Envs that are In Progress in Daily_Envs.txt - the only ones worth
    spending time checking in Jenkins. Passed envs never need it, and
    Failed envs already tell us which patch failed in the text itself, so
    they're handled directly from that (see mark_failed_row)."""
    envs = set()
    current_status = None

    for line in lines:
        header_status = match_status_header(line)
        if header_status:
            current_status = header_status
            continue

        if (
            current_status == "In Progress"
            and ENV_LABEL_RE.search(line)
            and "(" in line
        ):
            env_match = ENV_LABEL_RE.search(line)
            envs.add(normalize_env_key(env_match.group(1)))

    return envs


def extract_failed_details(line):
    details = extract_env_details(line)
    if not details:
        return None

    failed_match = re.search(r"\]\([^)]+\)(?:\*\*)?\s*\([^)]*\)\s*-\s*(.+)$", line)
    if not failed_match:
        return None

    failed_text = failed_match.group(1).strip()
    tms_match = re.search(r"https?://jira\.example\.com/browse/([A-Z]+-\d+)", failed_text)

    patch_name = failed_text
    if tms_match:
        patch_name = failed_text[:tms_match.start()].rstrip(" :")

    return {
        "env": details["env"],
        "version": details["version"],
        "patch": patch_name,
        "ticket": tms_match.group(1) if tms_match else None,
        "ticket_url": tms_match.group(0) if tms_match else None,
    }


def extract_patch_reference(line):
    stripped = line.strip()
    if not stripped:
        return None

    file_match = re.search(r"(file://\S+)", stripped, re.IGNORECASE)
    if file_match:
        return file_match.group(1).rstrip(").,;")

    snapshot_match = re.search(r"snapshot\s*:\s*(.+)", stripped, re.IGNORECASE)
    if snapshot_match:
        return snapshot_match.group(1).strip()

    if "http://" in stripped.lower() or "https://" in stripped.lower():
        # Jenkins job URLs (e.g. .../Restore-Eatlon-SnapShot/) contain the
        # word "snapshot" in the job name but are links, not patch names.
        return None

    generic_snapshot = re.search(
        r"\b[\w.-]+(?:/[\w.-]+)*snapshot[\w./-]*\b",
        stripped,
        re.IGNORECASE,
    )
    if generic_snapshot:
        return generic_snapshot.group(0)

    return None


def collect_patch_references(lines):
    patches = []
    seen = set()

    for line in lines:
        patch = extract_patch_reference(line)
        if patch and patch not in seen:
            seen.add(patch)
            patches.append(patch)

    return patches


def clear_cell(cell):
    try:
        cell.Hyperlinks.Delete()
    except Exception:
        pass
    cell.Value = ""


def get_patch_status_columns(ws):
    columns = []

    for col in range(PATCH_STATUS_FIRST_COL, PATCH_STATUS_LAST_COL + 1):
        if ws.Cells(4, col).Value:
            columns.append(col)

    return columns


def get_patch_status_columns_with_headers(ws):
    columns = []

    for col in range(PATCH_STATUS_FIRST_COL, PATCH_STATUS_LAST_COL + 1):
        header = ws.Cells(4, col).Value
        if header:
            columns.append((col, str(header)))

    return columns


def get_status_formats(ws):
    template_cells = {
        "Passed": "L54",
        "On Hold": "M54",
        "In Progress": "N54",
        "Skipped": "O54",
        "Blocked": "P54",
        "Failed": "Q54",
    }
    formats = {}

    for status, address in template_cells.items():
        source = ws.Range(address)
        formats[status] = {
            "interior_color": source.Interior.Color,
            "font_color": source.Font.Color,
            "bold": source.Font.Bold,
            "horizontal_alignment": source.HorizontalAlignment,
            "vertical_alignment": source.VerticalAlignment,
        }

    return formats


def write_status_cell(cell, status, status_formats):
    if not status_formats:
        cell.Value = status
        return

    cell_format = status_formats.get(status) or status_formats["In Progress"]
    cell.Value = status
    cell.Interior.Color = cell_format["interior_color"]
    cell.Font.Color = cell_format["font_color"]
    cell.Font.Bold = cell_format["bold"]
    cell.HorizontalAlignment = cell_format["horizontal_alignment"]
    cell.VerticalAlignment = cell_format["vertical_alignment"]


def mark_passed_row(ws, row, status_columns, status_formats):
    for col in status_columns:
        write_status_cell(ws.Cells(row, col), "Passed", status_formats)


def mark_blocked_row(ws, row, status_columns, status_formats):
    for col in status_columns:
        write_status_cell(ws.Cells(row, col), "Blocked", status_formats)


def mark_on_hold_row(ws, row, status_columns, status_formats):
    for col in status_columns:
        write_status_cell(ws.Cells(row, col), "On Hold", status_formats)


def find_matching_column_key(target_key, keys):
    """Match a patch name (from Daily_Envs.txt) to a column header key even
    when they're not byte-for-byte identical - typos, an extra word, etc.
    Tries exact match first, then containment, then a fuzzy near-match."""
    if not target_key:
        return None

    if target_key in keys:
        return target_key

    if len(target_key) >= 4:
        candidates = [key for key in keys if target_key in key or key in target_key]
        if len(candidates) == 1:
            return candidates[0]

    close = difflib.get_close_matches(target_key, keys, n=1, cutoff=0.82)
    return close[0] if close else None


def mark_failed_row(ws, row, columns_with_headers, status_formats, failed_patch_name):
    """Mark a row Failed straight from Daily_Envs.txt, no Jenkins check
    needed: everything before the failed patch is Passed, the failed patch
    itself is Failed, everything after is Blocked. Sanity is Blocked and
    Overall Status is Failed, same as the Jenkins-driven path would set."""
    sanity_key = normalize_excel_key("Sanity")
    overall_keys = {
        normalize_excel_key("Overall Status"),
        normalize_excel_key("Overal Status"),
    }
    target_key = normalize_excel_key(failed_patch_name) if failed_patch_name else None

    patch_columns = [
        (col, key)
        for col, header in columns_with_headers
        for key in [normalize_excel_key(header)]
        if key != sanity_key and key not in overall_keys
    ]

    matched_key = find_matching_column_key(
        target_key, [key for _, key in patch_columns]
    )

    failed_found = False
    for col, key in patch_columns:
        if failed_found:
            status = "Blocked"
        elif matched_key and key == matched_key:
            status = "Failed"
            failed_found = True
        else:
            status = "Passed"
        write_status_cell(ws.Cells(row, col), status, status_formats)

    if not failed_found:
        # Couldn't match the failed patch name from Daily_Envs.txt to a
        # column header - don't guess which ones passed, mark them all
        # Blocked instead.
        for col, _ in patch_columns:
            write_status_cell(ws.Cells(row, col), "Blocked", status_formats)

    for col, header in columns_with_headers:
        key = normalize_excel_key(header)
        if key == sanity_key:
            write_status_cell(ws.Cells(row, col), "Blocked", status_formats)
        elif key in overall_keys:
            write_status_cell(ws.Cells(row, col), "Failed", status_formats)


def insert_signature(selection):
    selection.TypeText("Thank you & Best regards,")
    selection.TypeParagraph()
    selection.TypeText("-----------------------------")
    selection.TypeParagraph()
    selection.TypeText("Alex Rivera, QA Engineer")
    selection.TypeParagraph()
    selection.Font.Bold = True
    selection.TypeText("Example Corp")
    selection.Font.Bold = False
    selection.TypeParagraph()
    selection.TypeParagraph()
    selection.TypeText("+00 0000000000 | Mobile")
    selection.TypeParagraph()


def get_open_mail_for_reply(outlook):
    warned = False

    while True:
        inspector = outlook.ActiveInspector()
        current_item = inspector.CurrentItem if inspector is not None else None

        if current_item is not None:
            return current_item

        if not warned:
            print("Abre el mail correcto en Outlook... esperando.")
            warned = True

        time.sleep(2)


def create_reply(target_mail):
    try:
        return target_mail.ReplyAll()
    except Exception as e:
        print("No se pudo crear ReplyAll. Intentando Reply normal...")
        print(e)

    try:
        return target_mail.Reply()
    except Exception as e:
        print("No se pudo crear Reply en Outlook.")
        print(e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Jenkins (SDCI) patch-status checking, merged in from the old test.py
# ---------------------------------------------------------------------------


def extract_deploy_urls(text):
    urls = re.findall(r"https://[\w.-]+\.example\.com/[^\s\)]+", text)
    clean_urls = []
    seen = set()

    for url in urls:
        url = url.rstrip(".,;")
        if url not in seen:
            seen.add(url)
            clean_urls.append(url)

    return clean_urls


def extract_env_label(url):
    match = re.search(r"ntt([a-zA-Z]*)(\d+)_qa", url, re.IGNORECASE)
    if not match:
        return "UNKNOWN"
    return format_env_key(match.group(1), match.group(2))


# Evita que varias pestañas intenten loguearse al mismo tiempo y se pisen.
_login_lock = asyncio.Lock()


async def ensure_logged_in(page):
    if await page.locator('input[name="j_username"]').count() == 0:
        return

    async with _login_lock:
        if await page.locator('input[name="j_username"]').count() == 0:
            return

        print("Logging in to SDCI...")
        await page.fill('input[name="j_username"]', USERNAME)
        await page.fill('input[name="j_password"]', PASSWORD)
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle", timeout=60000)

        # Si el formulario sigue ahí, SDCI no aceptó las credenciales.
        still_on_form = await page.locator('input[name="j_username"]').count() > 0
        if still_on_form or "loginError" in page.url:
            raise LoginError("jenkins", "SDCI no aceptó usuario/contraseña")


async def is_deployment_in_progress(page):
    active_progress_selector = "div.progress-bar.progress-bar-striped.active[role='progressbar']"
    active_progress = page.locator(active_progress_selector)

    try:
        await page.wait_for_selector(active_progress_selector, timeout=3000)
        return await active_progress.count() > 0
    except Exception:
        return False


async def get_pipeline_stages(page, deploy_url):
    base_url = deploy_url.rstrip("/")
    build_url = f"{base_url}/lastBuild/"
    api_url = f"{build_url}wfapi/describe"

    await page.goto(build_url, timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await ensure_logged_in(page)

    deployment_in_progress = await is_deployment_in_progress(page)
    data = await fetch_json_from_browser(page, api_url)
    return data.get("stages", []), deployment_in_progress


def split_stage_statuses(stages, deployment_in_progress=False):
    passed = []
    failed = []
    other = []
    active_or_pending_started = False
    running_statuses = {"IN_PROGRESS", "RUNNING", "PAUSED_PENDING_INPUT", "UNKNOWN"}
    pending_statuses = {"SKIPPED", "NOT_EXECUTED"}
    has_reported_running_stage = any(
        (stage.get("status") or "UNKNOWN").upper() in running_statuses
        for stage in stages
    )

    for stage in stages:
        name = stage.get("name", "UNKNOWN")
        status = (stage.get("status") or "UNKNOWN").upper()

        if status == "SUCCESS":
            passed.append(name)
        elif status == "ABORTED":
            # A parallel EAST/WEST branch that wasn't chosen shows up as ABORTED,
            # not a real failure.
            other.append((name, "ABORTED"))
        elif status in {"FAILED", "FAILURE"}:
            failed.append(name)
        elif deployment_in_progress and status in running_statuses:
            active_or_pending_started = True
            other.append((name, "IN_PROGRESS"))
        elif (
            deployment_in_progress
            and status in pending_statuses
            and (active_or_pending_started or not has_reported_running_stage)
        ):
            # When Jenkins still shows an active progress bar, pending cells after
            # the current point are future work, not truly skipped work.
            active_or_pending_started = True
            other.append((name, "IN_PROGRESS"))
        else:
            other.append((name, status))

    return passed, failed, other


def normalize_stage_status(status):
    status = (status or "UNKNOWN").upper()

    if status == "SUCCESS":
        return "Passed"
    if status in {"FAILED", "FAILURE"}:
        return "Failed"
    if status in {"IN_PROGRESS", "RUNNING", "PAUSED_PENDING_INPUT", "UNKNOWN"}:
        return "In Progress"
    if status == "BLOCKED":
        return "Blocked"

    # ABORTED / SKIPPED / NOT_EXECUTED / anything else: no real failure or
    # progress reported for this stage, so treat it the same as "not started
    # yet" rather than a dead-end "Skipped" status.
    return "Passed"


def normalize_excel_key(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def normalize_stage_name(stage_name):
    return re.sub(r"\s+(EAST|WEST|SHARED)$", "", stage_name, flags=re.IGNORECASE).strip()


def combine_statuses(statuses):
    priority = {
        "Failed": 60,
        "Blocked": 50,
        "In Progress": 40,
        "On Hold": 30,
        "Passed": 20,
    }

    return max(statuses, key=lambda status: priority.get(status, 0))


def get_selected_daily_env_text(path):
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    batch_value = os.environ.get(BATCH_ENV_VAR)
    if not batch_value:
        return text

    batches = split_daily_env_batches(text)
    if not batches:
        return text

    try:
        batch_index = int(batch_value)
    except ValueError:
        return text

    if not 1 <= batch_index <= len(batches):
        return text

    return batches[batch_index - 1]


def parse_daily_env_statuses(path):
    env_statuses = {}
    current_status = None

    for raw_line in get_selected_daily_env_text(path).splitlines():
        header_status = match_status_header(raw_line)
        if header_status:
            current_status = header_status
            continue

        if not current_status or "Deploy-Parallel" not in raw_line:
            continue

        env_match = ENV_LABEL_RE.search(raw_line)
        if env_match:
            env = normalize_env_key(env_match.group(1))
            env_statuses[env] = current_status

    return env_statuses


def parse_patch_status_log(log_path):
    log_path = Path(log_path)
    results = {}
    current_env = None
    current_section = None

    for raw_line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()

        header_match = re.match(r"^([A-Za-z]*\d+QA)\s+-\s+https?://", line)
        if header_match:
            current_env = header_match.group(1)
            results.setdefault(current_env, {})
            current_section = None
            continue

        if line in {"PASSED:", "FAILED:", "OTHER:"}:
            current_section = line.rstrip(":")
            continue

        if not current_env or not current_section or not line.startswith("- "):
            continue

        item = line[2:].strip()
        if item == "N/A":
            continue

        if current_section == "PASSED":
            stage_name = item
            status = "Passed"
        elif current_section == "FAILED":
            stage_name = item
            status = "Failed"
        else:
            other_match = re.match(r"^(.*?)\s+\((.*?)\)$", item)
            if other_match:
                stage_name = other_match.group(1).strip()
                status = normalize_stage_status(other_match.group(2))
            else:
                stage_name = item
                status = "In Progress"

        base_stage_name = normalize_stage_name(stage_name)
        results[current_env].setdefault(base_stage_name, []).append(status)

    return results


def get_excel_app():
    try:
        return win32.GetActiveObject("Excel.Application")
    except Exception:
        return win32.Dispatch("Excel.Application")


def find_workbook(excel, excel_path):
    target_path = str(Path(excel_path)).lower()

    for book in excel.Workbooks:
        if book.FullName.lower() == target_path:
            return book, False

    return excel.Workbooks.Open(excel_path, ReadOnly=False), True


def parse_daily_env_rows(path):
    envs = []
    current_status = None

    for line in get_selected_daily_env_text(path).splitlines():
        header_status = match_status_header(line)
        if header_status:
            current_status = header_status
            continue

        if not ENV_LABEL_RE.search(line) or "(" not in line:
            continue

        details = extract_env_details(line)
        if not details:
            continue

        envs.append(
            (
                details["num"],
                details["env"],
                details["version"],
                current_status,
            )
        )

    env_dict = {(env, version): (num, env, version, status) for num, env, version, status in envs}
    return sorted(env_dict.values(), key=lambda x: ([-i for i in version_key(x[2])], x[0]))


def update_excel_env_rows(ws):
    envs = parse_daily_env_rows(DAILY_ENVS_PATH)
    if not envs:
        return 0

    row = 5
    while ws.Cells(row, 7).Value:
        ws.Cells(row, 7).Value = ""
        row += 1

    row = 5
    for _, env, version, status in envs:
        ws.Cells(row, 7).Value = f"{env} ({version})"
        row += 1

    return row - 1


def update_excel_with_patch_results(log_path):
    results = parse_patch_status_log(log_path)
    if not results:
        print("No patch results found in log. Excel was not updated.")
        return

    deploy_parallel_statuses = parse_daily_env_statuses(DAILY_ENVS_PATH)
    excel = get_excel_app()
    excel.Visible = True
    previous_screen_updating = excel.ScreenUpdating
    previous_display_alerts = excel.DisplayAlerts
    excel.ScreenUpdating = False
    excel.DisplayAlerts = False
    wb, opened_by_script = find_workbook(excel, EXCEL_PATH)

    try:
        ws = wb.Worksheets(SHEET_NAME)
        status_formats = get_status_formats(ws)
        last_env_row = update_excel_env_rows(ws)

        header_row = 4
        first_data_row = 5
        env_col = 7
        first_patch_col = 8
        last_col = ws.UsedRange.Columns.Count
        last_row = max(ws.UsedRange.Rows.Count, last_env_row)

        stage_columns = {}
        stage_order = []
        for col in range(first_patch_col, last_col + 1):
            if col == PATCH_NAME_COL:
                continue

            header = ws.Cells(header_row, col).Value
            if header:
                key = normalize_excel_key(header)
                stage_columns[key] = col
                stage_order.append((key, str(header)))

        env_rows = {}
        for row in range(first_data_row, last_row + 1):
            env_text = ws.Cells(row, env_col).Text
            env_match = re.search(r"\b([A-Za-z]*\d+QA)\b", env_text or "")
            if env_match:
                env_rows[normalize_env_key(env_match.group(1))] = row

        updated = 0
        missing = []

        sanity_key = normalize_excel_key("Sanity")
        overall_keys = {
            normalize_excel_key("Overall Status"),
            normalize_excel_key("Overal Status"),
        }
        overall_col = next(
            (stage_columns[key] for key in overall_keys if key in stage_columns),
            42,
        )

        patch_order = [
            (key, header)
            for key, header in stage_order
            if key != sanity_key and key not in overall_keys
        ]

        for env, stages in results.items():
            row = env_rows.get(env)
            if not row:
                missing.append(f"{env}: row not found")
                continue

            combined_stages = {
                normalize_excel_key(stage_name): combine_statuses(statuses)
                for stage_name, statuses in stages.items()
            }
            combined_stage_keys = list(combined_stages.keys())

            failed_found = False
            failed_env = False
            in_progress_found = False
            in_progress_env = False
            written_patch_statuses = []

            planned = []  # [col, status, confirmed_passed]

            for stage_key, header in patch_order:
                col = stage_columns.get(stage_key)
                if not col:
                    continue

                confirmed_passed = False
                if failed_found:
                    status = "Blocked"
                elif in_progress_found:
                    status = "In Progress"
                else:
                    # Match Jenkins' stage name to this Excel column even if
                    # they're not identical (extra word, different EAST/WEST
                    # suffix, etc.) - same idea as the Failed-patch matching.
                    matched_key = find_matching_column_key(stage_key, combined_stage_keys)
                    status = combined_stages.get(matched_key) if matched_key else None
                    # No column-name match at all, or a real "Skipped"/aborted
                    # branch: nothing has failed or is in progress yet, so it
                    # must be Passed - a patch column is never left "Skipped".
                    if not status or status == "Skipped":
                        status = "Passed"
                    else:
                        confirmed_passed = status == "Passed"
                    if status == "Failed":
                        failed_found = True
                        failed_env = True
                    elif status == "In Progress":
                        in_progress_found = True
                        in_progress_env = True

                planned.append([col, status, confirmed_passed])

            # Every env in the log is In Progress in Daily_Envs.txt. If Jenkins
            # didn't flag any stage as running (empty stage list, build between
            # stages, ...), nothing past the last confirmed Passed can be Passed:
            # that spot is where the deployment is, and everything to its right
            # is In Progress too.
            if planned and not failed_found and not in_progress_found:
                last_confirmed = max(
                    (i for i, item in enumerate(planned) if item[2]), default=-1
                )
                if last_confirmed + 1 < len(planned):
                    for item in planned[last_confirmed + 1:]:
                        item[1] = "In Progress"
                    in_progress_found = True
                    in_progress_env = True

            for col, status, _ in planned:
                write_status_cell(ws.Cells(row, col), status, status_formats)
                written_patch_statuses.append(status)
                updated += 1

            for stage_name in stages:
                stage_key = normalize_excel_key(stage_name)
                if stage_key not in stage_columns:
                    missing.append(f"{env}: stage not found - {stage_name}")

            if failed_env:
                sanity_status = "Blocked"
            elif in_progress_env:
                sanity_status = "On Hold"
            elif written_patch_statuses and all(
                status == "Passed" for status in written_patch_statuses
            ):
                sanity_status = "Passed"
            elif written_patch_statuses:
                sanity_status = combine_statuses(written_patch_statuses)
            else:
                sanity_status = None

            if failed_env:
                overall_status = "Failed"
            elif in_progress_env:
                overall_status = "In Progress"
            else:
                overall_status = deploy_parallel_statuses.get(env)
                if not overall_status:
                    if written_patch_statuses and all(
                        status == "Passed" for status in written_patch_statuses
                    ):
                        overall_status = "Passed"
                    elif written_patch_statuses:
                        overall_status = combine_statuses(written_patch_statuses)

            if overall_status == "In Progress" and sanity_status != "Blocked":
                # Sanity waits (On Hold) for as long as the env is In Progress.
                sanity_status = "On Hold"

            if sanity_status and sanity_key in stage_columns:
                write_status_cell(ws.Cells(row, stage_columns[sanity_key]), sanity_status, status_formats)
                updated += 1

            if overall_status:
                write_status_cell(ws.Cells(row, overall_col), overall_status, status_formats)
                updated += 1

        wb.Save()
    finally:
        excel.ScreenUpdating = previous_screen_updating
        excel.DisplayAlerts = previous_display_alerts
        if opened_by_script:
            wb.Close(SaveChanges=True)

    print(f"Excel updated: {updated} patch cells.")
    if missing:
        print("Skipped items:")
        for item in missing:
            print(f"  - {item}")


def format_section(title, items):
    lines = [f"{title}:"]

    if not items:
        lines.append("  N/A")
        return lines

    for item in items:
        lines.append(f"  - {item}")

    return lines


def format_result(env_label, url, passed, failed, other=None, error=None):
    lines = [
        "=" * 80,
        f"{env_label} - {url}",
    ]

    if error:
        lines.append(f"ERROR: {error}")
        return lines

    lines.extend(format_section("PASSED", passed))
    lines.extend(format_section("FAILED", failed))

    if other:
        lines.append("OTHER:")
        for name, status in other:
            lines.append(f"  - {name} ({status})")

    return lines


async def check_jenkins_and_update_excel(checkable_envs):
    if not DAILY_ENVS_PATH.exists():
        raise FileNotFoundError(f"File not found: {DAILY_ENVS_PATH}")

    text = get_selected_daily_env_text(DAILY_ENVS_PATH)
    urls = [
        url
        for url in extract_deploy_urls(text)
        if extract_env_label(url) in checkable_envs
    ]

    if not urls:
        print(f"No hay envs In Progress/Failed con links de SDCI en {DAILY_ENVS_PATH}")
        return

    log_lines = [
        f"Patch status report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Source: {DAILY_ENVS_PATH}",
        f"Links found: {len(urls)}",
        "",
    ]

    print(f"Checking {len(urls)} SDCI links...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await new_context_with_session(browser, saved_session())

        # Login una sola vez; las demás pestañas heredan la cookie. Con la
        # sesión guardada (la mantiene viva Track_Deployments) ni hace falta.
        warmup = await context.new_page()
        await warmup.goto(urls[0].rstrip("/") + "/", timeout=60000)
        await warmup.wait_for_load_state("domcontentloaded")
        await ensure_logged_in(warmup)
        await persist_session(context)
        await warmup.close()

        semaphore = asyncio.Semaphore(CONCURRENCY)
        print_lock = asyncio.Lock()
        done = 0

        async def check(url):
            nonlocal done
            env_label = extract_env_label(url)

            async with semaphore:
                page = await context.new_page()
                try:
                    stages, deployment_in_progress = await get_pipeline_stages(page, url)
                    passed, failed, other = split_stage_statuses(stages, deployment_in_progress)
                    result_lines = format_result(env_label, url, passed, failed, other)
                except LoginError:
                    raise
                except Exception as exc:
                    result_lines = format_result(env_label, url, [], [], error=str(exc))
                finally:
                    await page.close()

            async with print_lock:
                done += 1
                print(f"[{done}/{len(urls)}] Checked {env_label}")

            return result_lines

        # gather respeta el orden de 'urls', así el log queda igual que antes.
        all_results = await asyncio.gather(*(check(url) for url in urls))

        for result_lines in all_results:
            log_lines.extend(result_lines)
            log_lines.append("")

        await browser.close()

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(log_lines), encoding="utf-8")

    print()
    print("\n".join(log_lines))
    print(f"Log saved to: {LOG_PATH}")
    update_excel_with_patch_results(LOG_PATH)


# ---------------------------------------------------------------------------
# Main script
# ---------------------------------------------------------------------------


def main():
    """Elige el batch, revisa Jenkins, llena el Excel y arma el correo en Outlook."""
    with open(txt_path, "r", encoding="utf-8") as f:
        daily_envs_text = f.read()

    selected_batch_index, selected_batch_lines = choose_daily_env_batch(daily_envs_text)
    os.environ[BATCH_ENV_VAR] = str(selected_batch_index)

    checkable_envs = get_checkable_envs(selected_batch_lines)
    if checkable_envs:
        try:
            asyncio.run(check_jenkins_and_update_excel(checkable_envs))
        except LoginError as login_error:
            # Sin Jenkins no hay datos de patches: se corta aqui para no escribir
            # informacion incorrecta en el Excel ni mandar un correo equivocado.
            print(login_error)
            print("\nNo se actualizo el Excel ni se genero el correo.")
            sys.exit(1)
    else:
        print("No hay builds IN PROGRESS/FAILED; se omite revision de Jenkins.")

    lines = [line + "\n" for line in selected_batch_lines]

    envs = []
    cas_created = []
    failed_details = {}
    patches_installed = collect_patch_references(lines)
    current_status = None

    for line in lines:
        header_status = match_status_header(line)
        if header_status:
            current_status = header_status
            continue

        if ENV_LABEL_RE.search(line) and "(" in line:
            try:
                details = extract_env_details(line)
                if not details:
                    continue

                if current_status == "Failed":
                    failed = extract_failed_details(line)
                    if failed:
                        failed_details[
                            deployment_key(failed["env"], failed["version"])
                        ] = failed

                if details["east"] or details["west"]:
                    cas_created.append(
                        (
                            details["num"],
                            details["env"],
                            details["version"],
                            details["link"],
                            details["east"],
                            details["west"],
                        )
                    )

                envs.append(
                    (
                        details["num"],
                        details["env"],
                        details["version"],
                        current_status,
                    )
                )
            except Exception:
                pass

    env_dict = {
        deployment_key(env, version): (num, env, version, status)
        for num, env, version, status in envs
    }
    envs = list(env_dict.values())
    envs = sorted(envs, key=lambda x: ([-i for i in version_key(x[2])], x[0]))
    cas_dict = {
        deployment_key(env, version): (num, env, version, link, east, west)
        for num, env, version, link, east, west in cas_created
    }
    cas_created = sorted(cas_dict.values(), key=lambda item: item[0])

    if not envs:
        print("No se detecto nada")
        sys.exit(1)

    excel = win32.Dispatch("Excel.Application")
    excel.Visible = True

    wb = None
    for book in excel.Workbooks:
        if book.FullName.lower() == excel_path.lower():
            wb = book
            break

    if wb is None:
        wb = excel.Workbooks.Open(excel_path, ReadOnly=False)

    ws = wb.Worksheets(sheet_name)
    status_formats = get_status_formats(ws)
    patch_status_columns = get_patch_status_columns(ws)
    patch_status_columns_with_headers = get_patch_status_columns_with_headers(ws)

    row = 5
    while ws.Cells(row, 7).Value:
        clear_cell(ws.Cells(row, 4))
        clear_cell(ws.Cells(row, 5))
        clear_cell(ws.Cells(row, 7))
        row += 1

    row = 5
    for _, env, version, status in envs:
        failed = failed_details.get(deployment_key(env, version))
        if failed:
            if failed["ticket_url"] and failed["ticket"]:
                ticket_cell = ws.Cells(row, 4)
                ticket_cell.Value = failed["ticket"]
                ws.Hyperlinks.Add(
                    Anchor=ticket_cell,
                    Address=failed["ticket_url"],
                    TextToDisplay=failed["ticket"],
                )
                ticket_cell.HorizontalAlignment = xlHAlignCenter
            ws.Cells(row, 5).Value = failed["patch"]

        ws.Cells(row, 7).Value = f"{env} ({version})"
        if status == "Passed":
            mark_passed_row(ws, row, patch_status_columns, status_formats)
        elif status == "Failed":
            mark_failed_row(
                ws,
                row,
                patch_status_columns_with_headers,
                status_formats,
                failed["patch"] if failed else None,
            )
        elif status == "Blocked":
            mark_blocked_row(ws, row, patch_status_columns, status_formats)
        elif status == "On Hold":
            mark_on_hold_row(ws, row, patch_status_columns, status_formats)
        row += 1

    last_row = row - 1
    wb.Save()

    print(f"Excel actualizado hasta fila {last_row}")

    all_passed = all(status == "Passed" for _, _, _, status in envs)
    if all_passed:
        print("Todos los envs estan Passed, se continua automaticamente.")
        confirm = "y"
    else:
        confirm = input("Todo correcto? (y/n): ").strip().lower()

    if confirm != "y":
        sys.exit(0)

    outlook = win32.Dispatch("Outlook.Application")
    target_mail = get_open_mail_for_reply(outlook)

    reply = create_reply(target_mail)
    reply.Display()

    inspector = reply.GetInspector
    doc = inspector.WordEditor
    selection = doc.Application.Selection

    selection.HomeKey(wdStory)
    selection.TypeText("Hi team, please find the current status on today's deployments:")
    selection.TypeParagraph()
    selection.TypeParagraph()

    tables_before_ticket = doc.Tables.Count
    ws.Range(f"B4:E{last_row}").Copy()
    selection.Paste()

    if doc.Tables.Count > tables_before_ticket:
        # Column D (ticket link) pastes left-aligned and blue by default -
        # force it centered with white text, skipping the header row.
        # One cell at a time: a Range from cell (2,3) to cell (N,3) is one
        # continuous stretch of text, so it would also whiten every cell
        # that sits between them (e.g. the "Deployment Status" column).
        ticket_table = doc.Tables(doc.Tables.Count)
        for table_row in range(2, ticket_table.Rows.Count + 1):
            ticket_cell_range = ticket_table.Cell(table_row, 3).Range
            ticket_cell_range.Font.Color = wdColorWhite
            ticket_cell_range.ParagraphFormat.Alignment = wdAlignParagraphCenter

    selection.TypeParagraph()

    ws.Range(f"G4:AQ{last_row}").Copy()
    selection.Paste()
    selection.TypeParagraph()

    selection.Font.Bold = True
    selection.TypeText("CAs Created:" if cas_created else "No CAs Created")
    selection.Font.Bold = False
    selection.TypeParagraph()

    for _, env, version, link, east, west in cas_created:
        env_start = selection.Range.Start
        selection.TypeText(env)
        env_end = selection.Range.End

        env_range = doc.Range(Start=env_start, End=env_end)
        if link:
            doc.Hyperlinks.Add(Anchor=env_range, Address=link, TextToDisplay=env)
        env_range.Font.Bold = True

        if version:
            selection.TypeText(f" ({version})")
        if east:
            selection.TypeText(f" EAST: {east}")
        if west:
            selection.TypeText(f" WEST: {west}")

        selection.Font.Bold = False
        selection.TypeParagraph()

    selection.Font.Bold = True
    selection.TypeParagraph()
    selection.TypeText("Patches installed:")
    selection.Font.Bold = False
    selection.TypeParagraph()

    if patches_installed:
        for patch in patches_installed:
            selection.TypeText(patch)
            selection.TypeParagraph()
    else:
        selection.TypeText("N/A")
        selection.TypeParagraph()

    selection.TypeParagraph()
    insert_signature(selection)

    trigger_webex_update(txt_path)
    remove_batch_from_daily_envs(txt_path, selected_batch_index)

    print("Mail listo con CAs alineados, headings en negritas y menos espacios.")


if __name__ == "__main__":
    main()
