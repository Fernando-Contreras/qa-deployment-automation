import asyncio
import itertools
import re
import sys
import time
import os
import ctypes
from urllib.parse import urlsplit
from datetime import datetime
from PIL import Image, ImageChops
import win32com.client
import pythoncom
import win32gui
import win32con
import win32process
import pyautogui
from playwright.async_api import async_playwright, Page

# The filter popup's in-page click isn't reliable — a real OS-level
# click at its fixed screen position is. Don't let pyautogui abort on
# corner-of-screen moves.
pyautogui.FAILSAFE = False

# Marks the tab we're about to click on, so we can find and foreground
# its actual OS window without ever grabbing some other Chrome window
# (e.g. the user's own browsing session).
FOREGROUND_TITLE_MARKER = "PIM_VALIDATION_ACTIVE_TAB"


# Credentials are no longer written here: they live DPAPI-encrypted in the
# same store the NTT scripts use (nc_credentials.py, in ..\NTT). The first
# run asks for anything that isn't saved yet; after that it never asks again.
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NTT"))
from nc_credentials import get_credentials  # noqa: E402


def _jira_credentials():
    """Same rule as Deployment_Defect.py: a 'jira' entry if there is one,
    otherwise the 'jenkins' one (same corporate login)."""
    try:
        return get_credentials("jira", prompt=False)
    except RuntimeError:
        return get_credentials("jenkins", default_username="your_user")


USERNAME, PASSWORD = get_credentials("toms", default_username="toms_user")
JIRA_USERNAME, JIRA_PASSWORD = _jira_credentials()

DOWNLOADS = os.path.join(
    os.path.expanduser("~"),
    "Downloads"
)

# Which TOMS to search is NOT fixed — it's whatever the pasted Server
# Details say, and an offering that exists in one environment often
# doesn't exist in another. Only the host changes though: the Product
# Offerings object itself carries the same id everywhere, so the search
# URL is this path hung off whatever host was given. The tab parameter
# lands straight on the Product Offerings grid, whose Name column holds
# the filter.
OBJECT_PATH = (
    "/common/uobject.jsp?tab=_Product+Offerings"
    "&object=9128632109113273016"
)

# Used only when the pasted TOMS names a host with no port.
TOMS_DEFAULT_PORT = 6300

TMS_TICKET_URL = "https://jira.example.com/browse/"

FILTER_ICON_SCREEN_X = 456
FILTER_ICON_SCREEN_Y = 2773

# Headless runs a bit faster (no window to paint). The filter is now
# opened from inside the page rather than with an OS-level mouse click,
# so searching by name works headless too — but the fixed-coordinate
# click and the wait-for-a-manual-click rescue can't, since neither has a
# window. Headless therefore fails fast on a filter it couldn't open
# instead of hanging.
HEADLESS = False

# How many TOMS tabs are worked at the same time underneath each price
# variant (price nodes and their dated versions — the bulk of a run).
# Their screenshots are still written to the report in the usual order.
# Set to 1 to go back to one tab at a time.
PARALLEL_TABS = 4

# Written next to the reports when the filter can't be opened, so the
# next fix can be made from what the page really looked like.
FILTER_DEBUG_PREFIX = "pim_filter_debug"

# Once every ticket is done, hand them to Excel_Screenshots.py so the
# loader screenshots go at the end of the same reports — no pasting the
# Server Details or the ticket list a second time. AM_Validation gets
# this through run_excel_screenshots() below. Set to False to stop after
# the TOMS screenshots.
RUN_EXCEL_SCREENSHOTS = True


def is_offering_url(entry):
    """True if an offering was given as a direct link to its object page
    instead of by name.

    Offering names in the ticket don't always match TOMS exactly, so the
    search can come up empty. A URL says exactly where the offering is,
    so there's nothing left to search for — we just open it."""

    return bool(
        re.match(r"^https?://", entry.strip(), re.I)
    )


# -------------------------
# PARSE INPUT
# -------------------------

def extract_toms(text):

    match = re.search(
        r"TOMS\s*:\s*(\S+)",
        text,
        re.I
    )

    if match:
        return match.group(1)

    return None


HOSTISH_RE = re.compile(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*(:\d+)?$")

ANY_URL_RE = re.compile(r"https?://[^\s\"'<>\]]+", re.I)


def _looks_like_host(value):
    """Host-shaped enough to actually dial: "192.0.2.4",
    "192.0.2.4:6300", "toms.example.com".

    Underscores rule it out (they're not legal in host names) and so does
    a bare single word with no port — that way an environment code name
    like "GCI_R12_ENV" is rejected instead of being turned into a URL
    that could never resolve."""

    if not HOSTISH_RE.match(value):
        return False

    host = value.split(":")[0]

    return "." in host or ":" in value


def build_object_url(toms):
    """Turn whatever was pasted next to "TOMS:" into the Product
    Offerings search URL on THAT environment.

    Accepts any of the shapes that show up in Server Details — a bare
    host, a host:port, or a full link to some page on the box — and keeps
    only the origin, since the object path is the same everywhere.
    Returns None if the value isn't an address at all (e.g. an
    environment code name), so the caller can look elsewhere."""

    value = (toms or "").strip().strip('"').strip("'").rstrip("/")

    if not value:
        return None

    if "://" not in value:
        # Reject things like "GCI_R12_ENV" that can't be dialled.
        if not _looks_like_host(value):
            return None
        value = "http://" + value

    parsed = urlsplit(value)

    host = parsed.netloc

    if not host:
        return None

    if ":" not in host:
        host = f"{host}:{TOMS_DEFAULT_PORT}"

    scheme = parsed.scheme or "http"

    return f"{scheme}://{host}{OBJECT_PATH}"


def resolve_object_url(toms, raw_text):
    """The search URL for this run's TOMS.

    Falls back to the first link in the pasted Server Details, so a
    "TOMS:" line holding an environment code name instead of an address
    still works as long as a real link is in there somewhere."""

    object_url = build_object_url(toms)

    if object_url:
        return object_url

    match = ANY_URL_RE.search(raw_text or "")

    if match:
        return build_object_url(match.group(0))

    return None


def extract_tickets(text):
    """Pull the ticket keys out of the pasted text.

    Accepts both full TMS links and the bare tail, e.g.:
        https://jira.example.com/browse/VALPRJ-184984
        VALPRJ-196650
    Returns a de-duplicated list preserving order, so each ticket is
    stored just as "VALPRJ-184984"."""

    tickets = []

    for match in re.finditer(r"[A-Z][A-Z0-9]+-\d+", text):
        key = match.group(0)
        if key not in tickets:
            tickets.append(key)

    return tickets


def extract_server_details(text):
    """Return the "Server Details" block (from that line downwards) with
    the ticket links removed, to put at the top of the Word report.

    Drops any line that is a TMS link or a bare ticket key, and starts
    capturing at the "Server Details" line if present."""

    lines = text.splitlines()

    kept = []
    started = False

    for line in lines:

        stripped = line.strip()

        # Skip ticket links (full URL or bare key).
        if "jira.example.com" in stripped.lower():
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", stripped):
            continue

        if not started:
            if re.search(r"server\s*details", stripped, re.I):
                started = True
            else:
                continue

        kept.append(line)

    result = "\n".join(kept).strip()

    return result if result else text.strip()


# A line that announces the device/offering list. The wording moves
# around between tickets ("Navigate to following devices:", "Search for
# the devices :", "Product Offerings:"), so match on the noun plus its
# colon rather than on a whole sentence. The colon matters: it's what
# keeps "Navigate to ... -> Product Offerings." (a step, not a list) from
# being mistaken for a header.
OFFERING_LIST_HEADERS = [
    re.compile(r"navigate to (the\s+)?following (devices|offerings)", re.I),
    re.compile(r"\b(devices?|offerings?)\s*:", re.I),
]

# What a device name looks like when there's no header to go by: shouty,
# several words, and either a capacity ("... 256GB") or a short code up
# front ("CPO ...", "OB ...").
CAPACITY_RE = re.compile(r"\d+\s*(GB|TB|MB)\b", re.I)
SHORT_CODE_RE = re.compile(r"^[A-Z]{2,4}\s+[A-Z0-9]")
OBJECT_URL_RE = re.compile(r"https?://\S*(nc|u)object\.jsp\S*", re.I)

TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
NUMBERED_STEP_RE = re.compile(r"^\d+[\.\)]")


def _clean_offering_line(line):
    """Strip the bullet/·/- that Jira renders in front of list items."""

    return re.sub(r"^[\-\*•·]\s*", "", line.strip()).strip()


def _looks_like_offering(line):
    """True if a line looks like a device/offering name on its own.

    Used only as a safety net, so it's deliberately picky — it's better
    to miss a name and ask than to send junk off to be searched."""

    line = _clean_offering_line(line)

    if not line or len(line) > 150:
        return False

    if OBJECT_URL_RE.fullmatch(line):
        return True

    if "://" in line:
        return False

    if NUMBERED_STEP_RE.match(line) or line.endswith(":"):
        return False

    if TICKET_KEY_RE.match(line):
        return False

    # Offering names are always several words.
    if len(line.split()) < 2:
        return False

    letters = [c for c in line if c.isalpha()]

    if not letters:
        return False

    upper = sum(1 for c in letters if c.isupper()) / len(letters)

    if upper < 0.8:
        return False

    return bool(CAPACITY_RE.search(line) or SHORT_CODE_RE.match(line))


def _collect_offering_list(lines, header_index):
    """Read the list that follows a header line, e.g. everything between
    "2. Search for the devices :" and "3. Validate ...".

    The list ends at the next numbered step, or at a blank line once at
    least one item has been picked up."""

    offerings = []

    # The list sometimes starts on the header line itself
    # ("Search for the devices : CPO SAMSUNG ...").
    header = lines[header_index]

    if ":" in header:
        tail = _clean_offering_line(header.split(":", 1)[1])
        if tail and (_looks_like_offering(tail) or is_offering_url(tail)):
            offerings.append(tail)

    for line in lines[header_index + 1:]:

        stripped = line.strip()

        if not stripped:
            # Blank line before the list starts -> keep waiting.
            # Blank line once we have items -> the list is over.
            if offerings:
                break
            continue

        if NUMBERED_STEP_RE.match(stripped):
            break

        cleaned = _clean_offering_line(stripped)

        if cleaned:
            offerings.append(cleaned)

    return offerings


def extract_offerings_from_description(text):
    """Pull the offering/device list out of a Jira ticket description.

    First choice is the structural read: find the line that announces the
    list ("Search for the devices :") and take what follows, which keeps
    working whether the ticket lists names or object URLs.

    If no header matches — or the one that does is followed by prose
    instead of devices — fall back to scanning the whole description for
    lines that look like device names, so a differently-worded ticket
    still gets picked up instead of stopping to ask."""

    if not text:
        return []

    lines = text.splitlines()

    for i, line in enumerate(lines):

        stripped = line.strip()

        if not any(p.search(stripped) for p in OFFERING_LIST_HEADERS):
            continue

        offerings = _collect_offering_list(lines, i)

        if offerings:
            return offerings

    # Safety net: no usable header, so go by what the lines look like.
    offerings = []

    for line in lines:

        cleaned = _clean_offering_line(line)

        if cleaned and cleaned not in offerings and _looks_like_offering(cleaned):
            offerings.append(cleaned)

    if offerings:
        print(
            "ℹ No device list header found — picked these up by name "
            "shape instead. Check them before trusting the report."
        )

    return offerings


# -------------------------
# LOGIN
# -------------------------

async def login_if_needed(page: Page, probe_timeout: float = 5000):

    username = page.locator(
        'input[name="username"], input[type="text"]'
    ).first

    password = page.locator(
        'input[name="password"], input[type="password"]'
    ).first


    # Detect the login page by its password field.
    # A page where we're already logged in won't have one, so a short
    # probe is enough — no need to sit through a long timeout when we're
    # already in.
    try:
        await password.wait_for(
            state="visible",
            timeout=probe_timeout
        )
    except Exception:
        print("ℹ Already logged in — skipping login.")
        return


    print("🔐 Login required...")


    await username.fill(USERNAME)
    await password.fill(PASSWORD)

    await page.keyboard.press("Enter")


    # Submitting navigates away from the login page. Waiting for
    # "networkidle" is unreliable here: Corp keeps polling in the
    # background, so the network never goes idle and we'd waste the full
    # timeout before giving up. Instead, wait for the login form itself
    # to disappear, which is the real signal that login succeeded.
    try:
        await password.wait_for(
            state="hidden",
            timeout=30000
        )
    except Exception:
        pass


    await page.wait_for_load_state(
        "domcontentloaded",
        timeout=30000
    )


    print("✅ Login completed.")


async def login_tms_if_needed(page: Page):

    password = page.locator(
        'input[name="j_password"], input[name="password"], '
        'input[type="password"]'
    ).first

    # Detect the TMS login page by its password field, same idea as
    # login_if_needed() above.
    try:
        await password.wait_for(
            state="visible",
            timeout=5000
        )
    except Exception:
        print("ℹ Already logged in to TMS — skipping login.")
        return


    print("🔐 TMS login required...")


    username = page.locator(
        'input[name="j_username"], input[name="username"], '
        'input[type="text"]'
    ).first

    try:
        await username.wait_for(
            state="visible",
            timeout=3000
        )
        await username.fill(JIRA_USERNAME)
        await password.fill(JIRA_PASSWORD)
        await page.keyboard.press("Enter")

    except Exception:
        # Fallback for a login page that auto-focuses the first field
        # instead of exposing a named username input.
        await page.keyboard.type(JIRA_USERNAME, delay=40)
        await page.keyboard.press("Tab")
        await page.keyboard.type(JIRA_PASSWORD, delay=40)
        await page.keyboard.press("Enter")


    try:
        await password.wait_for(
            state="hidden",
            timeout=30000
        )
    except Exception:
        pass


    await page.wait_for_load_state(
        "domcontentloaded",
        timeout=30000
    )


    print("✅ TMS login completed.")



# -------------------------
# TICKET TITLE + DESCRIPTION
# -------------------------

async def extract_jira_title(page: Page):
    """Return the ticket's summary/title, tried across a few known Jira
    layouts and falling back to the browser tab title."""

    selectors = [
        "#summary-val",
        '[data-testid="issue.views.issue-base.foundation.summary.heading"]',
        "h1#summary-val",
        "h1",
    ]

    for sel in selectors:

        loc = page.locator(sel).first

        try:
            await loc.wait_for(state="visible", timeout=4000)
            text = (await loc.inner_text()).strip()
            if text:
                return text
        except Exception:
            continue

    title = (await page.title()).strip()
    title = re.sub(r'^\[[A-Z][A-Z0-9]+-\d+\]\s*', "", title)
    title = re.sub(r'\s*-\s*TMS.*$', "", title, flags=re.I)

    return title


async def extract_jira_description(page: Page):
    """Return the ticket's description as plain text, or None if no known
    layout matched."""

    selectors = [
        "#description-val",
        '[data-testid="issue.views.field.rich-text.description"]',
        ".user-content-block",
    ]

    for sel in selectors:

        loc = page.locator(sel).first

        try:
            await loc.wait_for(state="visible", timeout=4000)
            text = await loc.inner_text()
            if text and text.strip():
                return text
        except Exception:
            continue

    return None



# -------------------------
# WINDOWS-LEVEL FOREGROUND
# -------------------------

def _find_chromium_hwnd(title_hint: str):
    """Find a visible top-level Chromium window whose title contains
    `title_hint`. Used instead of matching "any Chrome window" so this
    never accidentally grabs the user's own separate Chrome session."""

    matches = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":
            return
        if title_hint in win32gui.GetWindowText(hwnd):
            matches.append(hwnd)

    win32gui.EnumWindows(_callback, None)
    return matches[0] if matches else None


def _force_window_foreground(hwnd):
    """Windows blocks background processes from stealing focus outright,
    so page.bring_to_front() alone doesn't reliably raise the actual OS
    window — a coordinate click can then land on whatever else has
    focus. This is the standard AttachThreadInput workaround."""

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)

    if fg_thread != target_thread:
        ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, True)
        try:
            win32gui.SetForegroundWindow(hwnd)
        finally:
            ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, False)
    else:
        win32gui.SetForegroundWindow(hwnd)


async def bring_browser_to_foreground(page: Page):
    """Actually foreground the Chromium OS window (not just the
    Playwright tab) so an OS-level (pyautogui) click lands where it
    should. Marks the tab's title first so the right window is found
    even if the user has their own separate Chrome open."""

    try:
        await page.evaluate(
            "(marker) => { document.title = marker; }",
            FOREGROUND_TITLE_MARKER
        )
    except Exception:
        pass

    await page.bring_to_front()

    hwnd = _find_chromium_hwnd(FOREGROUND_TITLE_MARKER)

    if hwnd is None:
        print("⚠ Could not find the Chromium window to foreground.")
        return

    try:
        _force_window_foreground(hwnd)
    except Exception:
        pass


# -------------------------
# FRAME-AWARE LOCATOR
# -------------------------

async def locate_in_any_frame(
    page: Page,
    make_locator,
    timeout: float = 60000
):
    """Search the top document AND every child frame for an element.

    Corp's object page renders inside nested frames, so a plain
    page.locator() only looks at the top frame and misses anything that
    lives inside a child frame (which is why the filter icon "only
    appears" once you click into it manually).

    ``make_locator`` is a function that, given a frame, returns the
    locator to look for — e.g. ``lambda f: f.locator('...').first``.
    Returns the locator from whichever frame actually contains a visible
    match; raises TimeoutError if none do within ``timeout`` ms.
    """

    end = time.monotonic() + timeout / 1000

    while True:

        for frame in page.frames:

            loc = make_locator(frame)

            try:
                await loc.wait_for(
                    state="visible",
                    timeout=1000
                )
                return loc

            except Exception:
                continue

        if time.monotonic() >= end:
            raise TimeoutError(
                "Element not found in any frame."
            )

        await page.wait_for_timeout(150)


async def find_sub_item_names(
    page: Page,
    prefix: str,
    timeout: float = 15000,
    keep=None
):
    """Return the distinct link texts on `page` (searched across every
    frame) that start with `prefix` but are longer than it — i.e. actual
    sub-items/child rows, not the prefix link itself.

    Used to walk Corp's Price Node drill-down, where each deeper
    level's links are just the parent's label with more text appended
    (e.g. "<offering> - Retail Price" -> "<offering> - Retail Price
    Business NRC").

    ``keep`` is an optional predicate to narrow the matches down further.
    It's applied inside the retry loop on purpose, so we keep waiting for
    the links we actually want instead of returning early just because
    some other link shares the prefix."""

    pattern = re.compile("^" + re.escape(prefix))

    end = time.monotonic() + timeout / 1000

    while True:

        names = []
        seen = set()

        for frame in page.frames:

            try:
                links = frame.get_by_role("link", name=pattern)
                count = await links.count()
            except Exception:
                continue

            for i in range(count):
                try:
                    text = (await links.nth(i).inner_text()).strip()
                except Exception:
                    continue

                if not text or text == prefix or text in seen:
                    continue

                if keep is not None and not keep(text):
                    continue

                seen.add(text)
                names.append(text)

        if names or time.monotonic() >= end:
            return names

        await page.wait_for_timeout(150)



# -------------------------
# OFFERING FILTER
# -------------------------

# The filter icon is a jQuery UI tab: an <a class="ui-tabs-anchor"> whose
# picture comes from a CSS background, so the anchor itself is all but
# empty. Its inline onclick calls showDiv(...), which is what builds the
# popup. The surrounding ids are generated per object
# ("t9128243994413226210_0____$1"), so anchor on the class and the
# background image instead — those are stable.
FILTER_INPUT_SELECTOR = 'input.inputs[name="inp"]'

FILTER_MARK = "data-pim-filter"

# Runs inside each frame. Every grid column has its own funnel, and the
# page also carries hidden copies (popup templates, other grids), so
# "the first filter-looking thing in the HTML" is often the wrong one —
# which is how the old lookup kept clicking something that never opened
# anything. Instead: score every candidate and mark the best one.
#   * visible on screen          — a hidden copy can't open a popup
#   * sits in the "Name" column  — that's the column searched by name
#   * sits in a header row       — funnels live in column headers
FILTER_ICON_FINDER = """
(mark) => {
  document.querySelectorAll('[' + mark + ']')
    .forEach(el => el.removeAttribute(mark));

  const text = (el) => ((el && el.textContent) || '')
    .replace(/\\s+/g, ' ').trim();

  const mentionsFilter = (el) => {
    const own = ['style', 'src', 'class', 'title', 'alt']
      .map(a => el.getAttribute(a) || '').join(' ').toLowerCase();
    if (own.includes('filter')) return true;
    const bg = (getComputedStyle(el).backgroundImage || '').toLowerCase();
    return bg.includes('filter');
  };

  const shown = (el) => {
    if (!el.isConnected) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    }
    // The funnel is often a zero-sized <a> painted by a background image,
    // so measure the nearest ancestor that actually takes up space.
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const r = n.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return true;
    }
    return false;
  };

  const pool = new Set();
  const selectors = [
    'a[onclick]', 'img', '[class*="filter" i]', '[title*="filter" i]',
    '[style*="filter" i]', 'th *', '[role="columnheader"] *'
  ];
  for (const sel of selectors) {
    try { document.querySelectorAll(sel).forEach(el => pool.add(el)); }
    catch (e) {}
  }

  const found = [];
  for (const el of pool) {
    let filterish = false;
    try { filterish = mentionsFilter(el); } catch (e) {}
    if (!filterish) continue;

    const cell = el.closest('th, td, [role="columnheader"]');
    const header = cell ? text(cell) : '';
    const visible = shown(el);

    let score = 0;
    if (visible) score += 100;
    if (/^name\\b/i.test(header)) score += 50;
    if (el.closest('th, thead, [role="columnheader"]')) score += 20;
    if (el.tagName === 'A' && el.getAttribute('onclick')) score += 5;
    if (el.closest('li[role="tab"]')) score += 3;

    found.push({el, score, visible, header,
                html: (el.outerHTML || '').slice(0, 300)});
  }

  found.sort((a, b) => b.score - a.score);
  if (found.length) found[0].el.setAttribute(mark, '1');

  return found.slice(0, 8).map(f => ({
    score: f.score, visible: f.visible, header: f.header.slice(0, 40),
    tag: f.el.tagName, html: f.html
  }));
}
"""


async def _locate_filter_icon(page: Page, timeout: float = 15000):
    """Find and mark the right filter funnel, in whichever frame holds it.

    Returns (locator, report) — report lists the best candidates of every
    frame, for the debug file when the popup still won't open."""

    end = time.monotonic() + timeout / 1000

    while True:

        best = None
        report = []

        for frame in page.frames:

            try:
                candidates = await frame.evaluate(
                    FILTER_ICON_FINDER, FILTER_MARK
                )
            except Exception:
                continue

            for c in candidates:
                report.append({"frame": frame.url, **c})

            if candidates and (best is None or candidates[0]["score"] > best[1]):
                best = (frame, candidates[0]["score"])

        # Only settle for a candidate that's actually on screen, unless
        # time's up and there's nothing better coming.
        if best is not None and (best[1] >= 100 or time.monotonic() >= end):
            frame = best[0]
            return frame.locator(f"[{FILTER_MARK}]").first, report

        if time.monotonic() >= end:
            return None, report

        await page.wait_for_timeout(300)


async def _find_filter_input(page: Page, timeout: float = 2500):
    """The popup's text box, or None if the popup isn't open."""

    try:
        return await locate_in_any_frame(
            page,
            lambda f: f.locator(FILTER_INPUT_SELECTOR).last,
            timeout=timeout
        )
    except Exception:
        return None


async def _popup_says_open(icon):
    """True if the funnel's own tab reports itself expanded — the popup is
    on its way even if its text box hasn't rendered yet."""

    try:
        return await icon.evaluate(
            "el => { const li = el.closest('li[role=\"tab\"]');"
            " return !!li && (li.getAttribute('aria-expanded') === 'true'"
            " || li.classList.contains('ui-tabs-active')); }"
        )
    except Exception:
        return False


async def _real_mouse_click(page: Page, icon):
    """A trusted mouse click at the funnel's position, done by the browser
    itself — no OS mouse, so it works headless and whatever window is on
    top. Aims at the nearest element that actually has a size."""

    await icon.scroll_into_view_if_needed(timeout=2000)

    box = await icon.bounding_box()

    if not box or box["width"] <= 0 or box["height"] <= 0:
        box = await icon.locator("xpath=..").bounding_box()

    if not box or box["width"] <= 0 or box["height"] <= 0:
        raise RuntimeError("the funnel has no size on screen")

    await page.mouse.click(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2
    )


def _write_filter_debug(report, screenshot_path):
    """Leave a note of what the page offered, next to the reports."""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(DOWNLOADS, f"{FILTER_DEBUG_PREFIX}_{stamp}.txt")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Filter candidates (best first, per frame):\n\n")
            for c in report:
                fh.write(
                    f"score={c.get('score')} visible={c.get('visible')} "
                    f"header={c.get('header')!r} tag={c.get('tag')}\n"
                    f"frame={c.get('frame')}\n"
                    f"html={c.get('html')}\n\n"
                )
            if screenshot_path:
                fh.write(f"screenshot: {screenshot_path}\n")
        print(f"📝 Filter debug notes written to: {path}")
    except Exception:
        pass


async def open_filter_popup(page: Page):
    """Open the filter popup from inside the page and return its input.

    No OS-level mouse involved: the funnel's own onclick is what opens
    the popup, so the click only has to reach that element. Legacy
    handlers are fussy about how a click arrives, so this escalates
    through a few ways of delivering one and stops the moment the popup's
    input actually appears — which is the only real proof it worked.

    Returns None if none of them opened it (and leaves debug notes)."""

    icon, report = await _locate_filter_icon(page)

    if icon is None:
        print("⚠ Filter icon not found in any frame.")
        _write_filter_debug(report, None)
        return None

    best = report and max(report, key=lambda c: c["score"])
    if best:
        print(
            f"🎯 Filter funnel picked: column {best['header']!r}, "
            f"visible={best['visible']}"
        )

    # A real trusted click goes first now that the right funnel is picked.
    # DOM click() stays right behind it: the funnel can be a zero-sized <a>
    # painted by a background image, which trusted clicks refuse to hit
    # but el.click() still fires.
    attempts = [
        ("mouse click", lambda: _real_mouse_click(page, icon)),
        ("DOM click()", lambda: icon.evaluate("el => el.click()")),
        ("dispatched click", lambda: icon.dispatch_event("click")),
        # Last resort: run the inline handler itself. Skips whatever
        # jQuery UI would have done, but showDiv() is what builds the
        # popup, so it's usually enough on its own.
        ("inline onclick", lambda: icon.evaluate(
            "el => { const code = el.getAttribute('onclick');"
            " if (code) { new Function(code).call(el); } }"
        )),
    ]

    for label, run in attempts:

        # A slow popup from the previous try may have turned up since —
        # clicking again now would just toggle it shut.
        search_box = await _find_filter_input(page, timeout=500)
        if search_box is not None:
            print("✅ Filter popup opened.")
            return search_box

        try:
            await run()
        except Exception as e:
            # Playwright errors carry a whole call log; one line is plenty.
            reason = str(e).strip().splitlines()[0]
            print(f"   ↳ {label} errored ({reason}) — trying the next way.")
            continue

        search_box = await _find_filter_input(page, timeout=3000)

        if search_box is None and await _popup_says_open(icon):
            # The popup is loading; give it longer instead of re-clicking.
            search_box = await _find_filter_input(page, timeout=8000)

        if search_box is not None:
            print(f"✅ Filter popup opened ({label}).")
            return search_box

        print(f"   ↳ {label} didn't open it — trying the next way.")

    shot = None
    try:
        shot = os.path.join(
            DOWNLOADS,
            f"{FILTER_DEBUG_PREFIX}_{datetime.now():%Y%m%d_%H%M%S}.png"
        )
        await page.screenshot(path=shot, full_page=False)
    except Exception:
        shot = None

    _write_filter_debug(report, shot)

    return None


async def filter_offering(
    page: Page,
    offering: str
):

    print(
        f"🔎 Searching offering: {offering}"
    )


    # -------------------------
    # OPEN FILTER POPUP
    # -------------------------

    print(
        "🖱 Opening the filter..."
    )

    search_box = await open_filter_popup(page)


    # Fall back to the old OS-level click at fixed screen coordinates.
    # It needs a real window, so it's headed-only — and it's only worth
    # trying because it costs a couple of seconds.
    if search_box is None and not HEADLESS:

        print(
            f"🖱 Falling back to a real mouse click at "
            f"({FILTER_ICON_SCREEN_X}, {FILTER_ICON_SCREEN_Y})..."
        )

        await bring_browser_to_foreground(page)
        await page.wait_for_timeout(200)

        pyautogui.moveTo(FILTER_ICON_SCREEN_X, FILTER_ICON_SCREEN_Y)
        pyautogui.click()

        search_box = await _find_filter_input(page, timeout=5000)


    if search_box is None:

        if HEADLESS:
            raise RuntimeError(
                f"Could not open the filter popup for \"{offering}\", and "
                f"with HEADLESS on there's no window to click by hand. "
                f"Set HEADLESS = False, or give this offering as a URL."
            )

        # Last resort: wait a long time in case it still needs a
        # manual click — don't fail the run over this.
        print(
            "⌨ Still not open — click the filter icon manually; "
            "waiting as long as it takes."
        )
        search_box = await locate_in_any_frame(
            page,
            lambda f: f.locator(FILTER_INPUT_SELECTOR).last,
            timeout=3600000
        )

    await search_box.fill(
        offering
    )


    print(
        "✅ Offering entered."
    )


    # -------------------------
    # CLICK APPLY
    # -------------------------

    print(
        "🖱 Looking for Apply button..."
    )


    apply_button = await locate_in_any_frame(
        page,
        lambda f: f.get_by_role(
            "link",
            name="Apply"
        ),
        timeout=120000
    )


    await apply_button.click(
        force=True
    )


    print(
        "✅ Apply clicked."
    )


    await page.wait_for_timeout(
        3000
    )



# -------------------------
# INPUT
# -------------------------

def read_multiline():

    print(
        """
Paste Server Details.
Finish with two empty lines.

"""
    )

    lines = []

    empty = 0


    while True:

        line = input()


        if line.strip() == "":

            empty += 1

            if empty == 2:
                break

        else:

            empty = 0


        lines.append(line)


    return "\n".join(lines)



def read_offerings():

    print(
        """
Paste offerings — either their names or their object URLs
(a URL is opened directly, no searching in TOMS).

    SAMSUNG GALAXY Z FOLD8 ULTRA 256GB
        or
    http://192.0.2.4:6300/ncobject.jsp?id=9176805427065294059

Finish with two empty lines.

"""
    )


    offerings = []

    empty = 0


    while True:

        line = input()


        if line.strip() == "":

            empty += 1

            if empty == 2:
                break

        else:

            empty = 0


        if line.strip():

            offerings.append(
                line.strip()
            )


    return offerings



# -------------------------
# SCREENSHOTS -> WORD
# -------------------------

# Word refuses every COM call while it has a modal dialog up (a "save your
# changes?" prompt left over from an earlier run, an activation notice,
# ...). pywin32 reports that refusal as a plain AttributeError on whatever
# property was being read — which is how a forgotten dialog used to kill a
# whole ticket with a cryptic "Open.Content". So instead of dying: notice
# it, say so plainly, and wait for it to be dismissed.
WORD_BLOCKED_ATTEMPTS = 60
WORD_BLOCKED_DELAY = 5


def _word_is_blocked():
    """True if Word has a modal dialog up — its document windows are
    disabled, so no automation call can get through."""

    blocked = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) != "OpusApp":
            return
        if not win32gui.IsWindowEnabled(hwnd):
            blocked.append(hwnd)

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        return False

    return bool(blocked)


def _word_action(body, description):
    """Run one Word/COM operation, surviving a Word that is temporarily
    refusing calls because a dialog is up.

    Anything that fails for any OTHER reason is re-raised immediately, so
    real errors still surface exactly like they used to."""

    pythoncom.CoInitialize()

    try:
        for attempt in range(WORD_BLOCKED_ATTEMPTS):

            try:
                return body()

            except Exception:

                if not _word_is_blocked():
                    raise

                # Once a minute, not every retry.
                if attempt % 12 == 0:
                    print(
                        f"⏳ Word has a dialog open, so it's refusing to "
                        f"{description}. Answer it in Word (it's usually "
                        f"\"Save your changes?\") — waiting..."
                    )

                time.sleep(WORD_BLOCKED_DELAY)

        # Still blocked after the full wait — let the caller deal with it.
        return body()

    finally:
        pythoncom.CoUninitialize()


def _get_word():

    try:
        word = win32com.client.GetActiveObject(
            "Word.Application"
        )
    except Exception:
        word = win32com.client.Dispatch(
            "Word.Application"
        )

    word.Visible = True
    return word


def _get_document(word, word_path):

    for doc in word.Documents:
        # A document can be in a broken/stale COM state (e.g. mid-close
        # from a previous call) — skip it instead of crashing the run.
        try:
            full_name = doc.FullName
        except Exception:
            continue

        if full_name.lower() == word_path.lower():
            return doc

    if os.path.exists(word_path):
        return word.Documents.Open(word_path)

    doc = word.Documents.Add()
    doc.SaveAs(word_path)
    return doc


def _save_word_document(word_path):
    """Write one finished report to disk and LEAVE IT OPEN in Word, so it
    can be read through as soon as the ticket is done.

    Not closing it is also what keeps Word out of trouble: a Close() pops
    "Save your changes?" whenever Word still thinks the document is
    dirty, and that modal dialog blocks every later COM call — which is
    what used to stall whole runs."""

    def body():
        word = _get_word()

        for doc in word.Documents:
            try:
                full_name = doc.FullName
            except Exception:
                continue

            if full_name.lower() == word_path.lower():
                doc.Save()
                break

    _word_action(body, "save the report")


def _read_word_text(word_path):
    """Return the full text of an existing report, or "" if the file
    doesn't exist yet. Used to figure out how far a previous (crashed or
    interrupted) run got, so a re-run can resume instead of starting the
    whole ticket over."""

    if not os.path.exists(word_path):
        return ""

    def body():
        word = _get_word()

        for doc in word.Documents:
            try:
                full_name = doc.FullName
            except Exception:
                continue

            if full_name.lower() == word_path.lower():
                return doc.Content.Text

        doc = word.Documents.Open(word_path)
        return doc.Content.Text

    return _word_action(body, "read the existing report")


def _resume_skip_count(existing_text, offerings):
    """How many offerings, counted from the start of `offerings` in
    order, are safe to skip on a resumed run.

    Conservative on purpose: an offering only counts as done if its own
    "<offering> - Characteristics" heading is in the text AND it isn't
    the last offering that shows any progress at all — that last one is
    always redone in full, since it's the most likely place a previous
    run crashed mid drill-down.

    Offerings given as URLs never match: the report holds the offering's
    real name, which is only known once its page is open. So a resumed
    run just redoes them — right, if slower."""

    last_seen_index = -1
    finished = []

    for i, offering in enumerate(offerings):
        if offering in existing_text:
            last_seen_index = i
        finished.append(f"{offering} - Characteristics" in existing_text)

    skip = 0
    for i in range(len(offerings)):
        if i < last_seen_index and finished[i]:
            skip += 1
        else:
            break

    return skip


def _add_text_block_to_word(word_path, text):
    """Insert a plain block of text. If the document is empty this lands
    at the very top (used for the Server Details header)."""

    def body():
        word = _get_word()
        doc = _get_document(word, word_path)

        # Word treats \r as a new paragraph.
        block = text.replace("\r\n", "\n").replace("\n", "\r")

        if doc.Content.End <= 1:
            rng = doc.Range(0, 0)
        else:
            rng = doc.Range(doc.Content.End - 1)
            rng.InsertParagraphAfter()
            rng = doc.Range(doc.Content.End - 1)

        rng.InsertAfter(block)
        rng.InsertParagraphAfter()

        doc.Save()

    _word_action(body, "add a text block")


def _add_heading_to_word(word_path, text):
    """Insert one bold heading — a title for the page whose screenshots
    follow it."""

    def body():
        word = _get_word()
        doc = _get_document(word, word_path)

        rng = doc.Range(doc.Content.End - 1)
        rng.InsertParagraphAfter()
        rng = doc.Range(doc.Content.End - 1)

        start = rng.Start
        rng.InsertAfter(text)

        heading = doc.Range(start, doc.Content.End - 1)
        heading.Font.Bold = True
        heading.Font.Size = 14

        tail = doc.Range(doc.Content.End - 1)
        tail.InsertParagraphAfter()
        tail = doc.Range(doc.Content.End - 1)
        tail.Font.Bold = False
        tail.Font.Size = 11

        doc.Save()

    _word_action(body, "add a heading")


def _add_image_to_word(image_path, word_path, delete=True):

    def body():
        word = _get_word()
        doc = _get_document(word, word_path)

        # Go to the end of the document.
        rng = doc.Range(doc.Content.End - 1)
        rng.InsertParagraphAfter()
        rng = doc.Range(doc.Content.End - 1)

        rng.InlineShapes.AddPicture(
            os.path.abspath(image_path)
        )

        doc.Save()

    _word_action(body, "add a screenshot")

    if delete:
        os.remove(image_path)


def _add_images_to_word(image_paths, word_path, delete=True):
    """Append several screenshots in one go, in the order given.

    One trip to Word and one Save for the whole batch, instead of a trip
    and a Save per picture — saving an ever-growing .docx after every
    single screenshot was one of the slowest parts of a run. None entries
    (blank or failed shots) are skipped."""

    paths = [p for p in image_paths if p]

    if not paths:
        return

    def body():
        word = _get_word()
        doc = _get_document(word, word_path)

        for path in paths:
            rng = doc.Range(doc.Content.End - 1)
            rng.InsertParagraphAfter()
            rng = doc.Range(doc.Content.End - 1)

            rng.InlineShapes.AddPicture(
                os.path.abspath(path)
            )

        doc.Save()

    _word_action(body, "add screenshots")

    if delete:
        for path in paths:
            try:
                os.remove(path)
            except Exception:
                pass


_png_counter = itertools.count()


def _temp_png():
    # The counter keeps names unique when several tabs shoot at once.
    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    return os.path.join(
        DOWNLOADS,
        f"temp_{timestamp}_{next(_png_counter)}.png"
    )


def _is_blank_image(path):
    """True if the screenshot is essentially empty (uniform colour, no
    real information) — so we don't add blank pages to the report."""
    try:
        img = Image.open(path).convert("L").resize((72, 72))
        lo, hi = img.getextrema()
        return (hi - lo) < 10
    except Exception:
        return False


def _images_similar(path_a, path_b):
    """True if two screenshots are basically the same view — happens when
    a scroll near the bottom barely moves, so we skip the duplicate."""
    try:
        a = Image.open(path_a).convert("L").resize((80, 80))
        b = Image.open(path_b).convert("L").resize((80, 80))
        diff = ImageChops.difference(a, b)
        changed = sum(1 for p in diff.getdata() if p > 24)
        return (changed / (80 * 80)) < 0.02
    except Exception:
        return False


async def capture_single(page: Page, title: str, word_path: str):
    """One heading + one screenshot of the current view."""

    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )
    except Exception:
        pass

    _add_heading_to_word(word_path, title)

    temp = _temp_png()
    await page.screenshot(path=temp, full_page=False)
    _add_image_to_word(temp, word_path)

    print(f"📸 {title}")


async def capture_full_page(
    page: Page,
    title: str,
    word_path: str,
    max_views=40
):
    """Add ONE heading for the page, then screenshot it top-to-bottom by
    scrolling, keeping only the views that actually show NEW information.

    Skips shots that are blank (whitespace) or nearly identical to the
    previous kept shot (e.g. a tiny scroll at the end of the page), so
    the report gets every screen of real content and nothing redundant."""

    _add_heading_to_word(word_path, title)

    kept = await capture_full_page_shots(page, title, max_views)

    # All of this page's views go into Word together, in order.
    _add_images_to_word(kept, word_path)


async def capture_full_page_shots(page: Page, title: str, max_views=40):
    """The screenshot half of capture_full_page: scroll the page top to
    bottom and return the files of the views worth keeping, in order,
    without touching Word — so several pages can be shot at once."""

    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )
    except Exception:
        pass

    await page.wait_for_timeout(400)

    await _scroll_to_top(page)
    await page.wait_for_timeout(250)

    kept = []

    for _ in range(max_views):

        temp = _temp_png()
        await page.screenshot(path=temp, full_page=False)

        skip = _is_blank_image(temp) or (
            kept and _images_similar(temp, kept[-1])
        )

        if skip:
            os.remove(temp)
        else:
            kept.append(temp)

        moved = await _scroll_down_one_view(page)
        await page.wait_for_timeout(350)

        if not moved:
            break

    print(f"📄 {title}: {len(kept)} screenshot(s)")

    return kept


def _write_items(items, word_path):
    """Write captured report items in order: ("H", heading text) or
    ("I", [screenshot files])."""

    for kind, value in items:
        if kind == "H":
            _add_heading_to_word(word_path, value)
        else:
            _add_images_to_word(value, word_path)



# -------------------------
# NAVIGATION + SCROLL CAPTURE
# -------------------------

PRICE_VARIANTS = [
    "Retail Price",
    "Installments",
    "Subsidize",
    "Internal",
    "Lifeline",
]


async def detect_offering_name(page: Page, timeout: float = 20000):
    """Read an offering's name off its already-open object page.

    When the offering comes in as a URL we never see its name in a search
    result, but we still need it: every price variant link on the page is
    the offering's name plus a known suffix (e.g. "<offering> - Retail
    Price"), and the whole drill-down is driven by that exact text. So we
    take the name straight from those links — that way it's guaranteed to
    match what the later lookups search for.

    Returns (name, source), where source is "links" when the name came
    from a price variant link — exact, and safe to build the later
    lookups on — or "title" when it's only the tab title, which is a
    guess. Returns (None, None) if even the title is empty."""

    suffixes = "|".join(re.escape(s) for s in PRICE_VARIANTS)
    pattern = re.compile(r"^(.+?)\s+-\s+(?:" + suffixes + r")\b")

    end = time.monotonic() + timeout / 1000

    while True:

        for frame in page.frames:

            try:
                links = frame.get_by_role("link", name=pattern)
                count = await links.count()
            except Exception:
                continue

            for i in range(count):

                try:
                    text = (await links.nth(i).inner_text()).strip()
                except Exception:
                    continue

                match = pattern.match(text)

                if match:
                    return match.group(1).strip(), "links"

        if time.monotonic() >= end:
            break

        await page.wait_for_timeout(200)

    # No price variant on the page (or it never rendered) — the tab title
    # is the next best thing.
    try:
        title = (await page.title()).strip()
    except Exception:
        title = ""

    return (title, "title") if title else (None, None)


async def _link_url(link):
    """The absolute address a link points at, or None if it only runs
    script (href="#", "javascript:...") and has to be clicked."""

    try:
        href = await link.evaluate("a => a.href || ''")
    except Exception:
        return None

    if href.lower().startswith(("http://", "https://")) and "#" not in href:
        return href

    return None


async def link_url_by_name(page: Page, name: str, timeout=15000):
    """Find a link by (partial) text across every frame and return where
    it points, without opening anything. None if it has no usable href."""

    link = await locate_in_any_frame(
        page,
        lambda f: f.get_by_role("link", name=name).first,
        timeout=timeout
    )

    return await _link_url(link)


async def links_present(page: Page, names, timeout=3000):
    """Which of these link texts are on the page.

    Checked by counting, not by waiting for each one to show: it waits
    (up to `timeout`) only until the page shows at least one of them,
    then answers for all at once — so a missing link costs nothing
    instead of a full timeout of its own."""

    end = time.monotonic() + timeout / 1000

    while True:

        found = set()

        for name in names:
            for frame in page.frames:
                try:
                    if await frame.get_by_role("link", name=name).count():
                        found.add(name)
                        break
                except Exception:
                    continue

        if found or time.monotonic() >= end:
            return found

        await page.wait_for_timeout(200)


# Held while a tab is being created. The Ctrl+click fallback below learns
# about its new tab by catching "the next tab the browser opens", so no
# other tab may be opened in the meantime or it would catch that one.
_tab_open_lock = asyncio.Lock()


async def open_url_tab(context, url):
    """Open an address in a fresh tab of the same (logged-in) session."""

    async with _tab_open_lock:
        new_page = await context.new_page()

    await new_page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    return new_page


async def open_in_new_tab(context, source_page, name, timeout=30000):
    """Find a link by (partial) text across every frame of source_page
    and open it in a NEW browser tab. Returns the new Page.

    Preferred path: read the link's address and open it ourselves — the
    same page a Ctrl+click would give, without waiting to find out
    whether the Ctrl+click opened anything. Fallback, for links that only
    run script: Ctrl+click and catch the new tab."""

    link = await locate_in_any_frame(
        source_page,
        lambda f: f.get_by_role("link", name=name).first,
        timeout=timeout
    )

    url = await _link_url(link)

    if url:
        return await open_url_tab(context, url)

    new_page = None

    try:
        async with _tab_open_lock:
            async with context.expect_page(timeout=10000) as new_page_info:
                await link.click(modifiers=["Control"])
            new_page = await new_page_info.value

    except Exception:
        # Fallback: open the href in a brand-new tab ourselves.
        href = await link.get_attribute("href")
        if not href:
            raise
        if href.startswith("/"):
            origin = await source_page.evaluate(
                "() => location.origin"
            )
            href = origin + href
        return await open_url_tab(context, href)

    try:
        await new_page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )
    except Exception:
        pass

    return new_page


async def _scroll_to_top(page: Page):
    """Send every frame (and the top document) back to the top."""

    for frame in page.frames:
        try:
            await frame.evaluate(
                """() => {
                    const el = document.scrollingElement
                        || document.documentElement;
                    if (el) el.scrollTop = 0;
                }"""
            )
        except Exception:
            continue


async def _scroll_down_one_view(page: Page):
    """Scroll down ~90% of a viewport. Corp renders content inside
    frames, so we scroll whichever frames are actually scrollable, not
    just the top document. Returns True if anything moved (there was more
    to scroll)."""

    moved = False

    for frame in page.frames:
        try:
            did = await frame.evaluate(
                """() => {
                    const el = document.scrollingElement
                        || document.documentElement;
                    if (!el) return false;
                    if (el.scrollHeight - el.clientHeight <= 2) return false;
                    const before = el.scrollTop;
                    el.scrollBy(0, Math.round(el.clientHeight * 0.9));
                    return el.scrollTop > before + 2;
                }"""
            )
            if did:
                moved = True
        except Exception:
            continue

    return moved


async def _scroll_to_bottom(page: Page, max_steps=40):
    """Keep scrolling one view at a time until nothing moves anymore."""

    for _ in range(max_steps):

        moved = await _scroll_down_one_view(page)
        await page.wait_for_timeout(250)

        if not moved:
            break


async def _scroll_up_small(page: Page, fraction=0.3):
    """Scroll up a bit less than one full view — used right after
    reaching the bottom, to re-reveal the tail end without losing all the
    context from the last screenshot."""

    for frame in page.frames:
        try:
            await frame.evaluate(
                """(fraction) => {
                    const el = document.scrollingElement
                        || document.documentElement;
                    if (!el) return;
                    el.scrollBy(0, -Math.round(el.clientHeight * fraction));
                }""",
                fraction
            )
        except Exception:
            continue


async def shot_to_file(page: Page, label: str = ""):
    """Screenshot the current view to a temp file WITHOUT touching Word —
    used where several tabs work at once and their pictures are written
    to the report afterwards, in order. Returns None for a blank view."""

    temp = _temp_png()
    await page.screenshot(path=temp, full_page=False)

    if _is_blank_image(temp):
        os.remove(temp)
        return None

    print(f"📸 {label}" if label else "📸 extra view")
    return temp


async def error_shot_to_file(page: Page, label: str):
    """shot_to_file for when something went wrong. Never raises."""

    try:
        return await shot_to_file(page, f"{label} - ERROR")
    except Exception:
        return None


async def capture_extra_view(page: Page, word_path: str, label: str = ""):
    """One screenshot appended with no heading — used for the extra
    top/bottom views taken on top of a capture_full_page() pass."""

    temp = _temp_png()
    await page.screenshot(path=temp, full_page=False)

    if _is_blank_image(temp):
        os.remove(temp)
        return

    _add_image_to_word(temp, word_path)
    print(f"📸 {label}" if label else "📸 extra view")


async def _safe_error_shot(page: Page, word_path: str, label: str):
    """Best-effort screenshot when something goes wrong (a crashed tab,
    an element that never showed up, ...). Never raises — if the page
    itself is dead there's nothing to capture, and that's fine."""

    try:
        await capture_extra_view(page, word_path, label=f"{label} - ERROR")
    except Exception:
        pass


async def click_same_tab(context, page: Page, name: str, timeout=30000):
    """Click a link, preferring in-place navigation (same tab). If the
    app forces a new tab anyway, adopt it and close the old one so we
    still end up with a single tab. Returns the page holding the content
    afterward."""

    link = await locate_in_any_frame(
        page,
        lambda f: f.get_by_role("link", name=name).first,
        timeout=timeout
    )

    # Watch for both outcomes at once and move on as soon as either one
    # happens. The old way always sat out a full 5s waiting for a new tab
    # that, for an in-place click, never comes — on every Price Value,
    # Parameters and Characteristics click of the run.
    #
    # Only windows opened BY THIS TAB count ("popup"): with several tabs
    # working at once, other tabs open pages all the time, and adopting
    # one of those would hijack someone else's work.
    new_pages = []
    navigated = []

    def on_popup(p):
        new_pages.append(p)

    def on_nav(frame):
        if frame.url and not frame.url.startswith(("about:", "data:")):
            navigated.append(frame)

    page.on("popup", on_popup)
    page.on("framenavigated", on_nav)

    try:
        await link.click()

        end = time.monotonic() + 5
        while not new_pages and not navigated and time.monotonic() < end:
            await asyncio.sleep(0.1)

    finally:
        page.remove_listener("popup", on_popup)
        page.remove_listener("framenavigated", on_nav)

    if navigated and not new_pages:
        try:
            await navigated[0].wait_for_load_state(
                "domcontentloaded",
                timeout=30000
            )
        except Exception:
            pass

    if new_pages:
        new_page = new_pages[0]

        try:
            await page.close()
        except Exception:
            pass

        try:
            await new_page.wait_for_load_state(
                "domcontentloaded",
                timeout=30000
            )
        except Exception:
            pass

        return new_page

    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )
    except Exception:
        pass

    await page.wait_for_timeout(600)
    return page


# -------------------------
# PER-OFFERING / PER-TICKET ORCHESTRATION
# -------------------------

# The tab the run touched last is deliberately left open at the end, so
# whatever was captured last is still there to look at. Closing is
# therefore always one step behind: finishing with a tab closes the
# PREVIOUS one and holds this one back. At most one extra tab is ever
# open, and the final one is never closed at all.
_held_tab = {"page": None}


async def close_tab_later(page):
    """Mark a tab as finished with. Closes the previously held tab, not
    this one."""

    previous = _held_tab["page"]
    _held_tab["page"] = page

    if previous is not None and previous is not page:
        try:
            await previous.close()
        except Exception:
            pass

# -------------------------
# PARALLEL PRICE NODE DRILL-DOWN
# -------------------------
#
# Everything under a price variant (its price nodes and their dated
# versions) is worked on several tabs at once. Each piece CAPTURES its
# screenshots to files and hands the list back instead of writing to Word
# itself; the caller then writes them in the original order. So the
# report reads exactly as before, it just gets there sooner.
#
# A "target" below is either an already-open Page or the URL to open one
# at. Passing URLs lets a child open its own tab only once it has a free
# slot, so no more than PARALLEL_TABS tabs are ever busy at the same time.

_tab_slots = None


def _slots():
    global _tab_slots
    if _tab_slots is None:
        _tab_slots = asyncio.Semaphore(max(1, PARALLEL_TABS))
    return _tab_slots


async def _open_target(context, target):
    if isinstance(target, str):
        return await open_url_tab(context, target)
    return target


async def _resolve_children(context, page: Page, names):
    """For each child link name, where to find it: its URL when it has
    one (opened later, by the child, in its own slot), or else a tab
    opened right now through the link — it has to be, since the parent
    page is about to be closed."""

    targets = []

    for child in names:
        try:
            url = await link_url_by_name(page, child, timeout=15000)
            if url:
                targets.append((child, url))
                continue

            tab = await open_in_new_tab(context, page, child, timeout=15000)
            targets.append((child, tab))

        except Exception:
            print(f"⚠ Could not open: {child}")

    return targets


async def _gather_in_order(coros, empty=list):
    """Run captures side by side; return their results in the order the
    coroutines were given. A capture that blew up anyway counts as
    empty() rather than sinking its siblings."""

    results = await asyncio.gather(*coros, return_exceptions=True)

    ordered = []
    for result in results:
        if isinstance(result, BaseException):
            print(f"⚠ A parallel capture failed: {result}")
            ordered.append(empty())
        else:
            ordered.append(result)

    return ordered


async def capture_price_node_detail(context, target, name: str):
    """The deepest level: a single dated Price Node. Screenshot it, then
    Price Value (same tab), screenshot, then Parameters (same tab),
    screenshot. Returns the shot files, in that order.

    Never raises — a crashed/unresponsive tab gets a best-effort
    screenshot and we move on instead of losing the whole run."""

    shots = []
    tab = None

    async with _slots():
        try:
            tab = await _open_target(context, target)

            shots.append(await shot_to_file(tab, name))

            tab = await click_same_tab(context, tab, "Price Value")
            shots.append(await shot_to_file(tab, f"{name} - Price Value"))

            tab = await click_same_tab(context, tab, "Parameters")
            shots.append(await shot_to_file(tab, f"{name} - Parameters"))

        except Exception as e:
            print(f"⚠ Problem on {name}: {e} — screenshot and continuing.")
            if tab is not None:
                shots.append(await error_shot_to_file(tab, name))

        finally:
            if tab is not None:
                await close_tab_later(tab)

    return [s for s in shots if s]


async def process_price_node_detail(context, tab: Page, name: str, word_path: str):
    """capture_price_node_detail, written straight to the report."""

    shots = await capture_price_node_detail(context, tab, name)
    _add_images_to_word(shots, word_path)


# A Price Node link is labelled one of two ways depending on the price
# variant, so matching on the offering prefix alone isn't enough — that
# would also pick up the page's own row and its siblings. These are the
# two shapes seen in TOMS:
#     "<variant> [CC: ...]"
#     "<variant> Else Price Node" / "<variant> Number Price Node [...]"
PRICE_NODE_MARKERS = ("Price Node", "[CC:")


def _is_price_node_name(name):
    return any(marker in name for marker in PRICE_NODE_MARKERS)


async def capture_price_node_versions(context, target, node_name: str):
    """One Price Node: screenshot it, then every dated version under it
    (e.g. "<node> : 01/01/1970"), those side by side. Returns the node's
    shot followed by each version's shots, in page order.

    Never raises — on any problem it takes a best-effort screenshot and
    moves on."""

    shots = []
    children = []
    node_tab = None

    async with _slots():
        try:
            node_tab = await _open_target(context, target)

            shots.append(await shot_to_file(node_tab, node_name))

            dated_names = await find_sub_item_names(node_tab, node_name)

            if not dated_names:
                print(f"⚠ No dated version found under: {node_name}")

            children = await _resolve_children(context, node_tab, dated_names)

        except Exception as e:
            print(f"⚠ Problem on {node_name}: {e} — screenshot and continuing.")
            if node_tab is not None:
                shots.append(await error_shot_to_file(node_tab, node_name))

        finally:
            if node_tab is not None:
                await close_tab_later(node_tab)

    # The slot is given back before the versions start, so they can use it.
    for child_shots in await _gather_in_order(
        capture_price_node_detail(context, child_target, child_name)
        for child_name, child_target in children
    ):
        shots.extend(child_shots)

    return [s for s in shots if s]


async def process_price_node_versions(
    context,
    node_tab: Page,
    node_name: str,
    word_path: str
):
    """capture_price_node_versions, written straight to the report."""

    shots = await capture_price_node_versions(context, node_tab, node_name)
    _add_images_to_word(shots, word_path)


async def process_price_node(
    context,
    sub_tab: Page,
    variant_name: str,
    sub_name: str,
    word_path: str
):
    """One Price Node sub-item (e.g. "<offering> - Retail Price Business
    NRC" or "<offering> - Lifeline NRC"): screenshot it, then walk EVERY
    Price Node it lists — each one in its own tab, each fully drilled
    down through its dated versions.

    Never raises — on any problem it takes a best-effort screenshot and
    moves on."""

    shots = await capture_price_node(context, sub_tab, variant_name, sub_name)
    _add_images_to_word(shots, word_path)


async def capture_price_node(context, target, variant_name: str, sub_name: str):
    """Capture side of process_price_node: the sub-item's own shot, then
    every Price Node under it with all their versions — the nodes worked
    side by side, their shots returned in page order.

    Never raises — on any problem it takes a best-effort screenshot and
    moves on."""

    shots = []
    children = []
    sub_tab = None

    async with _slots():
        try:
            sub_tab = await _open_target(context, target)

            shots.append(await shot_to_file(sub_tab, sub_name))

            # Price Nodes hang off the variant's name, not this sub-item's,
            # so search on the variant prefix and keep only the actual
            # Price Node rows — never this page's own row.
            node_names = await find_sub_item_names(
                sub_tab,
                variant_name,
                keep=lambda n: n != sub_name and _is_price_node_name(n)
            )

            if not node_names:
                print(f"⚠ No Price Node link found under: {sub_name}")
            else:
                print(f"\n🔢 {len(node_names)} price node(s) under {sub_name}")
                for node_name in node_names:
                    print(f"🖱 Opening price node detail: {node_name}")

            children = await _resolve_children(context, sub_tab, node_names)

        except Exception as e:
            print(f"⚠ Problem on {sub_name}: {e} — screenshot and continuing.")
            if sub_tab is not None:
                shots.append(await error_shot_to_file(sub_tab, sub_name))

        finally:
            if sub_tab is not None:
                await close_tab_later(sub_tab)

    for child_shots in await _gather_in_order(
        capture_price_node_versions(context, child_target, child_name)
        for child_name, child_target in children
    ):
        shots.extend(child_shots)

    return [s for s in shots if s]


async def process_price_variant(
    context,
    variant_tab: Page,
    variant_name: str,
    word_path: str
):
    """Drill-down for one already-captured price variant tab, written
    straight to the report: Characteristics, then every Price Node
    sub-item under it. Never raises."""

    _, drill_items = await capture_variant(
        context, variant_tab, variant_name, capture_page=False
    )
    _write_items(drill_items, word_path)


async def capture_variant(context, target, variant_name: str, capture_page=True):
    """Everything for one price variant, captured without touching Word.

    Returns two item lists, because the report puts them in different
    places: `page_items` (the variant page itself — written with the
    other variant pages, right after the offering) and `drill_items`
    (its Characteristics and every Price Node under it — written after
    the offering's own Characteristics). Variants are separate tabs, so
    several of them run at once.

    Never raises — on any problem it takes a best-effort screenshot and
    moves on."""

    page_items = []
    drill_items = []
    sub_item_targets = []
    variant_tab = None

    async with _slots():
        try:
            variant_tab = await _open_target(context, target)

            if capture_page:
                page_items.append(("H", variant_name))
                page_items.append((
                    "I",
                    await capture_full_page_shots(variant_tab, variant_name)
                ))

            sub_item_names = await find_sub_item_names(variant_tab, variant_name)

            for sub_name in sub_item_names:
                print(f"\n🖱 Opening price node: {sub_name}")

            # Worked out now, while the variant page is still showing them.
            sub_item_targets = await _resolve_children(
                context, variant_tab, sub_item_names
            )

            print(f"\n🖱 Opening Characteristics: {variant_name}")

            variant_tab = await click_same_tab(
                context,
                variant_tab,
                "Characteristics"
            )

            title = f"{variant_name} - Characteristics"
            drill_items.append(("H", title))
            drill_items.append((
                "I",
                await capture_full_page_shots(variant_tab, title)
            ))

        except Exception as e:
            print(f"⚠ Problem on variant {variant_name}: {e} — screenshot and continuing.")
            if variant_tab is not None:
                drill_items.append((
                    "I",
                    [await error_shot_to_file(variant_tab, variant_name)]
                ))

        finally:
            if variant_tab is not None:
                await close_tab_later(variant_tab)

    # Every sub-item (and everything under it) is worked side by side;
    # the report still gets them one after another, in page order.
    started = time.monotonic()

    for sub_shots in await _gather_in_order(
        capture_price_node(context, sub_target, variant_name, sub_name)
        for sub_name, sub_target in sub_item_targets
    ):
        drill_items.append(("I", sub_shots))

    if sub_item_targets:
        print(
            f"⏱ {variant_name}: price nodes done in "
            f"{time.monotonic() - started:.0f}s"
        )

    return page_items, drill_items


async def process_offering(context, search_page: Page, offering: str, word_path: str):
    """Process one offering, given either by name or by a direct URL to
    its object page.

    By name: filter to it on the (already open) search page and open the
    result. By URL: go straight there — the link already says exactly
    where the offering is, so the search is skipped entirely, and the
    offering's real name is read back off the page for the headings and
    the price variant lookups.

    From there on both paths are the same: capture the offering, open
    every price variant tab, wrap up the offering tab itself (tail-end
    views + Characteristics, then close it), and finally walk each price
    variant fully — one at a time, depth-first — before returning.

    Never raises — on any problem it takes a best-effort screenshot and
    moves on so the rest of the offerings still get processed."""

    offering_tab = None

    # Everything downstream (headings, "<name> - Retail Price", ...) works
    # off the offering's name. When we're given a URL we don't know it yet.
    name = offering

    try:
        if is_offering_url(offering):

            print(f"\n🔗 Opening offering by URL: {offering}")

            offering_tab = await context.new_page()

            await offering_tab.goto(
                offering,
                wait_until="domcontentloaded",
                timeout=120000
            )

            # The URL can point at a different TOMS host than the search
            # page, so this tab may need its own login. The page is
            # already loaded, so a login form would be there by now — a
            # short look is enough, instead of 5s on every offering.
            await login_if_needed(offering_tab, probe_timeout=2500)

            detected, source = await detect_offering_name(offering_tab)

            if source == "links":
                name = detected
                print(f"🏷 Offering name: {name}")

            elif detected:
                name = detected
                print(
                    f"⚠ No price variant links on this page — falling "
                    f"back to the tab title for the name: {name}. If "
                    f"that isn't the offering's real name, its price "
                    f"variants will be skipped."
                )

            else:
                print(
                    "⚠ Could not read the offering name off the page — "
                    "using the URL as the heading; price variants will "
                    "likely be skipped."
                )

        else:
            await filter_offering(
                search_page,
                offering
            )

            await capture_single(
                search_page,
                f"Search results: {offering}",
                word_path
            )

            print(f"\n🖱 Opening offering: {offering}")

            offering_tab = await open_in_new_tab(
                context,
                search_page,
                offering
            )

        await capture_full_page(
            offering_tab,
            name,
            word_path
        )

        # The offering page is fully loaded by now (it was just captured
        # top to bottom), so one quick look settles which variants exist —
        # a missing one used to cost 15s of waiting each.
        wanted = [f"{name} - {suffix}" for suffix in PRICE_VARIANTS]
        present = await links_present(offering_tab, wanted, timeout=3000)

        variant_targets = []

        for variant_name in wanted:

            print(f"\n🗂 Opening variant: {variant_name}")

            if variant_name not in present:
                print(
                    f"⚠ Variant not found (skipping): {variant_name}"
                )
                continue

            try:
                url = await link_url_by_name(
                    offering_tab, variant_name, timeout=15000
                )
                target = url or await open_in_new_tab(
                    context,
                    offering_tab,
                    variant_name,
                    timeout=15000
                )
            except Exception:
                print(
                    f"⚠ Variant not found (skipping): {variant_name}"
                )
                continue

            variant_targets.append((variant_name, target))

        # The offering's own tail end (bottom views + Characteristics) and
        # every variant (page, Characteristics, price nodes) are separate
        # tabs, so they all run at the same time. The report is written
        # afterwards in the usual order:
        #   variant pages -> offering bottom + Characteristics
        #   -> each variant's Characteristics and price nodes.
        async def offering_tail():
            nonlocal offering_tab

            items = []

            try:
                await _scroll_to_bottom(offering_tab)
                items.append(("I", [
                    await shot_to_file(offering_tab, f"{name}: bottom")
                ]))

                await _scroll_up_small(offering_tab)
                items.append(("I", [
                    await shot_to_file(
                        offering_tab, f"{name}: bottom (scrolled up)"
                    )
                ]))

                print(f"\n🖱 Opening Characteristics: {name}")

                offering_tab = await click_same_tab(
                    context,
                    offering_tab,
                    "Characteristics"
                )

                title = f"{name} - Characteristics"
                items.append(("H", title))
                items.append((
                    "I",
                    await capture_full_page_shots(offering_tab, title)
                ))

            except Exception as e:
                print(f"⚠ Problem on offering {name}: {e} — screenshot and continuing.")
                items.append(("I", [
                    await error_shot_to_file(offering_tab, name)
                ]))

            finally:
                await close_tab_later(offering_tab)

            return items

        tail_task = asyncio.ensure_future(offering_tail())

        variant_results = await _gather_in_order(
            (
                capture_variant(context, target, variant_name)
                for variant_name, target in variant_targets
            ),
            empty=lambda: ([], [])
        )

        tail_items = await tail_task

        for page_items, _ in variant_results:
            _write_items(page_items, word_path)

        _write_items(tail_items, word_path)

        for _, drill_items in variant_results:
            _write_items(drill_items, word_path)

    except Exception as e:
        print(f"⚠ Problem on offering {name}: {e} — screenshot and continuing.")
        if offering_tab is not None:
            await _safe_error_shot(offering_tab, word_path, name)
            try:
                await offering_tab.close()
            except Exception:
                pass
        else:
            await _safe_error_shot(search_page, word_path, name)


async def ensure_toms_session(search_page: Page, toms_state: dict):
    """Open + log into the TOMS/Corp object once, the first time a
    ticket actually needs to search for an offering. Kept lazy so the
    Jira ticket is always visited (and logged into) first, per ticket
    order.

    The environment comes from the pasted Server Details, never from a
    default — searching the wrong TOMS is exactly how offerings end up
    "not existing"."""

    if toms_state["ready"]:
        return

    object_url = toms_state.get("object_url")

    if not object_url:
        raise RuntimeError(
            "No usable TOMS address in the pasted Server Details, so "
            "there's nowhere to search offerings by name. Add a line "
            "like \"TOMS: http://192.0.2.4:6300\", or give the "
            "offerings as URLs."
        )

    print(
        f"\n🌐 Opening TOMS object: {object_url}"
    )

    await search_page.goto(
        object_url,
        wait_until="domcontentloaded",
        timeout=120000
    )

    await login_if_needed(
        search_page
    )

    await search_page.wait_for_timeout(
        1200
    )

    toms_state["ready"] = True


async def process_ticket(
    context,
    search_page: Page,
    ticket: str,
    server_details: str,
    toms_state: dict
):
    """Open one Jira ticket, auto-detect its offerings from the
    description, and build ONE Word report for the WHOLE ticket (title +
    server details put once at the top, then every offering's full
    screenshot trail, one after another) — saved and closed only once
    every offering is done."""

    safe_ticket = re.sub(
        r'[<>:"/\\|?*\r\n\t]',
        "_",
        ticket
    ).strip(" .")

    word_path = os.path.join(
        DOWNLOADS,
        f"{safe_ticket}.docx"
    )

    print(f"\n🎫 Opening ticket: {ticket}")

    ticket_page = await context.new_page()

    await ticket_page.goto(
        TMS_TICKET_URL + ticket,
        wait_until="domcontentloaded",
        timeout=60000
    )

    await login_tms_if_needed(ticket_page)

    await ticket_page.wait_for_timeout(900)

    title = await extract_jira_title(ticket_page)
    print(f"📌 Title: {title}")

    description = await extract_jira_description(ticket_page)
    offerings = extract_offerings_from_description(description)

    if not offerings:
        print(
            "⚠ Could not auto-detect offerings from the ticket "
            "description."
        )
        offerings = read_offerings()

    try:
        await ticket_page.close()
    except Exception:
        pass

    print("\nOfferings:")
    for o in offerings:
        print(f"  - {o}")

    # Offerings given as URLs are opened directly, so the search page is
    # only needed when at least one offering still has to be looked up by
    # name.
    if any(not is_offering_url(o) for o in offerings):
        await ensure_toms_session(search_page, toms_state)

    # Resume support: if this ticket's report already exists (from a
    # previous run that got interrupted/crashed), skip the offerings
    # already fully captured instead of redoing the whole ticket. The
    # last offering that shows any progress is always redone in full,
    # since that's the most likely place a previous run stopped.
    existing_text = _read_word_text(word_path)

    if existing_text:

        skip_count = _resume_skip_count(existing_text, offerings)

        if skip_count:
            print(
                f"📄 Existing report found for {ticket} — resuming, "
                f"skipping {skip_count} already-finished offering(s)."
            )

        offerings_to_run = offerings[skip_count:]

    else:

        heading = f"{ticket} - {title}" if title else ticket

        _add_heading_to_word(word_path, heading)

        if server_details:
            _add_text_block_to_word(word_path, server_details)

        offerings_to_run = offerings

    for offering in offerings_to_run:
        await process_offering(context, search_page, offering, word_path)

    _save_word_document(word_path)
    print(f"💾 Saved (left open in Word): {word_path}")

    # What the loader pass needs to carry on without asking anything
    # again.
    return ticket, title, offerings



# -------------------------
# THE LOADER PASS
# -------------------------

async def run_excel_screenshots(context, raw_text, collected):
    """Hand the finished tickets to Excel_Screenshots.py.

    Shared by this script and AM_Validation, so the two trigger it the
    same way. `collected` is the (ticket, title, products) of every
    ticket that got through, and `raw_text` is the block pasted at the
    start — between them the loader pass knows everything it needs, so
    nothing is asked twice.

    Imported here rather than at the top: Excel_Screenshots imports this
    module, and importing it back at module level would be a loop. It
    also means a problem in that script can't stop this one from
    running."""

    if not RUN_EXCEL_SCREENSHOTS:
        return

    if not collected:
        return

    try:
        import Excel_Screenshots as excel
    except Exception as e:
        print(
            f"⚠ Couldn't load Excel_Screenshots.py ({e}) — the TOMS "
            "screenshots are saved; the loader ones were skipped."
        )
        return

    try:
        await excel.continue_from_validation(context, raw_text, collected)
    except Exception as e:
        print(
            f"⚠ The loader screenshots stopped: {e} — the reports so far "
            "are still saved."
        )



# -------------------------
# MAIN
# -------------------------

async def main():


    raw_text = read_multiline()


    toms = extract_toms(
        raw_text
    )


    tickets = extract_tickets(
        raw_text
    )


    print("\n================")
    print("TOMS:")
    print(toms)


    if not toms:

        print(
            "❌ TOMS not detected."
        )

        return


    # Every search happens on THIS environment, not a built-in one.
    object_url = resolve_object_url(toms, raw_text)

    if object_url:
        print(f"Search URL: {object_url}")
    else:
        print(
            "⚠ Couldn't read an address out of that TOMS — offerings "
            "given by name can't be searched. Offerings given as URLs "
            "will still work."
        )


    print("\nTickets:")

    for t in tickets:
        print(t)


    if not tickets:

        print(
            "❌ No tickets/links detected."
        )

        return



    # Server Details is one shared block (the environment used for this
    # whole batch), pasted once and reused in every ticket's report.
    server_details = extract_server_details(raw_text)


    async with async_playwright() as pw:


        browser = await pw.chromium.launch(
            headless=HEADLESS
        )


        context = await browser.new_context()


        search_page = await context.new_page()

        # TOMS is only opened/logged into the first time a ticket needs
        # it (inside process_ticket), so the very first thing that
        # happens is always: open the Jira ticket, then log into it.
        toms_state = {"ready": False, "object_url": object_url}


        collected = []

        for ticket in tickets:
            try:
                collected.append(
                    await process_ticket(
                        context,
                        search_page,
                        ticket,
                        server_details,
                        toms_state
                    )
                )
            except Exception as e:
                print(
                    f"⚠ Problem on ticket {ticket}: {e} — "
                    "moving on to the next ticket."
                )


        await run_excel_screenshots(context, raw_text, collected)


        print(
            """
🎉 Done.

Browser and Word left open — the last tab and every report are still
there to look through.
"""
        )


        await asyncio.sleep(
            9999999
        )



if __name__ == "__main__":

    asyncio.run(main())