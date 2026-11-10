"""Internal_Env_Test.py - guided runner for OE test-case tickets
(GCI.QA | BOT-xxxx | TC-nn | ...).

Input is the same paste as PIM_Validation.py: the ticket links plus the
Server Details block, e.g.

    https://jira.example.com/browse/VALPRJ-124614
    https://jira.example.com/browse/VALPRJ-110868

    Server Details :
    TOMS: http://192.0.2.2:6300/
    RBM: 192.0.2.3
    ...

For every ticket it opens TMS, reads the title (TC-nn) and the Test Steps
from the description, and builds the plan from THAT - nothing is typed in
here. Each device found as "NAME (Retail Price)" / "NAME (Installment-36)"
is tracked with its expected price mode and shown at every validation
step.

Then, one ticket at a time, it walks you through the steps while you work
in OE yourself, taking the screenshots into ONE Word report per ticket
(Server Details at the top, a heading per step) and recording an explicit
verdict for each "ER:" / validate step. Nothing is marked OK on its own:
a validation step you never judged ends as NO VERDICT in the summary.
It does not click inside OE for you.

Hotkeys (global)
----------------
    Alt+S   screenshot -> current step in the Word report
    Alt+O   verdict OK   for the current validation step
    Alt+F   verdict FAIL for the current validation step
    Alt+N   next step
    Alt+B   back one step
    Alt+Q   finish early (report is still written)

Usage:  python Internal_Env_Test.py      (then paste, two empty lines)
        python Internal_Env_Test.py --offline   (paste ticket text itself,
                                                 no TMS - for checking the parser)
"""

import asyncio
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

# ========= CONFIG =========

DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")

HK_SHOT = "alt+s"
HK_OK = "alt+o"
HK_FAIL = "alt+f"
HK_NEXT = "alt+n"
HK_BACK = "alt+b"
HK_QUIT = "alt+q"

SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
CUT_TOP = 120
CUT_BOTTOM = 50

# ===========================


# -------------------------
# DATA MODEL
# -------------------------

@dataclass
class Device:
    name: str
    mode: str
    note: str = ""

    def label(self):
        extra = f" {self.note}" if self.note else ""
        return f"{self.name}{extra}  [{self.mode}]"


@dataclass
class Step:
    text: str
    er: str = ""
    checklist: list = field(default_factory=list)
    verdict: str = ""
    shots: int = 0


@dataclass
class TestCase:
    ticket: str
    title: str
    tc: str
    steps: list
    notes: list = field(default_factory=list)
    devices: list = field(default_factory=list)


# -------------------------
# PARSING THE TICKET
# -------------------------

MODE_RE = re.compile(r"\(\s*(Retail Price|Installment\s*-\s*\d+)\s*\)", re.I)
NUMBERED_RE = re.compile(r"^\s*\d+\s*[\.\)]\s*(.*)$")
ER_RE = re.compile(r"^\s*ER\s*:\s*(.*)$", re.I)
STEPS_HEADER_RE = re.compile(r"^\s*test\s+steps?\s*:?\s*$", re.I)
VERDICT_STEP_RE = re.compile(
    r"^\s*validate\b|\bwill (not )?(show|allow)\b", re.I
)


def _is_name_token(tok):
    """Device names are shouty: letters/digits with no lowercase."""
    core = tok.strip(",;:")
    return bool(core) and core == tok and not any(c.islower() for c in core) \
        and any(c.isalnum() for c in core) and core not in ("+", "-")


def _normalize_mode(mode):
    mode = re.sub(r"\s+", "", mode)
    if mode.lower().startswith("installment"):
        return "Installment-" + mode.split("-")[1]
    return "Retail Price"


def extract_devices(text):
    """Every "NAME (price mode)" in a piece of step text, in order.

    The name is the run of SHOUTY tokens right before the parenthesis;
    lowercase words in between (e.g. "+ apple care protection") are kept
    as a note on the device rather than breaking it."""

    devices = []
    pos = 0

    for m in MODE_RE.finditer(text):

        segment = text[pos:m.start()]
        pos = m.end()

        tokens = segment.split()
        name, skipped = [], []
        i = len(tokens) - 1

        while i >= 0 and not _is_name_token(tokens[i]):
            skipped.insert(0, tokens[i])
            i -= 1

        while i >= 0 and _is_name_token(tokens[i]):
            name.insert(0, tokens[i])
            i -= 1

        if not name:
            continue

        note = " ".join(skipped).strip()
        # Only keep it as a note when it is an add-on ("+ apple care").
        note = note if note.startswith("+") else ""

        devices.append(Device(" ".join(name), _normalize_mode(m.group(1)), note))

    return devices


def parse_title(title):
    tc = re.search(r"\bTC[-\s]?\d+\b", title or "", re.I)
    return tc.group(0).upper().replace(" ", "-") if tc else ""


def parse_description(ticket, title, description):
    """Build a TestCase from a ticket's title + description text."""

    lines = [l.rstrip() for l in (description or "").splitlines()]

    # Drop everything before a "Test Steps" header, keeping earlier lines
    # (e.g. "Reuse Test data of TC no 09") as notes.
    notes = []
    start = 0
    for i, l in enumerate(lines):
        if STEPS_HEADER_RE.match(l):
            start = i + 1
            notes = [x.strip() for x in lines[:i] if x.strip()]
            break

    body = [l for l in lines[start:] if l.strip()]
    numbered = any(NUMBERED_RE.match(l) for l in body)

    raw = []          # [{"text": str, "er": [str]}]

    for l in body:
        er = ER_RE.match(l)
        num = NUMBERED_RE.match(l)

        if er:
            if raw:
                raw[-1]["er"].append(er.group(1).strip())
            continue

        if num or not numbered:
            raw.append({"text": (num.group(1) if num else l).strip(), "er": []})
        elif raw:
            raw[-1]["text"] += " " + l.strip()      # wrapped line
        else:
            notes.append(l.strip())

    steps = []
    registry = []

    for r in raw:
        found = extract_devices(r["text"])
        for d in found:
            if not any(x.name == d.name and x.mode == d.mode for x in registry):
                registry.append(d)

        er = " ".join(r["er"]).strip()
        if not er and VERDICT_STEP_RE.search(r["text"]):
            er = r["text"]

        steps.append(Step(r["text"], er=er, checklist=[d.label() for d in found]))

    # Validation steps show every device declared up to that point, so the
    # names/prices to compare against are in front of you.
    seen = []
    for step, r in zip(steps, raw):
        for d in extract_devices(r["text"]):
            if d.label() not in seen:
                seen.append(d.label())
        if step.er and not step.checklist and re.search(
                r"price|name|BPI|Products|invoice|discount", step.er + step.text, re.I):
            step.checklist = list(seen)

    return TestCase(ticket, title, parse_title(title), steps, notes, registry)


# -------------------------
# WORD REPORT
# -------------------------

def _grab():
    from PIL import ImageGrab
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(DOWNLOADS, f"temp_{stamp}.png")
    ImageGrab.grab(bbox=(0, CUT_TOP, SCREEN_WIDTH,
                         SCREEN_HEIGHT - CUT_BOTTOM)).save(path)
    return path


class Report:
    """One .docx per ticket, appended to as the run goes."""

    def __init__(self, ticket):
        safe = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", ticket).strip(" .")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(DOWNLOADS, f"Internal_Env_Test_{safe}_{stamp}.docx")
        self._lock = threading.Lock()

    def _with_doc(self, body):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            try:
                word = win32com.client.GetActiveObject("Word.Application")
            except Exception:
                word = win32com.client.Dispatch("Word.Application")
            word.Visible = True

            doc = None
            for d in word.Documents:
                try:
                    if d.FullName.lower() == self.path.lower():
                        doc = d
                        break
                except Exception:
                    continue
            if doc is None:
                if os.path.exists(self.path):
                    doc = word.Documents.Open(self.path)
                else:
                    doc = word.Documents.Add()
                    doc.SaveAs(self.path)

            body(doc)
            doc.Save()
        finally:
            pythoncom.CoUninitialize()

    @staticmethod
    def _append(doc, text, bold=False, size=11):
        text = text.replace("\r\n", "\n").replace("\n", "\r")
        rng = doc.Range(doc.Content.End - 1)
        rng.InsertParagraphAfter()
        rng = doc.Range(doc.Content.End - 1)
        start = rng.Start
        rng.InsertAfter(text)
        part = doc.Range(start, doc.Content.End - 1)
        part.Font.Bold = bold
        part.Font.Size = size

    def text(self, text, bold=False, size=11):
        with self._lock:
            self._with_doc(lambda doc: self._append(doc, text, bold, size))

    def heading(self, text):
        self.text(text, bold=True, size=14)

    def picture(self, image_path):
        def body(doc):
            rng = doc.Range(doc.Content.End - 1)
            rng.InsertParagraphAfter()
            rng = doc.Range(doc.Content.End - 1)
            rng.InlineShapes.AddPicture(os.path.abspath(image_path))

        with self._lock:
            self._with_doc(body)
        try:
            os.remove(image_path)
        except Exception:
            pass


# -------------------------
# RUNNER
# -------------------------

class Runner:

    def __init__(self, case, server_details=""):
        self.case = case
        self.server_details = server_details
        self.report = Report(case.ticket)
        self.index = 0
        self.done = False
        self.finished_early = False
        self._busy = threading.Lock()

    @property
    def step(self):
        return self.case.steps[self.index]

    def announce(self):
        step = self.step
        total = len(self.case.steps)
        print(f"\n[{self.case.tc or self.case.ticket}] Step {self.index + 1}/{total}")
        print(f"   {step.text}")
        for line in step.checklist:
            print(f"     - {line}")
        if step.er:
            print(f"   ER: {step.er}")
            print(f"   -> judge it: {HK_OK.upper()} = OK, {HK_FAIL.upper()} = FAIL")

        self.report.heading(f"Step {self.index + 1}: {step.text}")
        for line in step.checklist:
            self.report.text(f"Expected: {line}", size=10)
        if step.er:
            self.report.text(f"ER: {step.er}", size=10)

    def shot(self):
        with self._busy:
            time.sleep(0.2)
            self.report.picture(_grab())
            self.step.shots += 1
            print(f"   screenshot added ({self.step.shots})")

    def verdict(self, value):
        with self._busy:
            step = self.step
            if not step.er:
                print("   This step has no ER - nothing to judge.")
                return
            step.verdict = value
            self.report.text(f"VERDICT: {value}", bold=True)
            print(f"   verdict: {value}")

    def next(self):
        with self._busy:
            step = self.step
            if step.shots == 0:
                print("   ! no screenshot taken for this step.")
            if step.er and not step.verdict:
                print("   ! validation step left without a verdict.")
            if self.index + 1 >= len(self.case.steps):
                self.done = True
                return
            self.index += 1
            self.announce()

    def back(self):
        with self._busy:
            if self.index:
                self.index -= 1
                print(f"\n(back) Step {self.index + 1}: {self.step.text}")

    def quit(self):
        self.finished_early = True
        self.done = True

    def summary(self):
        lines = []
        fails = 0
        for i, s in enumerate(self.case.steps, 1):
            if s.er:
                if s.verdict == "FAIL":
                    fails += 1
                lines.append(f"  {i:>2}. {(s.verdict or 'NO VERDICT'):<10} - {s.text}")
            elif s.shots == 0:
                lines.append(f"  {i:>2}. NO EVIDENCE  - {s.text}")
        if self.finished_early:
            lines.append(f"  (stopped early at step {self.index + 1} of "
                         f"{len(self.case.steps)})")
        lines.append(f"  FAIL count: {fails}")
        return lines

    def run(self):
        import keyboard

        case = self.case
        print(f"\n=== {case.tc} | {case.ticket} ===")
        print(case.title)
        print(f"Report: {self.report.path}")
        for n in case.notes:
            print(f"NOTE: {n}")
        print(f"Hotkeys: {HK_SHOT} shot | {HK_OK} OK | {HK_FAIL} FAIL | "
              f"{HK_NEXT} next | {HK_BACK} back | {HK_QUIT} quit")

        self.report.heading(f"{case.ticket} - {case.title}")
        if self.server_details:
            self.report.text(self.server_details)
        for n in case.notes:
            self.report.text(n, size=10)

        keyboard.add_hotkey(HK_SHOT, self.shot)
        keyboard.add_hotkey(HK_OK, lambda: self.verdict("OK"))
        keyboard.add_hotkey(HK_FAIL, lambda: self.verdict("FAIL"))
        keyboard.add_hotkey(HK_NEXT, self.next)
        keyboard.add_hotkey(HK_BACK, self.back)
        keyboard.add_hotkey(HK_QUIT, self.quit)

        self.announce()

        try:
            while not self.done:
                time.sleep(0.2)
        finally:
            keyboard.unhook_all_hotkeys()

        lines = self.summary()
        print(f"\nSUMMARY {case.tc} ({case.ticket})\n" + "\n".join(lines))
        self.report.heading("Summary")
        self.report.text("\n".join(l.strip() for l in lines), size=10)


# -------------------------
# FETCH FROM TMS (reuses PIM_Validation's login + readers)
# -------------------------

async def fetch_cases(tickets):
    from playwright.async_api import async_playwright
    import PIM_Validation as pim

    cases = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()

        for ticket in tickets:
            page = await context.new_page()
            try:
                await page.goto(pim.TMS_TICKET_URL + ticket,
                                wait_until="domcontentloaded", timeout=60000)
                await pim.login_tms_if_needed(page)
                await page.wait_for_timeout(900)

                title = await pim.extract_jira_title(page)
                description = await pim.extract_jira_description(page)
            except Exception as e:
                print(f"⚠ Could not read {ticket}: {e}")
                continue
            finally:
                await page.close()

            case = parse_description(ticket, title, description)
            print(f"\n🎫 {ticket}: {title}")
            print(f"   {len(case.steps)} step(s), {len(case.devices)} device(s) found")
            if not case.steps:
                print("   ⚠ no steps detected in the description - skipped.")
                continue
            cases.append(case)

        await browser.close()

    return cases


# -------------------------
# MAIN
# -------------------------

def main(argv):
    import PIM_Validation as pim

    raw = pim.read_multiline()
    tickets = pim.extract_tickets(raw)
    server_details = pim.extract_server_details(raw)

    if not tickets:
        print("❌ No tickets/links detected.")
        return 1

    print("\nTickets:\n  " + "\n  ".join(tickets))

    if "--offline" in argv:
        case = parse_description(tickets[0], "", raw)
        cases = [case]
    else:
        cases = asyncio.run(fetch_cases(tickets))

    if not cases:
        print("❌ Nothing to run.")
        return 1

    for case in cases:
        runner = Runner(case, server_details)
        runner.run()
        if runner.finished_early:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
