"""Excel Screenshots — the loader-file half of PIM/AM Validation.

PIM_Validation.py and AM_Validation.py screenshot what TOMS shows. This
one screenshots what the LOADERS say, so both halves end up in the same
report and can be compared side by side.

Paste the Server Details (with the TMS ticket links) and, for every
ticket, this:

  1. reads the device/offering list out of the ticket description
     (same reader PIM and AM use);
  2. opens every "NC.GCI.Polaris*.xlsx" in Downloads — the Wireless
     Equipment, Equipment and Delivery&Return and DSG.AM.Configuration
     loaders. Already-open workbooks are reused, not reopened;
  3. per product, per workbook, per sheet: finds the rows that belong to
     that product, selects them, folds away the columns those rows leave
     empty, and photographs what's left. Every shot therefore carries a
     screenful of real values, blank stretches are never photographed,
     and a sheet where the product isn't mentioned gets no screenshot at
     all;
  4. appends it all to the END of Downloads\\<TICKET>.docx — the SAME
     file AM and PIM write, so running this after them just adds to the
     report instead of starting a new one;
  5. asks whether the reports look right, and on a yes attaches each one
     to its ticket, posts the Tested And Passed comment (with the report
     tagged as [^<TICKET>.docx]) and moves the ticket to Passed — from
     Open it goes through In Progress first. On a no, nothing is sent.

The horizontal walk relies on the loaders' frozen panes: the identity
columns stay on screen while the rest scrolls, which is exactly how the
shots are taken by hand. If a sheet has no frozen panes, one is made for
the run and undone afterwards.

Nothing is ever written to the .xlsx files — only the view is moved, and
the folded-away columns (and a freeze that had to be made) are put back
as they were, including when a run fails part way.
Nothing is sent to Jira until the question at the end is answered yes.
"""

import asyncio
import base64
import ctypes
import glob
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime

import pythoncom
import win32com.client
import win32gui
import win32ui
from PIL import Image
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import PIM_Validation as pim


# Only used to read the ticket description. A window is still handy for
# the TMS login, same as in the other two scripts.
HEADLESS = False

# Which files count as loaders. Everything the catalog team drops in
# Downloads starts like this, so a new loader is picked up on its own.
LOADER_GLOB = "NC.GCI.Polaris*.xlsx"

# Heading written above each product's loader screenshots — also what a
# resumed run looks for to know a product is done. Deliberately different
# from AM's " - Stock Item Model" so the two never confuse each other.
HEADING_SUFFIX = " - Loader Files"

# Matched rows this far apart are still shot together (a couple of
# foreign rows in the middle is cheaper than a second screenshot).
GROUP_GAP = 3

# A product that hits more rows than this in one sheet is almost always a
# too-short name ("IPHONE 18 PRO" matching every colour); shoot the first
# ones and say so instead of taking hundreds of screenshots. Generous on
# purpose — a real product legitimately owns a hundred-odd rows in the
# Offerings sheet, and dropping them silently would be worse.
MAX_MATCH_ROWS = 400

# Hard ceiling per sheet, so one runaway product can't fill a report.
MAX_SHOTS_PER_SHEET = 60

# Columns these rows leave empty are folded away before shooting, so the
# values close up and one picture carries as many of them as fit instead
# of one picture per handful. Only the view changes, and only while the
# group is being shot. Set to False to shoot the sheet as it lies.
HIDE_EMPTY_COLUMNS = True

# How many columns to hide per call to Excel.
HIDE_CHUNK = 25

# Repeat one row between consecutive shots so nothing falls in the crack
# between two screenshots. (Columns don't need this: each shot starts on
# a column that still has something to show.)
OVERLAP_ROWS = 1

# Rows shown above the match, so it isn't stuck to the column headers.
CONTEXT_ROWS = 2

# The loaders have formatting out to silly column letters, so the value
# scan is capped and the "is there still colour?" scan only peeks a
# little past the last value.
MAX_SCAN_COLUMNS = 1024
FILL_LOOKAHEAD = 40
FILL_MISSES = 3

# Safety valves for the scrolling loops.
MAX_PASSES = 60

# Time Excel needs to actually repaint after a scroll before the pixels
# are worth grabbing. Measured at ~0.05s on these loaders; kept at three
# times that, and a shot that comes out identical to the previous one is
# taken again after RESETTLE_DELAY rather than believed.
PAINT_DELAY = 0.15
RESETTLE_DELAY = 0.6

# The ribbon is just noise in the report — hidden for the run, put back
# at the end. It also buys a few more rows per screenshot.
HIDE_RIBBON = True

# A left-over AutoFilter hides the very rows we're looking for, so it's
# cleared (the data isn't touched).
CLEAR_FILTERS = True

# Everything above the grid (title bar, ribbon, formula bar, the "enable
# content" warning) is cut off — the report wants the cells. The cut is
# worked out from where Excel says the grid starts; this is how much of
# the column-letter strip to keep above it, at 100% scaling.
HEADER_STRIP_PX = 19

# Cut off the bottom too, if the status bar ever gets in the way.
CUT_BOTTOM = 0

# Excel enums, spelled out so the calls below read like the macros they
# mirror.
XL_VALUES = -4163
XL_FORMULAS = -4123
XL_PART = 2
XL_BY_ROWS = 1
XL_BY_COLUMNS = 2
XL_NEXT = 1
XL_PREVIOUS = 2
XL_NONE = -4142
XL_MAXIMIZED = -4137

# What Find looks at. xlFormulas reads what is stored in the cell,
# xlValues reads what is displayed — and on these loaders xlFormulas is
# four to eight times faster (the Wireless Offerings sheet: 1.7s against
# 13.2s). They only disagree on cells holding a formula, and the loaders
# are flat exported text, so this is the fast one. If a loader ever
# starts computing its names, put this back to XL_VALUES.
SEARCH_IN = XL_FORMULAS

MAX_FIND_STEPS = 4000

# What goes on the ticket once the reports are approved.
JIRA_BASE = "https://jira.example.com"

COMMENT_HEADLINE = "Tested And Passed"

# The Pass screen offers these three — % Passed (customfield_19500),
# % Executed (customfield_19501) and Log Work — and none of them are
# required, so they'd be left empty unless filled on purpose. Same
# numbers that get typed in by hand.
PERCENT_PASSED = 100
PERCENT_EXECUTED = 100
TIME_SPENT = "1h"

# Where every ticket should end up, and the only two places it is moved
# from. An Open ticket has to go through In Progress to get there; a
# ticket sitting anywhere else (Failed, Blocked, Cancelled...) is a
# decision somebody already made and is left alone.
GOAL_STATUS = "Passed"
MOVABLE_FROM = ("open", "in progress")

# A ticket needs at most Open -> In Progress -> Passed; the cap only
# stops a workflow that behaves unexpectedly from looping.
MAX_TRANSITIONS = 3

# Excel reports "busy right now" (a cell in edit mode, a dialog up) by
# refusing the COM call outright; that's worth a retry, unlike a real
# error.
EXCEL_BUSY_ATTEMPTS = 6
EXCEL_BUSY_DELAY = 3


def _make_dpi_aware():
    """Window rectangles only come back in physical pixels when the
    process is DPI-aware — without this the crop is wrong on a scaled
    display.

    Deliberately NOT done at import time: when PIM_Validation hands its
    finished tickets over, this module gets imported into the middle of
    a run that steers TOMS with fixed-coordinate pyautogui clicks, and
    changing the process's DPI awareness under it would move them. By
    the time the Excel pass starts, that clicking is over."""

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# -------------------------
# INPUT
# -------------------------

# Lines that turn up right under a product list when Jira swallows the
# blank line after it, and are instructions rather than products.
PROSE_START_RE = re.compile(
    r"^(validate|refer|note|also|please|check|verify|ensure|compare)\b",
    re.I
)


def _is_not_a_product(line):
    """True for a line the list reader picked up that is obviously prose:
    an instruction, a step number, a heading ending in a colon, or a
    mention of a file."""

    if pim.is_offering_url(line):
        return False

    if PROSE_START_RE.match(line):
        return True

    if pim.NUMBERED_STEP_RE.match(line) or line.endswith(":"):
        return True

    if "://" in line:
        return True

    lowered = line.lower()

    return ".xlsx" in lowered or ".xlsm" in lowered


def extract_products(description):
    """The products listed in the ticket — the same list PIM works from.

    PIM's own reader decides where the list starts and stops, so what it
    finds here is exactly what it found there. This used to push the list
    through AM's name-shape test as well (a capacity like "256GB" or a
    short prefix like "CPO"), which silently dropped real products that
    don't look like a phone: "FRANKLIN WIRELESS MIFI RG2100 5G" has
    neither, and the pass came back asking for it by hand.

    All that's removed is lines that are plainly instructions, for the
    case where Jira drops the blank line that normally ends the list."""

    found = pim.extract_offerings_from_description(description)

    return [d for d in found if d and not _is_not_a_product(d)]


def resume_skip_count(existing_text, products):
    """How many products, from the start, a resumed run can skip.

    Safe to take at face value here, unlike in PIM/AM: a product's
    heading is only written once all its screenshots are taken, so a run
    that died mid-product left no heading and that product is redone
    whole. The suffix is this script's own, so AM's and PIM's headings in
    the same report are never mistaken for loader work."""

    skip = 0

    for product in products:
        if f"{product}{HEADING_SUFFIX}" in existing_text:
            skip += 1
        else:
            break

    return skip


# What SVN calls a loader it hands you from the repository: opening one
# from the repo browser saves it as
# "<loader>.xlsx-revHEAD.svn001.tmp.xlsx". That IS the loader — HEAD is
# the latest revision, which is what the ticket says to validate against
# — so it's accepted, and treated as the same loader as a plainly named
# copy.
SVN_COPY_RE = re.compile(
    r"(\.xls[xm])-rev[^.]*\.svn\d+\.tmp\.xls[xm]$", re.I
)


def loader_identity(name):
    """The loader's own file name, with SVN's "-revHEAD.svn001.tmp"
    wrapping taken off — so every copy of the same loader maps to the
    same name."""

    return SVN_COPY_RE.sub(r"\1", name)


def find_loader_files():
    """The loaders sitting in Downloads: one file per loader.

    Excel's lock files (~$...) are not loaders and are skipped. When
    several copies of the same loader are there (a plain download and an
    SVN one, or two SVN copies), the most recently saved wins — and that
    choice is printed, so it's never a mystery which file was used."""

    pattern = os.path.join(pim.DOWNLOADS, LOADER_GLOB)

    newest = {}
    others = {}

    for path in glob.glob(pattern):

        name = os.path.basename(path)

        if name.startswith("~$"):
            continue

        key = loader_identity(name).lower()

        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue

        if key not in newest or stamp > newest[key][0]:
            if key in newest:
                others.setdefault(key, []).append(newest[key][1])
            newest[key] = (stamp, path)
        else:
            others.setdefault(key, []).append(path)

    for key, skipped in others.items():
        for path in skipped:
            print(
                f"   ⏭ {os.path.basename(path)} — an older copy of the "
                f"same loader; using {os.path.basename(newest[key][1])}."
            )

    return sorted(path for _stamp, path in newest.values())


def loader_label(path):
    """Short name for the report heading: the middle of the file name,
    without the "NC.GCI.Polaris." prefix, the extension, or SVN's
    revision wrapping."""

    name = os.path.splitext(loader_identity(os.path.basename(path)))[0]

    return re.sub(r"^NC\.GCI\.Polaris\.", "", name)


# -------------------------
# EXCEL
# -------------------------

# -------------------------
# WORD, ON ITS OWN THREAD
# -------------------------

class WordWriter:
    """Writes the report while Excel keeps working.

    Two reasons this is a thread and not just a few calls:

    1. Speed. Dropping fifty screenshots into a growing .docx is slow,
       and there's no reason Excel should sit still for it — by the time
       Word finishes one product, Excel has already shot the next.
    2. COM. PIM's Word helpers end with CoUninitialize(), which tears
       down the calling thread's COM apartment and disconnects every
       Excel object held there. On its own thread it only ever tears
       down its own apartment, so the Excel side stays alive.

    One worker, one queue: the report is written in exactly the order
    the work was handed over."""

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            item = self._queue.get()

            if item is None:
                self._queue.task_done()
                return

            fn, args, future = item

            try:
                result = fn(*args)
                if future is not None:
                    future.set_result(result)
            except Exception as e:
                if future is not None:
                    future.set_exception(e)
                else:
                    print(f"⚠ Word: {e}")
            finally:
                self._queue.task_done()

    def submit(self, fn, *args):
        """Hand Word some work and carry on."""
        self._queue.put((fn, args, None))

    def call(self, fn, *args):
        """Hand Word some work and wait for the answer."""
        future = Future()
        self._queue.put((fn, args, future))
        return future.result()

    def drain(self):
        """Wait until everything handed over so far is written."""
        self._queue.join()

    def stop(self):
        self._queue.put(None)
        self._thread.join(timeout=30)


def _excel_action(body, description):
    """Run one Excel/COM call, surviving an Excel that is momentarily
    refusing calls (a cell left in edit mode, a dialog up). Anything that
    keeps failing is raised like it always was."""

    last = None

    for attempt in range(EXCEL_BUSY_ATTEMPTS):

        try:
            return body()

        except Exception as e:
            last = e

            if attempt % 8 == 0:
                print(
                    f"⏳ Excel is refusing to {description} "
                    f"({e}). If a cell is being edited or a dialog is "
                    "open, close it — waiting..."
                )

            time.sleep(EXCEL_BUSY_DELAY)

    raise last


def get_excel():
    """The Excel the user already has open, or a new one."""

    pythoncom.CoInitialize()

    try:
        xl = win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        xl = win32com.client.Dispatch("Excel.Application")

    xl.Visible = True

    try:
        xl.WindowState = XL_MAXIMIZED
    except Exception:
        pass

    return xl


_announced = set()


def open_workbook(xl, path):
    """Reuse the workbook if it's already open, otherwise open it
    read-only — the loaders are huge and nothing here writes to them."""

    for index in range(1, int(xl.Workbooks.Count) + 1):

        try:
            wb = xl.Workbooks(index)
            full_name = wb.FullName
        except Exception:
            continue

        if full_name.lower() == path.lower():
            # Said once, not on every re-attach.
            if path not in _announced:
                _announced.add(path)
                print(f"📂 Already open: {os.path.basename(path)}")
            return wb

    _announced.add(path)

    print(f"📂 Opening (this one is big, give it a moment): "
          f"{os.path.basename(path)}")

    alerts = True

    try:
        alerts = xl.DisplayAlerts
        xl.DisplayAlerts = False
    except Exception:
        pass

    try:
        # Open(Filename, UpdateLinks, ReadOnly)
        return xl.Workbooks.Open(path, 0, True)
    finally:
        try:
            xl.DisplayAlerts = alerts
        except Exception:
            pass


def _set_ribbon(xl, shown):
    if not HIDE_RIBBON:
        return

    try:
        xl.ExecuteExcel4Macro(
            'SHOW.TOOLBAR("Ribbon",%s)' % ("True" if shown else "False")
        )
    except Exception:
        pass


def _client_box(hwnd):
    """(origin x, origin y, width, height) of the window's client area,
    in screen pixels."""

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    sx, sy = win32gui.ClientToScreen(hwnd, (left, top))

    return sx, sy, right - left, bottom - top


def _dpi_scale(hwnd):
    try:
        return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
    except Exception:
        return 1.0


def grid_top_offset(win, hwnd):
    """How much of the top of the window to throw away: everything above
    the column letters.

    Excel itself says where the grid starts (the top-left pane's first
    visible row), so this survives a shown or hidden ribbon, the "enable
    content" warning bar, and any display scaling."""

    try:
        grid_y = int(win.Panes(1).PointsToScreenPixelsY(0))
    except Exception:
        try:
            grid_y = int(win.PointsToScreenPixelsY(0))
        except Exception:
            return 0

    _sx, sy, _w, h = _client_box(hwnd)

    strip = int(round(HEADER_STRIP_PX * _dpi_scale(hwnd)))
    offset = grid_y - sy - strip

    if offset < 0 or offset > h - 80:
        return 0

    return offset


def _grab_excel_window(hwnd, top_offset=0):
    """One screenshot of Excel's grid, saved next to the other temp shots
    so the Word helpers can pick it up.

    Asks the window to draw itself (PrintWindow) instead of grabbing
    screen pixels: the shot is right even when Excel is behind the
    terminal, so the run never has to steal the screen — and nothing the
    user does in the meantime ends up in the report."""

    _sx, _sy, width, height = _client_box(hwnd)

    if width < 100 or height < 100:
        return None

    window_dc = mfc_dc = save_dc = bitmap = None

    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(window_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        # 3 = PW_CLIENTONLY | PW_RENDERFULLCONTENT — the second flag is
        # what makes Excel's hardware-drawn grid come out.
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)

        if not ok:
            return None

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)

        img = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits, "raw", "BGRX", 0, 1
        )

    except Exception as e:
        print(f"⚠ Couldn't grab the Excel window: {e}")
        return None

    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if window_dc is not None:
                win32gui.ReleaseDC(hwnd, window_dc)
        except Exception:
            pass

    bottom = img.height - CUT_BOTTOM

    if 0 <= top_offset < bottom:
        img = img.crop((0, top_offset, img.width, bottom))

    path = pim._temp_png()
    img.save(path)

    return path


def _settle(xl):
    try:
        xl.ScreenUpdating = True
    except Exception:
        pass

    time.sleep(PAINT_DELAY)


# -------------------------
# FINDING THE PRODUCT
# -------------------------

def _cell_matches(value, product):
    """True if this cell is about that product.

    A prefix match, not a "contains": "IPHONE DUO 512GB" has to sit at
    the START of the cell, so it catches "IPHONE DUO 512GB - Retail Price
    NRC" but never "CPO IPHONE DUO 512GB", which is a different product.
    What follows has to be a separator, so "…512GB" doesn't match
    "…512GBX"."""

    if value is None:
        return False

    text = " ".join(str(value).split()).upper()
    name = " ".join(product.split()).upper()

    if not text.startswith(name):
        return False

    rest = text[len(name):]

    return rest == "" or not rest[0].isalnum()


def _escape_for_find(product):
    """Excel's Find treats * ? ~ as wildcards."""

    return (
        product
        .replace("~", "~~")
        .replace("*", "~*")
        .replace("?", "~?")
    )


_bounds_cache = {}


def sheet_bounds(ws):
    """(last row, last column) worth searching.

    Not UsedRange: these loaders carry formatting down whole columns, so
    UsedRange claims all 1,048,576 rows on sheets whose data stops at
    3,097 — and searching that costs seconds per product. Asking Excel
    for the last cell that actually holds something takes a tenth of a
    second and cuts the search range by three hundred times.

    Cached: sheets don't grow while the run is going."""

    try:
        key = (ws.Parent.Name, ws.Name)
    except Exception:
        key = None

    if key in _bounds_cache:
        return _bounds_cache[key]

    last_row = 1
    last_col = MAX_SCAN_COLUMNS

    try:
        cell = ws.Cells.Find(
            "*", ws.Cells(1, 1), XL_FORMULAS, XL_PART,
            XL_BY_ROWS, XL_PREVIOUS, False
        )
        if cell is not None:
            last_row = int(cell.Row)
    except Exception:
        pass

    try:
        used = ws.UsedRange
        # The column side of UsedRange is only bloated on a few sheets,
        # and over-reporting columns costs far less than missing one.
        last_col = max(
            1,
            min(int(used.Column) + int(used.Columns.Count) - 1,
                MAX_SCAN_COLUMNS)
        )
    except Exception:
        pass

    if key is not None:
        _bounds_cache[key] = (last_row, last_col)

    return last_row, last_col


def find_product_rows(ws, product):
    """Every row of the sheet that belongs to this product, plus the
    column the name was found in (used as the identity column when a
    sheet has no frozen panes).

    Uses Excel's own Find over the sheet's real data area — walking the
    cells from Python over a 150k-row loader would take minutes per
    product."""

    last_row, last_col = sheet_bounds(ws)

    try:
        search_range = ws.Range(
            ws.Cells(1, 1), ws.Cells(last_row, last_col)
        )

        cell = search_range.Find(
            _escape_for_find(product),
            ws.Cells(1, 1),
            SEARCH_IN,
            XL_PART,
            XL_BY_ROWS,
            XL_NEXT,
            False
        )
    except Exception:
        return [], None

    if cell is None:
        return [], None

    first_address = None
    seen = set()

    rows = set()
    match_col = None

    for _ in range(MAX_FIND_STEPS):

        if cell is None:
            break

        try:
            address = cell.Address
        except Exception:
            break

        if first_address is None:
            first_address = address
        elif address == first_address or address in seen:
            break

        seen.add(address)

        try:
            value = cell.Value
            row = int(cell.Row)
            col = int(cell.Column)
        except Exception:
            break

        if _cell_matches(value, product):
            rows.add(row)
            if match_col is None:
                match_col = col

        try:
            cell = search_range.FindNext(cell)
        except Exception:
            break

    return sorted(rows), match_col


def row_groups(rows):
    """Rows that sit together become one screenshot."""

    groups = []

    for row in rows:
        if groups and row - groups[-1][-1] <= GROUP_GAP:
            groups[-1].append(row)
        else:
            groups.append([row])

    return groups


def _col_letter(index):

    letters = ""

    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters

    return letters


def row_value_columns(ws, first_row, last_row, last_col):
    """Which columns actually hold something for these rows.

    One bulk read of the block rather than a question per column, and
    bounded by last_col so it never reaches into the empty right-hand
    side of the sheet."""

    if last_col < 2:
        return [1]

    try:
        block = ws.Range(
            ws.Cells(first_row, 1),
            ws.Cells(last_row, last_col)
        ).Value
    except Exception:
        # Couldn't read it — treat every column as worth showing rather
        # than quietly leaving data out of the report.
        return list(range(1, last_col + 1))

    filled = set()

    for row_values in block or ():
        for index, value in enumerate(row_values, start=1):
            if value is None:
                continue
            if str(value).strip() == "":
                continue
            filled.add(index)

    return sorted(filled)


def _hidden_state(ws, first_col, last_col):
    """True / False if every column in the span agrees, None if mixed."""

    try:
        state = ws.Range(
            ws.Cells(1, first_col), ws.Cells(1, last_col)
        ).EntireColumn.Hidden
    except Exception:
        return None

    return None if state is None else bool(state)


def _set_hidden(ws, columns, hidden):
    """Hide or show these columns, a handful of areas per call instead of
    one call per column."""

    chunk = []

    for col in columns:

        letter = _col_letter(col)
        chunk.append(f"{letter}:{letter}")

        if len(chunk) == HIDE_CHUNK:
            ws.Range(",".join(chunk)).EntireColumn.Hidden = hidden
            chunk = []

    if chunk:
        ws.Range(",".join(chunk)).EntireColumn.Hidden = hidden


def hide_empty_columns(ws, keep, first_col, last_col):
    """Hide the columns these rows leave empty, so what IS filled closes
    up and fits in far fewer screenshots.

    This is what the by-hand screenshots do: nobody photographs twenty
    blank columns to reach the next value. Only the scrolling side is
    touched — the frozen identity columns stay — and only columns that
    were visible to begin with, so a column the sheet already had hidden
    is left hidden and is NOT reopened afterwards.

    Returns the columns it hid, to give back when the group is done."""

    if not HIDE_EMPTY_COLUMNS or last_col <= first_col:
        return []

    wanted = set(keep)

    empty = [c for c in range(first_col, last_col + 1) if c not in wanted]

    if not empty:
        return []

    # Normally nothing in the span is hidden yet, and then one question
    # answers it for the whole span instead of one per column.
    state = _hidden_state(ws, first_col, last_col)

    if state is None:
        empty = [c for c in empty if not _is_hidden(ws, c)]
    elif state is True:
        return []

    if not empty:
        return []

    try:
        _set_hidden(ws, empty, True)
    except Exception as e:
        print(f"   ⚠ Couldn't fold away the empty columns ({e}) — "
              "screenshotting the sheet as it is.")
        return []

    return empty


def _is_hidden(ws, col):

    try:
        return bool(ws.Columns(col).Hidden)
    except Exception:
        return False


def restore_columns(ws, columns):
    """Put back exactly what hide_empty_columns took away."""

    if not columns:
        return

    try:
        _set_hidden(ws, columns, False)
    except Exception as e:
        print(f"   ⚠ Couldn't unhide the columns again ({e}) — the file "
              "isn't saved, so closing it without saving puts them back.")


def data_extent(ws, first_row, last_row):
    """The last column worth screenshotting for these rows.

    First the last column holding a value, then a short peek further
    right for cells that are still coloured but empty — the loaders mark
    "this field was reviewed" with a fill, and the by-hand screenshots
    include those. Where both run out, the row is over."""

    _sheet_rows, scan_to = sheet_bounds(ws)

    last_col = 1

    # The last column these rows actually hold something in. Asking Excel
    # (one backwards Find across the block) instead of reading every cell
    # value into Python: same answer, a fraction of the time.
    try:
        block = ws.Range(
            ws.Cells(first_row, 1),
            ws.Cells(last_row, scan_to)
        )

        cell = block.Find(
            "*", ws.Cells(first_row, 1), XL_FORMULAS, XL_PART,
            XL_BY_COLUMNS, XL_PREVIOUS, False
        )

        if cell is not None:
            last_col = int(cell.Column)

    except Exception:
        pass

    # ...and now the coloured-but-empty tail. The window is fixed here:
    # letting it grow with every coloured column would walk a sheet with
    # whole-row fill all the way to the last column.
    stop = min(scan_to, last_col + FILL_LOOKAHEAD)

    if last_col >= stop:
        return last_col

    if _fill_of(ws, first_row, last_row, last_col + 1, stop) == XL_NONE:
        # Nothing coloured anywhere past the values — the usual case, and
        # one question instead of forty.
        return last_col

    misses = 0
    col = last_col + 1

    while col <= stop and misses < FILL_MISSES:

        color = _fill_of(ws, first_row, last_row, col, col)

        if color is None:
            break

        # 0 stands in for "mixed" — some of those cells are filled.
        if color != XL_NONE:
            last_col = col
            misses = 0
        else:
            misses += 1

        col += 1

    return last_col


def _fill_of(ws, first_row, last_row, first_col, last_col):
    """The fill of a block: xlNone if none of it is filled, 0 if it's a
    mix, the colour index if it's all one colour. None if Excel wouldn't
    say."""

    try:
        color = ws.Range(
            ws.Cells(first_row, first_col),
            ws.Cells(last_row, last_col)
        ).Interior.ColorIndex
    except Exception:
        return None

    # Excel answers a mixed block with Nothing, which arrives as None.
    return 0 if color is None else int(color)


# -------------------------
# THE VIEW
# -------------------------

def _data_pane(win):
    """The pane that actually scrolls — bottom-right when panes are
    frozen, the whole window when they aren't."""

    try:
        panes = win.Panes
        return panes(panes.Count)
    except Exception:
        return None


def ensure_panes(win, ws, identity_end):
    """Make sure the identity columns stay on screen while the rest
    scrolls.

    The loaders already come with frozen panes, which is what makes the
    by-hand screenshots look the way they do — those are left alone.
    Only a sheet without them gets one, and it's undone afterwards.

    Returns (pane, created_here)."""

    pane = _data_pane(win)

    try:
        frozen = bool(win.FreezePanes)
    except Exception:
        frozen = False

    if frozen and pane is not None:
        return pane, False

    try:
        win.FreezePanes = False
        pane = _data_pane(win)

        if pane is not None:
            pane.ScrollRow = 1
            pane.ScrollColumn = 1

        ws.Cells(2, max(2, identity_end + 1)).Select()
        win.FreezePanes = True

    except Exception as e:
        print(f"⚠ Couldn't freeze the identity columns ({e}) — "
              "screenshots will scroll the whole sheet.")
        return _data_pane(win), False

    return _data_pane(win), True


def _visible_last_col(pane):
    visible = pane.VisibleRange
    return int(visible.Column) + int(visible.Columns.Count) - 1


def _visible_last_row(pane):
    visible = pane.VisibleRange
    return int(visible.Row) + int(visible.Rows.Count) - 1


def frozen_through(pane):
    """The last column of the frozen block — the identity columns that
    show in every screenshot no matter where the sheet is scrolled.

    Found by asking for column 1: Excel clamps that to the freeze
    point."""

    try:
        pane.ScrollColumn = 1
        return int(pane.ScrollColumn) - 1
    except Exception:
        return 0


def _same_png(path_a, path_b):
    """Byte-for-byte the same picture. Strict on purpose: two screenfuls
    of the same coloured rows look alike to a fuzzy comparison even when
    they're completely different columns, so only an exact match counts
    as "Excel hasn't repainted yet"."""

    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False

        with open(path_a, "rb") as a, open(path_b, "rb") as b:
            return a.read() == b.read()

    except Exception:
        return False


def _grab_settled(hwnd, top_offset, previous):
    """One screenshot, taken again if it came out identical to the last
    one — that means the scroll hadn't been painted yet."""

    path = _grab_excel_window(hwnd, top_offset)

    if path is None or previous is None:
        return path

    if not _same_png(previous, path):
        return path

    os.remove(path)
    time.sleep(RESETTLE_DELAY)

    return _grab_excel_window(hwnd, top_offset)


def horizontal_shots(xl, ws, pane, hwnd, value_cols, frozen_last,
                     top_offset, previous=None):
    """Walk right across the selected rows, taking a picture only where
    those rows hold something.

    Every shot starts on a column that still has a value to show, and
    then carries whatever else fits beside it — with the empty columns
    folded away, that's a screenful of real values at a time. Columns
    already caught by the previous shot are not shot again, and a stretch
    with nothing in it is never photographed: the walk jumps straight
    over it to the next column that has something.

    Two brakes: a blank picture, and a scroll that didn't move."""

    shots = []

    # The frozen columns ride along in every picture, so only what sits
    # to the right of them has to be reached.
    pending = [c for c in value_cols if c > frozen_last]

    if not pending:
        # Everything these rows hold is inside the always-visible block:
        # one picture is the lot.
        pending = [frozen_last + 1]

    shown_through = frozen_last

    for _ in range(MAX_PASSES):

        remaining = [c for c in pending if c > shown_through]

        if not remaining and shots:
            break

        target = remaining[0] if remaining else frozen_last + 1

        try:
            pane.ScrollColumn = target
            landed = int(pane.ScrollColumn)
        except Exception:
            break

        _settle(xl)

        path = _grab_settled(hwnd, top_offset, shots[-1] if shots else previous)

        if path is None:
            break

        if pim._is_blank_image(path):
            os.remove(path)
            break

        shots.append(path)

        try:
            visible_last = _visible_last_col(pane)
        except Exception:
            break

        # No forward progress (Excel clamped the scroll, or the column is
        # wider than the window) — another shot would be the same shot.
        if visible_last <= shown_through:
            break

        shown_through = max(visible_last, landed)

    return shots


def capture_group(xl, ws, win, pane, hwnd, rows, last_col, value_cols,
                  top_offset):
    """Screenshots for one clump of rows: fold away the columns they
    leave empty, select them, put them at the top of the pane, then walk
    right across what's left. A clump taller than the window is done in
    screenfuls.

    Whatever was folded away is given back before returning, however the
    walk ends."""

    frozen_last = frozen_through(pane)

    hidden = hide_empty_columns(ws, value_cols, frozen_last + 1, last_col)

    if hidden:
        print(f"      (folded away {len(hidden)} empty column(s))")

    try:
        shots = _capture_group_rows(
            xl, ws, pane, hwnd, rows, last_col, value_cols, frozen_last,
            top_offset
        )
    finally:
        restore_columns(ws, hidden)

    return shots


def _capture_group_rows(xl, ws, pane, hwnd, rows, last_col, value_cols,
                        frozen_last, top_offset):
    """The walk itself, with the empty columns already folded away."""

    shots = []

    first_row = rows[0]
    last_row = rows[-1]

    top = first_row
    landed = None

    for _ in range(MAX_PASSES):

        try:
            ws.Range(
                ws.Cells(max(top, first_row), 1),
                ws.Cells(last_row, last_col)
            ).Select()
        except Exception:
            pass

        try:
            # A couple of rows above the match, so the group doesn't sit
            # glued to the column headers.
            pane.ScrollRow = max(1, top - CONTEXT_ROWS)
            now = int(pane.ScrollRow)
        except Exception:
            now = top

        if landed is not None and now == landed:
            break

        landed = now

        _settle(xl)

        shots.extend(
            horizontal_shots(
                xl, ws, pane, hwnd, value_cols, frozen_last, top_offset,
                shots[-1] if shots else None
            )
        )

        try:
            visible_last_row = _visible_last_row(pane)
        except Exception:
            break

        if visible_last_row >= last_row:
            break

        if len(shots) >= MAX_SHOTS_PER_SHEET:
            break

        nxt = max(top + 1, visible_last_row - OVERLAP_ROWS)

        if nxt <= top:
            break

        top = nxt

    return shots


# -------------------------
# ONE SHEET / ONE PRODUCT
# -------------------------

_filters_cleared = set()


def clear_filter(wb, ws):
    """Drop a left-over AutoFilter — once per sheet per run.

    Not cosmetic: Excel's Find skips rows an AutoFilter has hidden, so a
    filtered sheet answers "product not here" for rows that are sitting
    right there. Only the filter is touched, never the data, and nothing
    is ever saved back to the .xlsx."""

    if not CLEAR_FILTERS:
        return

    try:
        key = (wb.Name, ws.Name)
    except Exception:
        return

    if key in _filters_cleared:
        return

    _filters_cleared.add(key)

    try:
        if not ws.FilterMode:
            return

        print(f"   🧹 {ws.Name}: clearing an active filter so its rows "
              "can be searched.")

        wb.Activate()
        ws.Activate()
        ws.ShowAllData()

    except Exception as e:
        print(f"   ⚠ Couldn't clear the filter on {ws.Name}: {e}")


def process_sheet(xl, hwnd, wb, ws, product):
    """Every screenshot this sheet owes for this product — empty list if
    the product isn't in it."""

    clear_filter(wb, ws)

    rows, match_col = find_product_rows(ws, product)

    if not rows:
        return []

    if len(rows) > MAX_MATCH_ROWS:
        print(
            f"   ⚠ {ws.Name}: {len(rows)} rows match — taking the first "
            f"{MAX_MATCH_ROWS}. A longer product name would narrow it."
        )
        rows = rows[:MAX_MATCH_ROWS]

    print(f"   ✅ {ws.Name}: {len(rows)} row(s)")

    try:
        wb.Activate()
        ws.Activate()
    except Exception as e:
        print(f"   ⚠ Couldn't bring up {ws.Name}: {e}")
        return []

    win = xl.ActiveWindow

    try:
        win.WindowState = XL_MAXIMIZED
    except Exception:
        pass

    pane, created = ensure_panes(win, ws, match_col or 1)

    if pane is None:
        print(f"   ⚠ No usable pane on {ws.Name} — skipped.")
        return []

    shots = []

    _settle(xl)
    top_offset = grid_top_offset(win, hwnd)

    try:
        for group in row_groups(rows):

            if len(shots) >= MAX_SHOTS_PER_SHEET:
                print(f"   ⚠ {ws.Name}: stopping at {MAX_SHOTS_PER_SHEET} "
                      "screenshots.")
                break

            last_col = data_extent(ws, group[0], group[-1])
            value_cols = row_value_columns(
                ws, group[0], group[-1], last_col
            )

            shots.extend(
                capture_group(
                    xl, ws, win, pane, hwnd, group, last_col, value_cols,
                    top_offset
                )
            )

    except Exception:
        # Don't leave half a sheet's screenshots behind in Downloads for
        # a retry to duplicate.
        for path in shots:
            try:
                os.remove(path)
            except Exception:
                pass
        raise

    finally:
        if created:
            try:
                win.FreezePanes = False
            except Exception:
                pass

    print(f"   📸 {ws.Name}: {len(shots)} screenshot(s)")

    return shots


def collect_product(xl, hwnd, workbooks, product):
    """Every screenshot this product owes, as (heading, shots) sections.

    Nothing is written to Word from in here: that's the writer thread's
    job, and it gets on with the previous product while this one is
    being shot."""

    print(f"\n🔎 {product}")

    sections = []

    for path, wb in workbooks:

        label = loader_label(path)

        try:
            # Indexed, not iterated: pywin32's late binding can't always
            # enumerate an Excel collection.
            count = int(wb.Worksheets.Count)
        except Exception as e:
            print(f"  ⚠ Couldn't read the sheets of {label}: {e}")
            continue

        for index in range(1, count + 1):

            try:
                ws = wb.Worksheets(index)
                # -1 is xlSheetVisible; hidden sheets are left alone.
                if int(ws.Visible) != -1:
                    continue
                sheet_name = ws.Name
            except Exception:
                continue

            try:
                shots = _excel_action(
                    lambda: process_sheet(xl, hwnd, wb, ws, product),
                    f"screenshot {sheet_name}"
                )
            except Exception as e:
                print(f"  ⚠ Problem on {label} / {sheet_name}: {e} — "
                      "moving on.")
                continue

            if not shots:
                continue

            sections.append(
                (f"{product} — {label} / {sheet_name}", shots)
            )

    if not sections:
        print("  ❌ Not mentioned in any loader.")

    return sections


def write_product(word_path, product, sections):
    """The Word half — run once a product is fully screenshotted."""

    pim._add_heading_to_word(word_path, f"{product}{HEADING_SUFFIX}")

    if not sections:
        pim._add_text_block_to_word(
            word_path,
            "Not found in the loader files."
        )
        return

    for heading, shots in sections:
        pim._add_heading_to_word(word_path, heading)
        pim._add_images_to_word(shots, word_path)


# -------------------------
# ONE TICKET
# -------------------------

def attach(paths):
    """Excel plus the loaders, opening whatever isn't open yet."""

    xl = get_excel()
    hwnd = int(xl.Hwnd)

    _set_ribbon(xl, False)

    workbooks = []

    for path in paths:
        try:
            workbooks.append((path, open_workbook(xl, path)))
        except Exception as e:
            print(f"⚠ Couldn't open {os.path.basename(path)}: {e}")

    return xl, hwnd, workbooks


def ticket_report_path(ticket):
    """Downloads\\<TICKET>.docx — the same name AM and PIM use, so all
    three halves land in one report per ticket."""

    safe = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", ticket).strip(" .")

    return os.path.join(pim.DOWNLOADS, f"{safe}.docx")


def process_ticket(xl, hwnd, workbooks, writer, job, server_details):

    ticket, title, products = job

    word_path = ticket_report_path(ticket)

    print(f"\n🎫 {ticket} — {len(products)} product(s)")

    # The one Word call that has to be waited for: what's already in the
    # report decides what still needs doing.
    existing_text = writer.call(pim._read_word_text, word_path)

    if existing_text:
        print(f"📄 Adding to the end of the existing "
              f"{os.path.basename(word_path)}.")

        skip = resume_skip_count(existing_text, products)

        if skip:
            print(
                f"   ...skipping {skip} product(s) this report already "
                "has loader screenshots for."
            )

        products = products[skip:]

    else:
        writer.submit(
            pim._add_heading_to_word,
            word_path,
            f"{ticket} - {title}" if title else ticket
        )
        if server_details:
            writer.submit(pim._add_text_block_to_word, word_path,
                          server_details)

    for product in products:

        if pim.is_offering_url(product):
            print(f"\n⏭ {product} — a URL, nothing to search for in the "
                  "loaders.")
            continue

        try:
            sections = collect_product(xl, hwnd, workbooks, product)
        except Exception as e:
            print(f"⚠ Problem on {product}: {e} — moving on.")
            continue

        # Handed over, not waited for: Word writes this product while
        # Excel is already shooting the next one.
        writer.submit(write_product, word_path, product, sections)

    writer.submit(pim._save_word_document, word_path)

    return word_path


def run_excel(jobs, server_details):
    """The whole Excel half: open the loaders once, then every ticket."""

    _make_dpi_aware()

    files = find_loader_files()

    if not files:
        print(
            f"\n❌ No loader files in {pim.DOWNLOADS} matching "
            f"\"{LOADER_GLOB}\". Download them first."
        )
        return []

    print("\nLoaders:")
    for path in files:
        print(f"  - {os.path.basename(path)}")

    xl, hwnd, workbooks = attach(files)

    if not workbooks:
        print("❌ None of the loaders could be opened.")
        return []

    writer = WordWriter()
    reports = []

    try:
        for job in jobs:
            try:
                reports.append(
                    (job[0], process_ticket(
                        xl, hwnd, workbooks, writer, job, server_details
                    ))
                )
            except Exception as e:
                print(f"⚠ Problem on ticket {job[0]}: {e} — "
                      "moving on to the next ticket.")

        print("\n⏳ Waiting for Word to finish writing...")
        writer.drain()

    finally:
        writer.stop()

        try:
            _set_ribbon(xl, True)
        except Exception:
            pass

    return reports


# -------------------------
# JIRA
# -------------------------

# Jira's REST calls are made from inside the Jira page, not from Python.
# Playwright's own request API carries the session cookie but still gets
# "XSRF check failed" on every POST; a fetch running on the page passes,
# because it's a same-origin request from the site itself. Same trick
# Deployment_Defect.py uses.
JSON_FETCH = """async ({method, url, body}) => {
    const options = {
        method,
        credentials: 'same-origin',
        headers: {'Accept': 'application/json', 'X-Atlassian-Token': 'no-check'},
    };
    if (body) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    return {status: response.status, text: await response.text()};
}"""

# The file travels as base64 and is rebuilt into a real multipart upload
# on the page. Content-Type is left alone on purpose: the browser has to
# add the boundary itself.
FILE_FETCH = """async ({url, name, b64}) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const form = new FormData();
    form.append('file', new File([bytes], name));
    const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'X-Atlassian-Token': 'no-check'},
        body: form,
    });
    return {status: response.status, text: await response.text()};
}"""


def _jira_result(result):

    text = result.get("text") or ""
    data = None

    if text.strip()[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None

    return result["status"], data, text


async def jira_request(page, method, path, payload=None):
    """One REST call on the browser's own Jira session — no second
    password, no API token."""

    result = await page.evaluate(
        JSON_FETCH,
        {"method": method, "url": JIRA_BASE + path, "body": payload},
    )

    return _jira_result(result)


async def jira_upload(page, path, file_path):
    """The one call that isn't JSON: a real multipart file upload."""

    with open(file_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")

    result = await page.evaluate(
        FILE_FETCH,
        {
            "url": JIRA_BASE + path,
            "name": os.path.basename(file_path),
            "b64": encoded,
        },
    )

    return _jira_result(result)


def _jira_error(text):
    """The useful half of a Jira error body."""

    try:
        data = json.loads(text)
    except Exception:
        return (text or "")[:300]

    bits = list(data.get("errorMessages") or [])
    bits += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]

    return "; ".join(bits) or (text or "")[:300]


def _jira_now():
    """Jira wants its timestamps with the offset glued on, no colon."""

    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000%z")


def build_comment(file_name, server_details):
    """The comment, in Jira wiki markup.

    [^file.docx] is what makes the attachment show up as the tagged
    link — the same thing Jira inserts when a file is attached from
    inside the comment box."""

    lines = [
        f"*{COMMENT_HEADLINE}*",
        "",
        f"*POTs:* [^{file_name}]",
    ]

    if server_details:
        lines += ["", _bold_labels(server_details.strip())]

    return "\n".join(lines)


LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _&.\-]{0,24}?)\s*:(.*)$")


def _bold_labels(text):
    """Bold the "TOMS :" / "RBM :" / "Server Details :" labels, exactly
    like the comment written by hand. Lines that merely happen to hold a
    colon (URLs, zip names with a timestamp in them) are left alone."""

    out = []

    for line in text.splitlines():

        stripped = line.strip()
        lowered = stripped.lower()

        if lowered.startswith(("http://", "https://")):
            out.append(line)
            continue

        match = LABEL_RE.match(stripped)

        if match:
            out.append(f"*{match.group(1).strip()}* :{match.group(2)}")
        else:
            out.append(line)

    return "\n".join(out)


async def jira_attachment_names(page, ticket):

    status, data, text = await jira_request(
        page, "GET", f"/rest/api/2/issue/{ticket}?fields=attachment,status"
    )

    if status != 200 or not data:
        raise RuntimeError(f"couldn't read {ticket}: {_jira_error(text)}")

    fields = data.get("fields") or {}

    names = [a.get("filename") for a in (fields.get("attachment") or [])]
    state = ((fields.get("status") or {}).get("name") or "").strip()

    return names, state


async def jira_attach(page, ticket, word_path):

    file_name = os.path.basename(word_path)
    size = os.path.getsize(word_path) / (1024 * 1024)

    print(f"   📎 Uploading {file_name} ({size:.1f} MB)...")

    status, _data, text = await jira_upload(
        page, f"/rest/api/2/issue/{ticket}/attachments", word_path
    )

    if status not in (200, 201):
        raise RuntimeError(f"attachment refused: {_jira_error(text)}")

    print(f"   📎 Attached {file_name}")


async def jira_comment(page, ticket, body):

    status, _data, text = await jira_request(
        page,
        "POST",
        f"/rest/api/2/issue/{ticket}/comment",
        payload={"body": body},
    )

    if status not in (200, 201):
        raise RuntimeError(f"comment refused: {_jira_error(text)}")

    print("   💬 Comment added")


def _find_transition(transitions, targets):
    """The transition that lands on one of these statuses (or is named
    like one). Matched whole: "Passed" must not pick "Passed With Minor
    Defects"."""

    for transition in transitions:

        to_name = ((transition.get("to") or {}).get("name") or "").strip().lower()
        own_name = (transition.get("name") or "").strip().lower()

        if to_name in targets or own_name in targets:
            return transition

    return None


def _pick_transition(transitions, state):
    """The next move toward Passed from where the ticket is now.

    Straight to Passed whenever the workflow offers it from here; failing
    that, an Open ticket goes to In Progress first. Returns (transition,
    wanted): wanted is None when the ticket isn't somewhere it gets
    moved from, and otherwise names the status that was being aimed at,
    for the message when no way there exists."""

    if state.strip().lower() not in MOVABLE_FROM:
        return None, None

    goal = _find_transition(transitions, ("passed", "pass"))

    if goal is not None:
        return goal, GOAL_STATUS

    if state.strip().lower() == "open":

        start = _find_transition(transitions, ("in progress",))

        if start is not None:
            return start, GOAL_STATUS

    return None, GOAL_STATUS


def _transition_payload(transition):
    """Fill in that transition's screen — the same boxes that get typed
    in by hand on the Pass dialog.

    Everything the screen OFFERS is filled, not only what it demands: on
    this workflow % Passed, % Executed and Log Work are all optional, so
    going by "required" alone would move the ticket with the boxes left
    empty.

    Anything required that can't be worked out is reported instead of
    guessed, so that ticket is left for a human rather than moved with a
    made-up value."""

    fields = {}
    update = {}
    unfilled = []

    for field_id, meta in (transition.get("fields") or {}).items():

        name = (meta.get("name") or "").lower()
        kind = ((meta.get("schema") or {}).get("type") or "").lower()
        required = bool(meta.get("required"))

        if field_id == "comment":
            # The real comment is posted on its own, so this is only
            # filled when the screen refuses to submit without one.
            if required:
                update["comment"] = [{"add": {"body": COMMENT_HEADLINE}}]

        elif field_id == "worklog" or "log work" in name or "time spent" in name:
            update["worklog"] = [
                {"add": {"timeSpent": TIME_SPENT, "started": _jira_now()}}
            ]

        elif "%" in name or "percent" in name:

            if "pass" in name:
                value = PERCENT_PASSED
            elif "execut" in name:
                value = PERCENT_EXECUTED
            elif required:
                value = 100
            else:
                continue

            fields[field_id] = value if kind == "number" else str(value)

        elif kind in ("date", "datetime") or "date started" in name:
            stamp = _jira_now()
            fields[field_id] = stamp[:10] if kind == "date" else stamp

        elif required and meta.get("allowedValues"):
            first = meta["allowedValues"][0]
            value = {"id": first["id"]} if "id" in first else first
            fields[field_id] = [value] if kind == "array" else value

        elif required:
            unfilled.append(meta.get("name") or field_id)

    return fields, update, unfilled


async def jira_transition(page, ticket, state):
    """Walk the ticket to Passed: straight there from In Progress, through
    In Progress first from Open. The transitions are asked for again after
    every move, because what's on offer (and what the Pass screen wants
    filled in) depends on where the ticket is by then."""

    for _ in range(MAX_TRANSITIONS):

        status, data, text = await jira_request(
            page,
            "GET",
            f"/rest/api/2/issue/{ticket}/transitions"
            "?expand=transitions.fields",
        )

        if status != 200 or not data:
            raise RuntimeError(
                f"couldn't read transitions: {_jira_error(text)}"
            )

        transitions = data.get("transitions") or []

        transition, wanted = _pick_transition(transitions, state)

        if transition is None:

            if wanted is None:
                print(f"   ⏭ Status is \"{state}\" — not one that gets "
                      "moved, left as it is.")
            else:
                available = ", ".join(
                    t.get("name", "?") for t in transitions
                ) or "none"
                print(f"   ⚠ Stuck at \"{state}\": no way on to "
                      f"\"{wanted}\" from here. Available: {available}.")

            return

        fields, update, unfilled = _transition_payload(transition)

        if unfilled:
            print(f"   ⚠ \"{transition.get('name')}\" also needs "
                  f"{', '.join(unfilled)} — left at \"{state}\" so nothing "
                  "gets filled in with a guess. Finish it by hand.")
            return

        payload = {"transition": {"id": transition["id"]}}

        if fields:
            payload["fields"] = fields
        if update:
            payload["update"] = update

        status, _data, text = await jira_request(
            page,
            "POST",
            f"/rest/api/2/issue/{ticket}/transitions",
            payload=payload,
        )

        if status not in (200, 204):
            raise RuntimeError(
                f"couldn't move it on from \"{state}\" "
                f"({transition.get('name')}): {_jira_error(text)}"
            )

        to_name = (
            (transition.get("to") or {}).get("name")
            or transition.get("name")
        )

        print(f"   ➡ {state} → {to_name}")

        state = to_name

        if state.strip().lower() == GOAL_STATUS.lower():
            return

    print(f"   ⚠ Still at \"{state}\" after {MAX_TRANSITIONS} moves — "
          "stopping so it can't loop. Finish it by hand.")


async def ensure_on_jira(page):
    """Put the page on Jira if it isn't already.

    The REST calls run as fetches from whatever page this is, which is
    the whole point — a same-origin fetch is what gets past the XSRF
    check. On a fresh tab (about:blank) that same fetch is cross-origin
    and dies with "Failed to fetch", so a page handed over by
    PIM/AM has to be sent to Jira first."""

    try:
        if page.url.startswith(JIRA_BASE):
            return
    except Exception:
        pass

    await page.goto(
        JIRA_BASE + "/", wait_until="domcontentloaded", timeout=60000
    )

    await pim.login_tms_if_needed(page)


async def update_jira(page, reports, server_details):
    """Attach the report, comment, and move the ticket to Passed."""

    await ensure_on_jira(page)

    status, me, text = await jira_request(page, "GET", "/rest/api/2/myself")

    if status != 200 or not me:
        print(f"\n❌ Jira didn't recognise the session ({_jira_error(text)}). "
              "Nothing was changed on any ticket.")
        return

    print(f"\n👤 Jira: {me.get('displayName')} ({me.get('name')})")

    for ticket, word_path in reports:

        print(f"\n🎫 {ticket}")

        if not word_path or not os.path.exists(word_path):
            print("   ⚠ No report file to attach — skipped.")
            continue

        try:
            # On the ticket's own page: the REST calls run as fetches
            # from there, which is what gets them past the XSRF check.
            await page.goto(
                pim.TMS_TICKET_URL + ticket,
                wait_until="domcontentloaded",
                timeout=60000
            )
            await pim.login_tms_if_needed(page)

            names, state = await jira_attachment_names(page, ticket)
        except Exception as e:
            print(f"   ⚠ {e} — skipped.")
            continue

        file_name = os.path.basename(word_path)

        if file_name in names:
            if not ask_yes_no(
                f"   ⚠ {ticket} already has a {file_name}. Attach this one "
                "too?"
            ):
                print("   ⏭ Ticket left untouched.")
                continue

        try:
            await jira_attach(page, ticket, word_path)
            await jira_comment(
                page, ticket, build_comment(file_name, server_details)
            )
        except Exception as e:
            print(f"   ⚠ {e} — status left as it is.")
            continue

        try:
            await jira_transition(page, ticket, state)
        except Exception as e:
            print(f"   ⚠ {e}")


# -------------------------
# THE TICKET LIST
# -------------------------

def ask_yes_no(question):
    """No is the safe answer, so anything that isn't a clear yes — an
    interrupted prompt included — counts as no."""

    while True:

        try:
            answer = input(f"{question} [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if answer in ("y", "yes", "s", "si", "sí"):
            return True

        if answer in ("n", "no"):
            return False


async def read_ticket(page, ticket):
    """Title + product list for one ticket. Falls back to asking when the
    description can't be read."""

    print(f"\n🎫 Opening ticket: {ticket}")

    title = ""
    products = []

    try:
        await page.goto(
            pim.TMS_TICKET_URL + ticket,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await pim.login_tms_if_needed(page)
        await page.wait_for_timeout(900)

        title = await pim.extract_jira_title(page)
        print(f"📌 Title: {title}")

        description = await pim.extract_jira_description(page)
        products = extract_products(description)

    except Exception as e:
        print(f"⚠ Couldn't read {ticket}: {e}")

    if not products:
        print(f"⚠ No product list found in {ticket} — paste it.")
        products = pim.read_offerings()

    print("\nProducts:")
    for p in products:
        print(f"  - {p}")

    return ticket, title, products


# -------------------------
# THE RUN
# -------------------------

async def continue_from_validation(context, raw_text, jobs):
    """Everything after the tickets are known: the loader screenshots,
    the question, and the Jira updates.

    This is what PIM_Validation and AM_Validation call when they're done.
    They already asked for the Server Details and already read each
    ticket's product list, so both are handed over rather than asked for
    a second time — `jobs` is a list of (ticket, title, products) and
    `raw_text` is the block that was pasted at the start.

    Reports go to the same Downloads\\<TICKET>.docx those two just
    wrote, so the loader screenshots land at the end of the report they
    already built."""

    server_details = pim.extract_server_details(raw_text)

    jobs = [job for job in jobs if job and job[2]]

    if not jobs:
        print("\n⏭ No products to look up in the loaders.")
        return []

    print(
        "\n================\n"
        "📊 Loader screenshots — same tickets, same Server Details, "
        "nothing to paste again."
    )

    reports = run_excel(jobs, server_details)

    if not reports:
        print("\n⏭ No loader report was written, so there's nothing to "
              "put on the tickets.")
        return reports

    print(
        """
🎉 Screenshots done.

Excel and Word are left open — the loaders are where they were and
every report is there to look through."""
    )

    for _ticket, word_path in reports:
        print(f"  📄 {word_path}")

    print()

    if ask_yes_no(
        "All good? Answering yes attaches each report to its ticket, "
        "adds the Tested And Passed comment, and moves the ticket to "
        "Passed."
    ):
        page = await context.new_page()

        try:
            await update_jira(page, reports, server_details)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    else:
        print("\n👍 Nothing sent to Jira. The reports are still in "
              "Downloads.")

    return reports


# -------------------------
# MAIN
# -------------------------

async def main():

    pim.HEADLESS = HEADLESS

    raw_text = pim.read_multiline()

    tickets = pim.extract_tickets(raw_text)

    print("\n================")
    print("Tickets:")
    for t in tickets:
        print(f"  {t}")

    if not tickets:
        print("❌ No tickets/links detected.")
        return

    jobs = []

    async with async_playwright() as pw:

        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()

        for ticket in tickets:
            jobs.append(await read_ticket(page, ticket))

        # Same tail PIM and AM run — the browser stays open through the
        # Excel pass, because its logged-in session is what attaches and
        # comments at the end.
        await continue_from_validation(context, raw_text, jobs)

        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
