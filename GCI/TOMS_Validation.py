"""TOMS_Validation.py - runs OE test-case tickets (GCI.QA | BOT-xxxx | TC-nn)
by itself and builds the POT.

Paste the same thing as for PIM/AM validation - ticket links plus the
server block - and finish with two empty lines:

    https://jira.example.com/browse/VALPRJ-118014
    https://jira.example.com/browse/VALPRJ-114805

    TOMS : http://192.0.2.1:6300/
    GCI_MS_POLARIS.ci.DEV....zip. 2026-11-06T00:07:14Z(stable)

    RBM : 192.0.2.5
    GCI.RBM.DEV....zip. 2026-11-05T16:41:08Z(stable)

How it works
------------
* Every ticket's "Test Steps" are read from TMS. Each step is matched with
  regexes against the ACTIONS table below; one step can fire several
  functions ("Create SO2, return X ... and replace A with B").
* A ticket that says "Reuse Test data of TC no 03" (or simply starts with
  the same steps as another pasted ticket) CONTINUES that ticket's
  customer instead of starting over, and its POT starts with the same
  screenshots - exactly how the POTs are done by hand.
* Independent tickets run at the same time, each in its own browser
  window. Serial numbers are looked up in the warehouse in parallel tabs.
* When a step matches nothing, or a function fails, the run PAUSES on that
  ticket: do it by hand in that window and press Enter (it screenshots and
  carries on), or r = retry, q = stop. Every miss is appended to
  Downloads\\toms_validation_misses.txt with a screenshot + the page HTML, so
  the function can be taught and the run repeated.

Usage
-----
    python TOMS_Validation.py            run
    python TOMS_Validation.py --plan     only show step -> function mapping
    python TOMS_Validation.py --offline  paste the ticket TEXT itself (no TMS,
                                      no TOMS) and show the mapping
    python TOMS_Validation.py --serial   one ticket at a time
    python TOMS_Validation.py --no-bill  skip the RBM part
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import Internal_Env_Test as iet          # ticket parser (steps, devices)

DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")

# ========= CONFIG - the things a ticket never says =========

VIEWPORT = {"width": 1920, "height": 910}       # same size as the manual POTs

CIM_PATH = "/common/uobject.jsp?tab=_Residential+Customers&object=9134256259513156680&runsearch=true"
BCA_PATH = "/common/uobject.jsp?tab=_Business+Customers&object=9134256259513156680&runsearch=true"

STORE_NAME = "Dimond 1"                         # In Store delivery
WAREHOUSES = {                                  # object ids are the same on every TOMS
    "Dimond 1": "/ncobject.jsp?id=9147568464913900961",
    "Mail Fulfillment": "/ncobject.jsp?id=9147480535213897386",
}

CUSTOMER_ADDRESS = "1800 ARCTIC"                # typed in the address box, first hit is taken
CUSTOMER_PIN = "13579"
DISTRIBUTION_CHANNEL = "Residential Customer Service"

SIM_TYPE = "eSIM"                               # as in the manual POTs
SIM_PROVISIONING_DEVICE = "GCI Equipment"

INTERNET_EQUIPMENT_TAB = "Cable Modem"
INTERNET_EQUIPMENT_MODEL = "HITRON CDA-3-35"
INTERNET_UNCHECK = ["AK-Fi Home Wi-Fi"]

RETURN_REASON = "Trial"
DISCONNECT_REASON = "Service Disconnect"

# "apply associated Installment device discounts" - the ticket never says
# which one. Device name -> discount label as shown in OE. A device that is
# not here makes the run pause and ask (teach it once, add it here).
DEVICE_DISCOUNTS = {
    # "FRANKLIN WIRELESS MIFI RG2100 5G": "$100 Off Discount 36",
}

RBM_DIR = "QA"
RBM_SCRIPT_GLOB = "BC*.sh"
RBM_MENU_CHOICE = "1"

CAPTIONS = False            # the POT is title + server details + screenshots, nothing else
MISS_LOG = os.path.join(DOWNLOADS, "toms_validation_misses.txt")
STATE_DIR = os.path.join(DOWNLOADS, "toms_validation_state")

# ============================================================


class NeedsHuman(Exception):
    """An action that knows it cannot finish alone (says what is missing)."""


@dataclass
class Unit:
    """One piece of hardware on the order."""
    name: str
    mode: str = ""
    note: str = ""
    where: str = ""          # tree node it hangs from
    serial: str = ""
    color: str = ""
    catalog: str = ""        # the name as TOMS spells it
    stock_tab: str = ""
    stock_shot: str = ""


@dataclass
class Ctx:
    ticket: str
    title: str
    origin: str
    rbm_host: str
    server_details: str
    page: object = None
    context: object = None
    shots: list = field(default_factory=list)       # png paths, in POT order
    inherited: int = 0                              # how many came from the base ticket
    customer_url: str = ""
    customer_name: str = ""
    billing_account: str = ""
    so_count: int = 0
    lines: list = field(default_factory=list)       # tree labels of the mobile lines
    units: dict = field(default_factory=dict)       # device name -> Unit
    store_node: str = ""
    notes: list = field(default_factory=list)       # things the summary must say
    done: int = 0                                   # steps finished
    bill: bool = True

    @property
    def word_path(self):
        return os.path.join(DOWNLOADS, f"{self.ticket}.docx")

    def log(self, msg):
        print(f"[{self.ticket}] {msg}", flush=True)


# ------------------------------------------------------------------
# INPUT
# ------------------------------------------------------------------

def extract_origin(raw):
    m = re.search(r"TOMS\s*:\s*(https?://[^\s/]+|[\d.]+(?::\d+)?)", raw, re.I)
    if not m:
        return ""
    v = m.group(1)
    if "://" not in v:
        v = "http://" + v
    if not re.search(r":\d+$", v):
        v += ":6300"
    return v.rstrip("/")


def extract_rbm(raw):
    m = re.search(r"RBM\s*:\s*(?:https?://)?([\d.]+|[\w.-]+\.\w+)", raw, re.I)
    return m.group(1) if m else ""


def extract_server_block(raw):
    """Everything from the TOMS line down, without the ticket links."""
    lines = raw.splitlines()
    start = next((i for i, l in enumerate(lines) if re.search(r"\bTOMS\s*:", l, re.I)), None)
    if start is None:
        return ""
    if start and re.search(r"server|trunk|branch", lines[start - 1], re.I):
        start -= 1
    keep = [l for l in lines[start:] if "jira.example.com" not in l.lower()]
    return "\n".join(keep).strip()


def short_title(title):
    """'[GCI.QA] | BOT-1234 | TC-03 | OE | Validation of ...' -> 'Validation of ...'"""
    return (title or "").split("|")[-1].strip()


# ------------------------------------------------------------------
# STEP -> FUNCTIONS
# ------------------------------------------------------------------

ACTIONS = []        # (name, compiled regex, coroutine)


def action(pattern):
    def wrap(fn):
        ACTIONS.append((fn.__name__, re.compile(pattern, re.I), fn))
        return fn
    return wrap


def match_step(text):
    """Every action whose regex hits this step, in the order they appear
    in the sentence. [] means: nobody knows this step."""
    hits = []
    for name, rx, fn in ACTIONS:
        m = rx.search(text)
        if m:
            hits.append((m.start(), name, fn, m))
    hits.sort(key=lambda h: h[0])
    return [(name, fn, m) for _, name, fn, m in hits]


def names_in(text):
    """Shouty device names in a free sentence: 'Disconnect A B and C D.'"""
    out = []
    for chunk in re.split(r"\s+and\s+|,", text):
        toks = [t.strip(".;:") for t in chunk.split()]
        run = []
        for t in toks:
            if t and t == t.upper() and any(c.isalnum() for c in t):
                run.append(t)
            elif run:
                break
        if run:
            out.append(" ".join(run))
    return out


def device_rx(name, whole=True):
    """Regex for a device as the catalogue spells it. Tickets shorten
    names ("OB HOMEPOD MINI 2" is "OB APPLE HOMEPOD MINI 2" in TOMS), so
    one extra word is allowed between two given words - but never in
    front, which keeps "APPLE TV 4K 2026" away from "OB APPLE TV 4K 2026"."""
    body = r"\s+(?:\S+\s+)??".join(re.escape(t) for t in name.split())
    return re.compile(r"^\s*" + body + (r"\s*$" if whole else r"(?=\s|$)"), re.I)


def device_core(name):
    """What to type in a search box: the name without OB/CPO."""
    return re.sub(r"^(OB|CPO)\s+", "", name.strip(), flags=re.I)


def build_chains(cases):
    """Group tickets: a ticket that reuses another one's data runs right
    after it on the same customer. Returns [[case, case...], ...] where
    each inner list is (case, steps_to_skip)."""

    def norm(s):
        return re.sub(r"\W+", " ", s.text).strip().lower()

    by_tc = {c.tc: c for c in cases if c.tc}
    parent = {}

    for c in cases:
        for n in c.notes:
            m = re.search(r"reuse\s+test\s+data\s+of\s+TC\s*(?:no\.?)?\s*(\d+)", n, re.I)
            if m:
                key = f"TC-{int(m.group(1)):02d}"
                if key in by_tc and by_tc[key] is not c:
                    parent[c.ticket] = by_tc[key]
        if c.ticket in parent:
            continue
        for other in cases:           # same opening steps = same data
            if other is c or len(other.steps) >= len(c.steps):
                continue
            if all(norm(a) == norm(b) for a, b in zip(other.steps, c.steps)):
                parent[c.ticket] = other
                break

    chains = []
    placed = set()
    for c in cases:
        if c.ticket in parent or c.ticket in placed:
            continue
        chain = [(c, 0)]
        placed.add(c.ticket)
        cur = c
        while True:
            nxt = next((x for x in cases if parent.get(x.ticket) is cur and x.ticket not in placed), None)
            if not nxt:
                break
            skip = 0
            for a, b in zip(cur.steps, nxt.steps):
                if norm(a) != norm(b):
                    break
                skip += 1
            chain.append((nxt, skip))
            placed.add(nxt.ticket)
            cur = nxt
        chains.append(chain)
    for c in cases:                   # orphans whose parent was not pasted
        if c.ticket not in placed:
            chains.append([(c, 0)])
    return chains


def print_plan(chains):
    missing = 0
    for chain in chains:
        for case, skip in chain:
            print(f"\n=== {case.tc or '?'} | {case.ticket} | {short_title(case.title)}")
            for n in case.notes:
                print(f"    note: {n}")
            for i, s in enumerate(case.steps):
                hits = match_step(s.text)
                tag = "(done by previous ticket) " if i < skip else ""
                if hits:
                    print(f"  {i + 1:>2}. {tag}{s.text}\n        -> " + ", ".join(h[0] for h in hits))
                else:
                    missing += 1
                    print(f"  {i + 1:>2}. {tag}{s.text}\n        -> ??? NO FUNCTION - the run will pause here")
    print(f"\n{missing} step(s) without a function." if missing else "\nEvery step has a function.")
    return missing


# ------------------------------------------------------------------
# PAGE HELPERS
# ------------------------------------------------------------------

def rx_text(text):
    """Regex source for a text whatever the page uses between its words
    (TOMS writes most labels and options with non-breaking spaces)."""
    return r"\s+".join(re.escape(t) for t in text.split())


def _rx_exact(text):
    return re.compile(r"^\s*" + rx_text(text) + r"\s*\*?\s*:?\s*$", re.I)


def clean(text):
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def T(scope, text, exact=True):
    """Visible element showing this text (the smallest one)."""
    loc = scope.get_by_text(_rx_exact(text)) if exact else scope.get_by_text(text)
    return loc.locator("visible=true")


async def busy_wait(page, timeout=120000):
    """Sit out the 'Loading...' veil OE throws on every click."""
    veil = page.get_by_text(re.compile(r"^\s*Loading\.\.\.\s*$")).locator("visible=true")
    await page.wait_for_timeout(400)
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        try:
            if await veil.count() == 0:
                return
        except Exception:
            return
        await page.wait_for_timeout(350)
    raise TimeoutError("still 'Loading...' - the page never came back")


async def click(page, loc, wait=True):
    await loc.first.scroll_into_view_if_needed(timeout=15000)
    await loc.first.click(timeout=15000)
    if wait:
        await busy_wait(page)


FIELD_CSS = "input.refsel_input, select, input[type='text'], textarea"


async def field_of(scope, label, css=FIELD_CSS):
    """The control that belongs to a label ('First Name', 'IMEI'...).

    Corp forms put the label in one cell and its control in the
    next one; anything built differently falls back to "the first control
    after that text"."""
    lab = scope.locator(".nc-field-label:visible, .gwt-Label:visible").filter(has_text=_rx_exact(label)).last
    try:
        await lab.wait_for(state="visible", timeout=8000)
        cell = lab.locator("xpath=ancestor::td[1]/following-sibling::td[1]")
        ctl = cell.locator(css).first
        if await ctl.count():
            return ctl
    except Exception:
        pass
    lab = T(scope, label).last
    await lab.wait_for(state="visible", timeout=8000)
    return lab.locator(
        "xpath=following::*[(self::input and not(@type='hidden') and not(@type='checkbox')"
        " and not(@type='radio') and not(@type='button')) or self::select or self::textarea][1]"
    )


async def fill(scope, label, value):
    f = await field_of(scope, label)
    await f.click()
    await f.fill("")
    await f.type(value, delay=15)
    return f


async def pick_ref(page, inp, option, log=None):
    """A Corp reference selector: type, wait for its list, click the
    row. Improvises when the wanted text is not there letter by letter:
    a single row is taken as is, otherwise the closest one."""
    rows = page.locator("div.refsel_row:visible")
    for attempt in ("type", "arrow", "short"):
        await inp.click()
        if attempt == "type":
            await inp.fill("")
            await inp.type(option, delay=25)
        elif attempt == "arrow":
            await inp.locator("xpath=following-sibling::i[contains(@class,'refsel_arrow')]").click()
        else:
            await inp.fill("")
            await inp.type(option.split()[0], delay=40)
        try:
            await rows.first.wait_for(state="visible", timeout=12000)
            break
        except Exception:
            continue
    else:
        raise NeedsHuman(f"the list for '{option}' never opened")
    texts = [clean(t) for t in await rows.all_inner_texts()]
    want = clean(option).lower()
    idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
    if idx is None:
        idx = next((i for i, t in enumerate(texts) if want in t.lower()), None)
    if idx is None:
        words = set(want.split())
        idx = max(range(len(texts)), key=lambda i: len(words & set(texts[i].lower().split())))
        if log:
            log(f"   '{option}' is not in the list as such - took '{texts[idx]}' of {texts[:6]}")
    await rows.nth(idx).click()
    return texts[idx]


async def choose(page, scope, label, option, log=None):
    """Set a field to an option, whether it is a <select> or one of the
    Corp reference selectors."""
    f = await field_of(scope, label)
    if await f.evaluate("el => el.tagName") == "SELECT":
        opts = await f.evaluate("el => Array.from(el.options).map(o => o.text)")
        want = clean(option).lower()
        hit = next((o for o in opts if clean(o).lower() == want), None) \
            or next((o for o in opts if want in clean(o).lower()), None)
        if hit is None:
            raise NeedsHuman(f"'{label}' has no option like '{option}' (has: {[clean(o) for o in opts]})")
        await f.select_option(label=hit)
        picked = clean(hit)
    else:
        picked = await pick_ref(page, f, option, log)
    await busy_wait(page)
    return picked


async def tick(page, label, want=True, scope=None):
    """Tick the checkbox/radio drawn just left of a label (offers in the
    Overview, discounts, 'eSIM', 'Instant Return'...)."""
    lab = T(scope or page, label).first
    await lab.wait_for(state="visible", timeout=15000)
    await lab.scroll_into_view_if_needed()
    box = lab.locator(
        "xpath=(preceding::*[self::input[@type='checkbox' or @type='radio'] or "
        "contains(translate(@class,'CHECK','check'),'check') or @role='checkbox' or @role='radio'])[last()]"
    )
    try:
        state = await box.evaluate(
            "el => el.checked === true || el.getAttribute('aria-checked') === 'true' || /checked|selected/i.test(el.className)"
        )
        if bool(state) == want:
            return
        await box.click(timeout=5000)
    except Exception:
        b = await lab.bounding_box()           # the box sits ~11px left of its label
        await page.mouse.click(b["x"] - 11, b["y"] + b["height"] / 2)
    await busy_wait(page)


async def open_node(page, label):
    """Click a node of the left tree ('red Unlimited #6580', 'Summit Unlimited Line #6453')."""
    node = page.get_by_text(re.compile(r"^\s*" + re.escape(label))).locator("visible=true").first
    await click(page, node)


async def node_label(page, prefix, last=True):
    """Full text of a tree node that starts with prefix ('In Store #6499 | Dimond 1')."""
    loc = page.get_by_text(re.compile(r"^\s*" + re.escape(prefix) + r"\s*#\d+")).locator("visible=true")
    await loc.first.wait_for(state="visible", timeout=20000)
    el = loc.last if last else loc.first
    return re.sub(r"\s+", " ", (await el.inner_text())).strip()


async def shot(ctx, page=None, label=""):
    page = page or ctx.page
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{ctx.ticket}_{len(ctx.shots):03d}_{datetime.now():%H%M%S%f}.png")
    await page.screenshot(path=path, full_page=False)
    ctx.shots.append(path)
    ctx.log(f"   shot {len(ctx.shots)}" + (f" - {label}" if label else ""))
    return path


async def shot_scrolling(ctx, page=None, label="", max_views=12):
    """Screenshot a long classic page top to bottom."""
    page = page or ctx.page
    await page.evaluate("window.scrollTo(0, 0)")
    seen = -1
    for _ in range(max_views):
        await shot(ctx, page, label)
        y = await page.evaluate("() => { window.scrollBy(0, window.innerHeight - 80); return window.scrollY; }")
        if y == seen:
            break
        seen = y
        await page.wait_for_timeout(350)


# ------------------------------------------------------------------
# REPORT (python-docx: no Word window, safe from several tickets at once)
# ------------------------------------------------------------------

def write_report(ctx):
    from docx import Document
    from docx.shared import Cm, Pt

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(1.5)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    p = doc.add_paragraph()
    p.add_run(f"{ctx.ticket} - {short_title(ctx.title)}").bold = True
    for line in ctx.server_details.splitlines():
        doc.add_paragraph(line).paragraph_format.space_after = Pt(0)

    width = sec.page_width - sec.left_margin - sec.right_margin
    for path in ctx.shots:
        if os.path.exists(path):
            doc.add_picture(path, width=width)

    try:
        doc.save(ctx.word_path)
    except PermissionError:
        alt = ctx.word_path.replace(".docx", f"_{datetime.now():%H%M%S}.docx")
        doc.save(alt)
        ctx.log(f"   {os.path.basename(ctx.word_path)} is open in Word - saved as {os.path.basename(alt)}")


def save_state(ctx):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {k: getattr(ctx, k) for k in (
        "ticket", "customer_url", "customer_name", "billing_account", "so_count",
        "lines", "store_node", "notes", "done", "shots")}
    data["units"] = {k: vars(v) for k, v in ctx.units.items()}
    with open(os.path.join(STATE_DIR, f"{ctx.ticket}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


# ------------------------------------------------------------------
# WAREHOUSE - find a free serial for a model
# ------------------------------------------------------------------

_claimed = set()                # serials already taken by any ticket of this run
_claim_lock = asyncio.Lock()


async def _filter_item_model(pim, page, model):
    """Open the funnel of the 'Item Model' column and filter by model."""
    finder = pim.FILTER_ICON_FINDER.replace("/^name\\b/i", "/^item model/i")
    for frame in page.frames:
        try:
            found = await frame.evaluate(finder, pim.FILTER_MARK)
        except Exception:
            continue
        if not found or found[0]["score"] < 100:
            continue
        icon = frame.locator(f"[{pim.FILTER_MARK}]").first
        for how in ("click", "dom"):
            try:
                if how == "click":
                    await icon.click(timeout=3000)
                else:
                    await icon.evaluate("el => el.click()")
            except Exception:
                continue
            box = await pim._find_filter_input(page, timeout=4000)
            if box is not None:
                await box.fill(model)
                apply = await pim.locate_in_any_frame(
                    page, lambda f: f.get_by_role("link", name="Apply"), timeout=15000)
                await apply.click(force=True)
                await page.wait_for_timeout(3000)
                return True
    return False


async def find_stock(ctx, unit, model=None, warehouse=None):
    """Fill unit.serial (and colour) with an Available item of that model.
    Runs in its own tab, so several can go at once."""
    import PIM_Validation as pim

    model = model or unit.name
    page = await ctx.context.new_page()
    try:
        await page.goto(ctx.origin + WAREHOUSES[warehouse or STORE_NAME],
                        wait_until="domcontentloaded", timeout=120000)
        await pim.login_if_needed(page, probe_timeout=2500)

        for tab in ("Stock Items with IMEI/MEID", "Stock Items with Serial Number"):
            link = await pim.locate_in_any_frame(
                page, lambda f, t=tab: f.get_by_role("link", name=t, exact=True).first, timeout=30000)
            await link.click()
            await page.wait_for_timeout(2500)

            if not await _filter_item_model(pim, page, device_core(model)):
                ctx.log(f"   (warehouse filter did not open for '{model}' - reading the page as is)")

            rows = []
            for frame in page.frames:
                try:
                    rows += await frame.evaluate(
                        """() => Array.from(document.querySelectorAll('tr')).map(tr =>
                            Array.from(tr.children).map(td => (td.innerText || '').trim())
                        ).filter(c => c.length > 3 && c.some(x => x === 'Available'))""")
                except Exception:
                    continue

            rx = device_rx(model, whole=False)      # the stock name carries the colour at the end
            async with _claim_lock:
                for cells in rows:
                    serial = next((c for c in cells if re.fullmatch(r"\d{12,}", c)), "")
                    hit = next((rx.match(c) for c in cells if rx.match(c)), None)
                    if hit and serial and serial not in _claimed:
                        _claimed.add(serial)
                        unit.serial = serial
                        unit.color = hit.string[hit.end():].strip()
                        unit.stock_tab = tab
                        break

            if unit.serial:
                path = os.path.join(STATE_DIR, f"{ctx.ticket}_stock_{unit.serial}.png")
                os.makedirs(STATE_DIR, exist_ok=True)
                await page.screenshot(path=path, full_page=False)
                unit.stock_shot = path
                ctx.log(f"   stock: {model} -> {unit.serial} {unit.color}")
                return unit
        raise NeedsHuman(f"no Available '{model}' in {warehouse or STORE_NAME} - load stock or give a serial")
    finally:
        await page.close()


async def prefetch_stock(ctx, units):
    """All serials at once, before OE needs them."""
    todo = [u for u in units if not u.serial]
    results = await asyncio.gather(*(find_stock(ctx, u) for u in todo), return_exceptions=True)
    for u, r in zip(todo, results):
        if isinstance(r, Exception):
            ctx.log(f"   stock lookup failed for {u.name}: {r}")


# ------------------------------------------------------------------
# OE BUILDING BLOCKS
# ------------------------------------------------------------------

async def ensure_store(ctx):
    """Delivery Methods > In Store, with the store set (once per SO)."""
    page = ctx.page
    if ctx.store_node:
        return
    await click(page, T(page, "Overview"))
    await tick(page, "In Store")
    label = await node_label(page, "In Store")
    await open_node(page, label)
    await choose(page, page, "Store", STORE_NAME)
    ctx.store_node = await node_label(page, "In Store")
    await shot(ctx, label="delivery method")


async def add_equipment(ctx, unit, tab=None, model_option=None):
    """On the node that is open: search the device, Add, sale type/colour,
    serial, delivery method, Reserve."""
    page = ctx.page

    if tab:
        await click(page, page.get_by_role("link", name=tab, exact=True).locator("visible=true"))
    else:
        box = page.locator("xpath=//*[normalize-space(text())='Sort by']/preceding::input[1]").locator("visible=true")
        await box.last.click()
        await box.last.fill(device_core(unit.name))
        await page.keyboard.press("Enter")
        await busy_wait(page)

    row = page.get_by_text(device_rx(unit.name)).locator("visible=true").last
    try:
        await row.wait_for(state="visible", timeout=20000)
    except Exception:
        raise NeedsHuman(f"'{unit.name}' is not in the Add Equipment list of this node")
    unit.catalog = re.sub(r"\s+", " ", await row.inner_text()).strip()
    add = row.locator("xpath=following::*[normalize-space(text())='Add'][1]")
    await click(page, add, wait=False)

    dialog = page.get_by_text(re.compile(r'^\s*Add\s+["“\']')).locator("visible=true").last
    await dialog.wait_for(state="visible", timeout=15000)

    if unit.mode:
        sale = "Retail Price" if unit.mode.lower().startswith("retail") else "Installments"
        await choose(page, page, "Sale Type", sale)
    for label, value in (("Color", unit.color), ("Model", model_option)):
        if value:
            try:
                await choose(page, page, label, value.title() if label == "Color" else value)
            except Exception:
                pass
    await shot(ctx, label=f"add {unit.name}")
    await click(page, page.get_by_role("button", name="OK").locator("visible=true").last)

    # the new item opens expanded; if not, "Show" does it
    try:
        await T(page, "Delivery Method").last.wait_for(state="visible", timeout=6000)
    except Exception:
        await click(page, T(page, "Show").last)

    if unit.serial:
        for label in ("IMEI", "Serial Number"):
            try:
                await fill(page, label, unit.serial)
                await page.keyboard.press("Tab")
                await busy_wait(page)
                break
            except Exception:
                continue
    if ctx.store_node:
        await choose(page, page, "Delivery Method", ctx.store_node)

    months = re.search(r"-(\d+)", unit.mode or "")
    if months:
        try:
            await choose(page, page, "Contract Term", months.group(1))
        except Exception:
            ctx.notes.append(f"{unit.name}: Contract Term {months.group(1)} not offered - left as is")

    if unit.serial:
        await click(page, T(page, "Reserve").last)
        try:
            await T(page, "Reserved").last.wait_for(state="visible", timeout=30000)
        except Exception:
            raise NeedsHuman(f"{unit.name}: serial {unit.serial} did not get Reserved")

    if "care" in unit.note.lower():
        try:
            await tick(page, "AppleCare+")
        except Exception:
            ctx.notes.append(f"{unit.name}: no AppleCare+ option on this device")

    if unit.stock_shot:
        ctx.shots.append(unit.stock_shot)
    await shot(ctx, label=f"{unit.name} reserved")


async def assign_phone_and_sim(ctx):
    page = ctx.page
    try:
        await tick(page, SIM_TYPE)
        await choose(page, page, "Provisioning Device", SIM_PROVISIONING_DEVICE)
    except Exception as e:
        ctx.notes.append(f"SIM type not set automatically ({e})")
    num = await field_of(page, "Phone Number")
    await num.click()
    await busy_wait(page)
    opt = page.get_by_text(re.compile(r"^\s*\(\d{3}\)\s*\d{3}-\d{4}\s*$")).locator("visible=true")
    await opt.first.wait_for(state="visible", timeout=60000)
    await click(page, opt)


async def discount(ctx, label):
    await tick(ctx.page, label)


async def manage(ctx, unit_name, tab, reason_label, reason):
    """Existing equipment > Manage > Return / Replace / Disconnect > Apply."""
    page = ctx.page
    unit = ctx.units.get(unit_name)
    if unit and unit.where:
        await open_node(page, unit.where)
    item = page.get_by_text(device_rx(unit_name, whole=False)).locator("visible=true").last
    await item.scroll_into_view_if_needed()
    try:
        await T(page, "Manage").last.wait_for(state="visible", timeout=4000)
    except Exception:
        await click(page, item.locator("xpath=following::*[normalize-space(text())='Show'][1]"))
    await click(page, item.locator("xpath=following::*[normalize-space(text())='Manage'][1]"), wait=False)
    await click(page, page.get_by_role("link", name=tab, exact=True).locator("visible=true").last, wait=False)
    if reason:
        await choose(page, page, reason_label, reason)
    await shot(ctx, label=f"{tab} {unit_name}")


# ------------------------------------------------------------------
# ACTIONS - one per instruction. `m` is the regex match, `text` the step.
# ------------------------------------------------------------------

@action(r"\bcreate\s+(?P<kind>RCA|BCA)\b")
async def create_customer(ctx, m, text):
    import PIM_Validation as pim
    page = ctx.page
    business = m.group("kind").upper() == "BCA"
    num = ctx.ticket.split("-")[-1]
    last = f"{ctx.ticket.split('-')[0]}-{num}"

    await page.goto(ctx.origin + (BCA_PATH if business else CIM_PATH),
                    wait_until="domcontentloaded", timeout=120000)
    await pim.login_if_needed(page)
    new = page.get_by_role("button", name=re.compile(r"New (Residential|Business) Customer")).locator("visible=true")
    await new.first.click(timeout=60000)
    dialog = page.get_by_text(re.compile(r"^New (Residential|Business) Customer Account$"))
    await dialog.wait_for(state="visible", timeout=30000)

    dlg = page.locator("div.ui-dialog:visible").last
    if business:
        await fill(dlg, "Name", f"BCA {last}")
    else:
        await fill(dlg, "First Name", "RCA")
        await fill(dlg, "Last Name", last)
    await choose(page, dlg, "Customer Address", CUSTOMER_ADDRESS, ctx.log)
    # contact methods: keep the e-mail row, drop every other one (Mobile)
    for _ in range(4):
        extra = dlg.locator("img.nc-field-remove-button:visible")
        if not await extra.count():
            break
        await extra.first.click()
        await page.wait_for_timeout(400)
    await dlg.locator("input.gci_text_box:visible").first.fill(f"{last}@test.com")
    await fill(dlg, "Account PIN", CUSTOMER_PIN)
    await shot(ctx, label="new customer")
    await dlg.locator("button.ParCtr-saveButton:visible, button:visible:text-is('Create')").first.click()
    try:
        # "Possible Customer Account Duplicates" -> Create Anyway / Create New
        anyway = page.locator("button:visible, a:visible, input[type=button]:visible").filter(
            has_text=re.compile(r"^\s*Create\s+(Anyway|New)\s*$", re.I))
        start_url = page.url
        end = time.monotonic() + 180
        while page.url == start_url and time.monotonic() < end:
            if await anyway.count():
                await shot(ctx, label="duplicates")
                await anyway.first.click()
                await page.wait_for_timeout(3000)
            await page.wait_for_timeout(700)
        if page.url == start_url:
            raise TimeoutError("still on the customer list")
        await page.wait_for_load_state("domcontentloaded")
        oid = re.search(r"(?:object|id)=(\d+)", page.url)
        if "tab=_Summary" not in page.url and oid:      # landed on another view of the new customer
            await page.goto(f"{ctx.origin}/common/uobject.jsp?tab=_Summary&object={oid.group(1)}",
                            wait_until="domcontentloaded")
    except Exception:
        bad = [clean(t) for t in await dlg.locator(".nc-field-error:visible, .gci_invalid_text_box:visible, "
                                                   ".ui-state-error:visible").all_inner_texts()]
        raise NeedsHuman(f"the customer form was not accepted {bad or ''}")

    ctx.customer_url = page.url
    ctx.customer_name = ("BCA " if business else "RCA ") + last

    if re.search(r"employee", text, re.I):
        await page.locator("a:visible, button:visible").filter(has_text=_rx_exact("Edit")).first.click()
        box = await field_of(page, "Employee", css="input[type='checkbox']")
        await box.wait_for(state="visible", timeout=20000)
        for _ in range(20):                         # greyed out until the form is really in edit mode
            if await box.is_enabled():
                break
            await page.wait_for_timeout(500)
        if not await box.is_checked():
            await box.check()
        await shot(ctx, label="employee")
        await page.locator("button:visible").filter(has_text=_rx_exact("Save")).last.click()
        await page.wait_for_timeout(3000)
    await shot(ctx, label="customer")


@action(r"\+\s*BA\b|\bcreate\s+(a\s+)?(BA|billing\s+account)\b")
async def create_billing_account(ctx, m, text):
    page = ctx.page
    await page.goto(ctx.customer_url.replace("tab=_Summary", "tab=_Billing+Accounts+++"),
                    wait_until="domcontentloaded")
    btn = page.get_by_role("button", name="New Billing Account").locator("visible=true")
    await btn.first.click(timeout=60000)
    await T(page, "Invoice Delivery Method", exact=False).last.wait_for(state="visible", timeout=30000)
    await shot(ctx, label="new BA")
    await page.get_by_role("button", name="Create").locator("visible=true").last.click()
    try:                                    # "A fee will be charged for printed invoices"
        yes = page.get_by_role("button", name="Yes").locator("visible=true")
        await yes.last.wait_for(state="visible", timeout=8000)
        await shot(ctx, label="confirmation")
        await yes.last.click()
    except Exception:
        pass
    acct = page.get_by_role("link", name=re.compile(r"^\d{8,}\s")).locator("visible=true")
    await acct.first.wait_for(state="visible", timeout=120000)
    ctx.billing_account = re.match(r"\s*(\d+)", await acct.first.inner_text()).group(1)
    ctx.log(f"   BA {ctx.billing_account}")
    await shot(ctx, label="BA created")


@action(r"\bcreate\s+SO\s*\d*\b")
async def create_sales_order(ctx, m, text):
    page = ctx.page
    await page.goto(ctx.customer_url.replace("tab=_Summary", "tab=_Sales+Orders"),
                    wait_until="domcontentloaded")
    await page.get_by_role("button", name="New Sales Order", exact=True).locator("visible=true").first.click(timeout=60000)
    await T(page, "Distribution Channel").last.wait_for(state="visible", timeout=30000)
    await choose(page, page, "Distribution Channel", DISTRIBUTION_CHANNEL)
    await shot(ctx, label="new SO")
    await page.get_by_role("button", name="Create").locator("visible=true").last.click()
    await page.wait_for_url(re.compile(r"/roe/roe\.html"), timeout=120000)
    await page.get_by_text(re.compile(r"^Sales Order #\d+")).first.wait_for(state="visible", timeout=180000)
    await busy_wait(page)
    ctx.so_count += 1
    ctx.store_node = ""
    ctx.log("   " + (await page.get_by_text(re.compile(r"^Sales Order #\d+")).first.inner_text()).strip())


@action(r"\bwith\s+(?P<plan>[A-Za-z][\w+ ]*?)\s+(?P<n>\d+)\s+add-?\s*on\s+lines?")
async def add_mobile_plan(ctx, m, text):
    page = ctx.page
    plan, extra = m.group("plan").strip(), int(m.group("n"))
    await tick(page, plan)
    root = await node_label(page, plan)
    # the "+" right of "<plan> Line x1" asks how many more
    line = page.get_by_text(re.compile(r"^\s*" + re.escape(plan) + r"\s+Line\s*x\d")).locator("visible=true")
    try:
        await line.first.wait_for(state="visible", timeout=5000)
    except Exception:
        b = await T(page, root).first.bounding_box()      # expander sits left of the node
        await page.mouse.click(b["x"] - 14, b["y"] + b["height"] / 2 + 12)
        await line.first.wait_for(state="visible", timeout=15000)
    if extra:
        b = await line.first.bounding_box()
        plus = line.first.locator("xpath=following::*[self::a or self::img or self::span or self::div][1]")
        try:
            await plus.click(timeout=3000)
        except Exception:
            await page.mouse.click(b["x"] + b["width"] + 60, b["y"] + b["height"] / 2)
        qty = T(page, "Enter the number of product instances", exact=False).locator(
            "xpath=following::input[1]")
        await qty.wait_for(state="visible", timeout=10000)
        await qty.fill(str(extra))
        await shot(ctx, label="add-on lines")
        await click(page, page.get_by_role("button", name="OK").locator("visible=true").last)
    nodes = page.get_by_text(re.compile(r"^\s*" + re.escape(plan) + r"\s+Line\s*#\d+")).locator("visible=true")
    await nodes.first.wait_for(state="visible", timeout=30000)
    ctx.lines = [re.sub(r"\s+", " ", t).strip() for t in await nodes.all_inner_texts()]
    ctx.log(f"   lines: {ctx.lines}")
    if len(ctx.lines) != extra + 1:
        raise NeedsHuman(f"expected {extra + 1} lines, the tree shows {len(ctx.lines)}")


@action(r"\bon\s+respective\s+lines\b")
async def add_line_devices(ctx, m, text):
    devices = iet.extract_devices(text)
    units = [Unit(d.name, d.mode, d.note) for d in devices]
    if len(units) > len(ctx.lines):
        raise NeedsHuman(f"{len(units)} devices for {len(ctx.lines)} lines")
    await asyncio.gather(prefetch_stock(ctx, units), ensure_store(ctx))
    for unit, line in zip(units, ctx.lines):
        unit.where = line
        ctx.units[unit.name] = unit
        await open_node(ctx.page, line)
        await add_equipment(ctx, unit)
        await assign_phone_and_sim(ctx)
        await shot(ctx, label=f"{line} done")


@action(r"\bapply\s+associated\b.*\bdiscounts?\b")
async def apply_device_discounts(ctx, m, text):
    missing = []
    for unit in ctx.units.values():
        if not unit.mode.lower().startswith("installment") or not unit.where.count("Line"):
            continue
        label = DEVICE_DISCOUNTS.get(unit.name)
        if not label:
            missing.append(unit.name)
            continue
        await open_node(ctx.page, unit.where)
        await discount(ctx, label)
        await shot(ctx, label=f"discount {unit.name}")
    if missing:
        raise NeedsHuman("no discount configured for: " + ", ".join(missing)
                         + " - apply them by hand now and add them to DEVICE_DISCOUNTS")


@action(r"\badd\s+(?P<plan>[\w+ ]+?)\s+internet\s+offering")
async def add_internet(ctx, m, text):
    page = ctx.page
    plan = m.group("plan").strip()
    unit = Unit(INTERNET_EQUIPMENT_TAB, "", "")
    stock = asyncio.create_task(find_stock(ctx, unit, model=INTERNET_EQUIPMENT_MODEL))
    await ensure_store(ctx)
    await click(page, T(page, "Overview"))
    await tick(page, plan)
    node = await node_label(page, plan)
    await open_node(page, node)
    for label in INTERNET_UNCHECK:
        try:
            await tick(page, label, want=False)
        except Exception:
            pass
    try:
        await stock
    except Exception as e:
        ctx.log(f"   {e}")
    unit.where, unit.color = node, ""
    ctx.units[INTERNET_EQUIPMENT_MODEL] = unit
    await add_equipment(ctx, unit, tab=INTERNET_EQUIPMENT_TAB, model_option=INTERNET_EQUIPMENT_MODEL)


@action(r"\badd\s+new\s+equipment\b")
async def add_standalone_equipment(ctx, m, text):
    page = ctx.page
    units = [Unit(d.name, d.mode, d.note) for d in iet.extract_devices(text)]
    await asyncio.gather(prefetch_stock(ctx, units), ensure_store(ctx))
    await click(page, T(page, "Overview"))
    await tick(page, "New Equipment")
    node = await node_label(page, "New Equipment")
    await open_node(page, node)
    for unit in units:
        unit.where = node
        ctx.units[unit.name] = unit
        await add_equipment(ctx, unit)


@action(r"\bapply\s+(?P<disc>(?!associated)[A-Z][\w ]*?\bDiscount)\b")
async def apply_named_discount(ctx, m, text):
    label = m.group("disc").strip()
    try:
        await discount(ctx, label)
    except Exception:
        raise NeedsHuman(f"discount '{label}' is not on this screen - open the node that has it and tick it")
    await shot(ctx, label=label)


@action(r"\btry\s+to\s+add\s+any\s+other\b.*\bE[QP]{2}MP\b")
async def try_other_eqpmp(ctx, m, text):
    """Tick another EQPMP discount: the system must refuse with a pop-up."""
    page = ctx.page
    cand = page.get_by_text(re.compile(r"E[PQ]{2}MP.*Discount")).locator("visible=true")
    if await cand.count() == 0:
        for name in list({u.where for u in ctx.units.values() if u.where}):
            await open_node(page, name)
            if await cand.count():
                break
    if await cand.count() == 0:
        raise NeedsHuman("no EQPMP discount visible on any node")
    label = (await cand.first.inner_text()).strip()
    b = await cand.first.bounding_box()
    await page.mouse.click(b["x"] - 11, b["y"] + b["height"] / 2)
    await page.wait_for_timeout(2500)
    await shot(ctx, label=f"tried {label}")
    popup = page.get_by_text(re.compile(r"can\s*not select this discount", re.I)).locator("visible=true")
    if await popup.count() == 0:
        ctx.notes.append(f"EQPMP: '{label}' was ACCEPTED - no pop-up (ER says it must be refused)")
        raise NeedsHuman("the system accepted the second EQPMP discount - no pop-up. Check it, then Enter.")
    for name in ("OK", "Close"):
        btn = page.get_by_role("button", name=name).locator("visible=true")
        if await btn.count():
            await btn.last.click()
            break


@action(r"\bwill\s+show\s+pop-?\s*up\b")
async def popup_already_checked(ctx, m, text):
    """The pop-up is captured by the step before it."""


@action(r"\bupdate\s+(bill\s+estimates?|BE)\b")
async def update_bill_estimate(ctx, m, text):
    page = ctx.page
    await click(page, page.get_by_text(re.compile(r"^\s*Review\s*$")).locator("visible=true").first)
    await shot(ctx, label="review")
    try:
        await click(page, page.get_by_role("button", name="Configure Contract").locator("visible=true"))
        await page.wait_for_timeout(2000)
    except Exception:
        pass
    await click(page, T(page, "Bill Estimates").first)
    await click(page, page.get_by_role("button", name="Update Bill Estimates").locator("visible=true"))
    await T(page, "Total Including Taxes").first.wait_for(state="visible", timeout=120000)
    await shot(ctx, label="bill estimate")
    red = page.locator("[class*='warning' i], [class*='error' i]").get_by_text(
        re.compile(r"cannot be submitted|not specified|failed", re.I)).locator("visible=true")
    if await red.count():
        raise NeedsHuman("OE still shows blocking warnings: "
                         + " / ".join(t.strip()[:80] for t in await red.all_inner_texts()))


@action(r"\bcheck\s*-?\s*out\b")
async def checkout_and_process(ctx, m, text):
    page = ctx.page
    await click(page, page.get_by_text(re.compile(r"^\s*Review\s*$")).locator("visible=true").first)
    out = page.get_by_role("button", name="Check Out").locator("visible=true")
    if await out.first.is_disabled():
        raise NeedsHuman("Check Out is disabled - clear the warnings on the right")
    await out.first.click()
    await page.get_by_text(re.compile(r"has been successfully checked out")).wait_for(state="visible", timeout=300000)
    await shot(ctx, label="checked out")

    await page.goto(ctx.customer_url.replace("tab=_Summary", "tab=_Sales+Orders"), wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    pending = T(page, "Pending payment").first
    if await pending.count():
        await shot(ctx, label="pending payment")
        row = pending.locator("xpath=ancestor::tr[1]")
        await row.locator("input[type=checkbox]").first.check()
        await page.get_by_role("button", name="Submit to Provisioning").locator("visible=true").click()
        await page.wait_for_timeout(6000)
    await shot(ctx, label="sales orders")

    # Execution Flow of the newest order -> Activity View until Completed
    flow = page.get_by_role("link", name=re.compile(re.escape(ctx.customer_name) + r"\s*#")).locator("visible=true").last
    end = time.monotonic() + 600
    while await flow.count() == 0 and time.monotonic() < end:
        await page.wait_for_timeout(8000)
        await page.reload(wait_until="domcontentloaded")
    await flow.click()
    await page.wait_for_load_state("domcontentloaded")
    async with ctx.context.expect_page() as popup:
        await page.get_by_role("link", name="Activity View").locator("visible=true").first.click()
    view = await popup.value
    await view.set_viewport_size(VIEWPORT)
    end = time.monotonic() + 1800
    while time.monotonic() < end:
        await view.wait_for_timeout(10000)
        if await view.get_by_text(re.compile(r"^\s*Completed\s*$")).locator("visible=true").count():
            break
        await view.reload(wait_until="domcontentloaded")
    else:
        await shot(ctx, view, "flow NOT completed")
        await view.close()
        raise NeedsHuman("the execution flow did not reach Completed in 30 min")
    await shot(ctx, view, "flow completed")
    await view.close()


@action(r"\bvalidate\s+BPI\b|\bproducts\s+tab\b")
async def validate_bpi_and_products(ctx, m, text):
    page = ctx.page
    await page.goto(ctx.customer_url.replace("tab=_Summary", "tab=_Sales+Orders"), wait_until="domcontentloaded")
    so = page.get_by_role("link", name=re.compile(r"^Sales Order #\d+")).locator("visible=true").last
    await so.click(timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await page.get_by_role("link", name="Order Items", exact=True).locator("visible=true").first.click()
    await page.wait_for_timeout(5000)
    await shot_scrolling(ctx, label="order items")
    await page.goto(ctx.customer_url.replace("tab=_Summary", "tab=_Products"), wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    await shot_scrolling(ctx, label="products")


@action(r"\breturn\s+(?P<dev>[A-Z0-9][A-Z0-9 ]+?)\s+via\s+(?P<how>\w+\s+Return)\b")
async def return_device(ctx, m, text):
    page = ctx.page
    await click(page, T(page, "Overview"))
    await tick(page, m.group("how").title())
    await manage(ctx, m.group("dev").strip(), "Return", "Return Reason", RETURN_REASON)
    await click(page, page.get_by_role("button", name="Apply").locator("visible=true").last)


@action(r"\breplace\s+(?P<old>[A-Z0-9][A-Z0-9 ]+?)\s+with\s+(?P<new>[A-Z0-9][A-Z0-9 ]+?)\s*\((?P<mode>[^)]+)\)")
async def replace_device(ctx, m, text):
    old, new = m.group("old").strip(), m.group("new").strip()
    unit = Unit(new, iet._normalize_mode(m.group("mode")))
    await prefetch_stock(ctx, [unit])
    await manage(ctx, old, "Replace", "Replace Reason", "")
    unit.where = ctx.units[old].where if old in ctx.units else ""
    ctx.units[new] = unit
    # The Replace dialog was never captured in a POT, so its fields are not known yet.
    raise NeedsHuman(f"Replace tab is open for {old}. Pick {new} ({unit.mode}), serial "
                     f"{unit.serial or '?'} and Apply - then tell me the fields so I learn this one.")


@action(r"^\s*disconnect\s+(?P<list>.+)$")
async def disconnect_devices(ctx, m, text):
    page = ctx.page
    for name in names_in(m.group("list")):
        await manage(ctx, name, "Disconnect", "Disconnect Reason", DISCONNECT_REASON)
        await click(page, page.get_by_role("button", name="Apply").locator("visible=true").last)


@action(r"\brun\s+bill\b")
async def run_bill(ctx, m, text):
    if not ctx.bill:
        ctx.notes.append("billing skipped (--no-bill)")
        return
    if not ctx.rbm_host:
        raise NeedsHuman("no 'RBM :' host in the pasted block")
    if not ctx.billing_account:
        raise NeedsHuman("billing account number unknown")
    out = await asyncio.to_thread(rbm_bill, ctx)
    for path in render_terminal(ctx, out):
        ctx.shots.append(path)
    if "</Invoice>" not in out:
        raise NeedsHuman("the bill script did not print an invoice - see the last screenshots")


# ------------------------------------------------------------------
# RBM over SSH (what was done by hand in PuTTY)
# ------------------------------------------------------------------

def rbm_bill(ctx):
    import paramiko
    sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NTT"))
    from nc_credentials import get_credentials

    user, password = get_credentials("gci_rbm", default_username="rbmadmin")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ctx.rbm_host, username=user, password=password, timeout=30,
                look_for_keys=False, allow_agent=False)
    chan = ssh.invoke_shell(width=200, height=50)
    out = ""

    def read_until(pattern, timeout):
        nonlocal out
        end = time.monotonic() + timeout
        seen = len(out)
        while time.monotonic() < end:
            if chan.recv_ready():
                out += chan.recv(65535).decode("utf-8", "replace")
                if re.search(pattern, out[seen:], re.I | re.S):
                    return True
            else:
                time.sleep(0.3)
        return False

    try:
        read_until(r"[$#>]\s*$", 20)
        chan.send(f"cd {RBM_DIR}\n")
        read_until(r"[$#>]\s*$", 10)
        chan.send("ls -lrt\n")
        read_until(r"[$#>]\s*$", 15)
        scripts = re.findall(r"(\S+\.sh)\s*$", out, re.M)
        rx = re.compile(RBM_SCRIPT_GLOB.replace(".", r"\.").replace("*", ".*"), re.I)
        script = next((s for s in scripts if rx.fullmatch(s)), None)
        if not script:
            raise NeedsHuman(f"no {RBM_SCRIPT_GLOB} in ~/{RBM_DIR} on {ctx.rbm_host}")
        ctx.log(f"   RBM: {script} for account {ctx.billing_account}")
        chan.send(script + "\n")
        chose = False
        end = time.monotonic() + 1800
        while time.monotonic() < end:
            mark = len(out)
            read_until(r"account[^\n]*:\s*$|enter\s+choice\s*:\s*$|</Invoice>|[$#>]\s*$", 600)
            tail = out[mark:]
            if "</Invoice>" in tail:
                break
            if re.search(r"enter\s+choice\s*:\s*$", tail, re.I):
                chan.send(("9" if chose else RBM_MENU_CHOICE) + "\n")
                chose = True
            elif re.search(r"account[^\n]*:\s*$", tail, re.I):
                chan.send(ctx.billing_account + "\n")
            elif re.search(r"[$#>]\s*$", tail):
                break
        read_until(r"[$#>]\s*$", 5)
    finally:
        ssh.close()
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out).replace("\r", "")


def render_terminal(ctx, text, rows=52):
    """The session as terminal-looking pictures, one per screenful."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("consola.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    lines = text.splitlines()
    pages = [lines[i:i + rows] for i in range(0, len(lines), rows)] or [[]]
    paths = []
    os.makedirs(STATE_DIR, exist_ok=True)
    for n, chunk in enumerate(pages[:3] + pages[-3:] if len(pages) > 6 else pages):
        img = Image.new("RGB", (VIEWPORT["width"], VIEWPORT["height"]), "black")
        d = ImageDraw.Draw(img)
        for i, line in enumerate(chunk):
            d.text((6, 4 + i * 17), line[:230], font=font, fill=(230, 230, 230))
        path = os.path.join(STATE_DIR, f"{ctx.ticket}_rbm_{n}.png")
        img.save(path)
        paths.append(path)
    return paths


# ------------------------------------------------------------------
# RUNNER
# ------------------------------------------------------------------

_prompt_lock = asyncio.Lock()


async def ask_human(ctx, step_no, step_text, why):
    """Pause this ticket only. Enter = done by hand, r = retry, q = stop."""
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    base = os.path.join(STATE_DIR, f"miss_{ctx.ticket}_{step_no}_{stamp}")
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        await ctx.page.screenshot(path=base + ".png")
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(await ctx.page.content())
    except Exception:
        pass
    with open(MISS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} | {ctx.ticket} | step {step_no} | {step_text}\n    {why}\n    {base}.png / .html\n")
    async with _prompt_lock:
        print(f"\n[{ctx.ticket}] PAUSED at step {step_no}: {step_text}\n    why: {why}\n"
              f"    Do it by hand in the '{ctx.customer_name or ctx.ticket}' window, then:\n"
              f"    Enter = done, continue | r = retry it | q = stop this ticket", flush=True)
        answer = (await asyncio.to_thread(input, "    > ")).strip().lower()
    return answer


async def run_steps(ctx, case, skip):
    for i, step in enumerate(case.steps):
        if i < skip or i < ctx.done:
            continue
        ctx.log(f"step {i + 1}/{len(case.steps)}: {step.text}")
        hits = match_step(step.text)
        pending = list(hits)
        while True:
            why = ""
            if not hits:
                why = "no function matches this instruction (add an @action for it)"
            try:
                while pending:
                    name, fn, m = pending[0]
                    ctx.log(f"  -> {name}")
                    await fn(ctx, m, step.text)
                    pending.pop(0)
            except NeedsHuman as e:
                why = f"{pending[0][0]}: {e}"
            except Exception as e:
                why = f"{pending[0][0]} failed: {type(e).__name__}: {str(e).splitlines()[0][:300]}"
            if not why:
                break
            answer = await ask_human(ctx, i + 1, step.text, why)
            if answer == "q":
                return False
            if answer == "r":
                continue
            if pending:
                pending.pop(0)          # that function's part was done by hand
            try:
                await shot(ctx, label="after manual step")
            except Exception:
                pass
            if not pending:
                break
        ctx.done = i + 1
        save_state(ctx)
        write_report(ctx)
    return True


async def run_chain(browser, chain, origin, rbm, server_details, bill):
    prev = None
    for case, skip in chain:
        ctx = Ctx(case.ticket, case.title, origin, rbm, server_details)
        if prev is not None:            # continue the same customer, same window
            for k in ("page", "context", "customer_url", "customer_name", "billing_account",
                      "so_count", "lines", "units", "store_node"):
                setattr(ctx, k, getattr(prev, k))
            ctx.shots = list(prev.shots)
            ctx.inherited = len(prev.shots)
        else:
            ctx.context = await browser.new_context(viewport=VIEWPORT)
            ctx.page = await ctx.context.new_page()
        ctx.bill = bill
        ok = await run_steps(ctx, case, skip)
        write_report(ctx)
        ctx.log(f"POT: {ctx.word_path}  ({len(ctx.shots)} screenshots)")
        for n in ctx.notes:
            ctx.log(f"NOTE: {n}")
        if not ok:
            ctx.log("stopped - later tickets of this chain are skipped")
            return
        prev = ctx


async def fetch_cases(tickets):
    """Same reader as Internal_Env_Test, kept apart so it can run alone."""
    return await iet.fetch_cases(tickets)


def read_paste():
    print("\nPaste the ticket links + the TOMS / RBM block.\nFinish with two empty lines.\n")
    lines, empty = [], 0
    while empty < 2:
        try:
            line = input()
        except EOFError:
            break
        empty = empty + 1 if not line.strip() else 0
        lines.append(line)
    return "\n".join(lines)


def main(argv):
    raw = read_paste()
    tickets = list(dict.fromkeys(re.findall(r"[A-Z][A-Z0-9]+-\d+", raw)))
    tickets = [t for t in tickets if not re.match(r"(BOT|TC)-", t)]
    origin = extract_origin(raw)
    rbm = extract_rbm(raw)
    server_details = extract_server_block(raw)

    if "--offline" in argv:
        case = iet.parse_description(tickets[0] if tickets else "OFFLINE-1", "", raw)
        print_plan(build_chains([case]))
        return 0

    if not tickets:
        print("No tickets/links detected.")
        return 1
    print(f"\nTOMS: {origin or '??'}\nRBM : {rbm or '??'}\nTickets: {', '.join(tickets)}")
    if not origin:
        print("No 'TOMS : http://host:port/' line in the paste.")
        return 1

    cases = asyncio.run(fetch_cases(tickets))
    if not cases:
        print("Nothing to run.")
        return 1

    chains = build_chains(cases)
    missing = print_plan(chains)
    if "--plan" in argv:
        return 0
    if missing:
        go = input("\nRun anyway and pause on those steps? (y/n): ").strip().lower()
        if go != "y":
            return 1

    async def go_all():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            jobs = [run_chain(browser, ch, origin, rbm, server_details, "--no-bill" not in argv)
                    for ch in chains]
            if "--serial" in argv:
                for j in jobs:
                    await j
            else:
                results = await asyncio.gather(*jobs, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        print(f"chain crashed: {type(r).__name__}: {r}")
            print("\nDone. Browser left open - press Enter here to close it.")
            await asyncio.to_thread(input)

    asyncio.run(go_all())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
