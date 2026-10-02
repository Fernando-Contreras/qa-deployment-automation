"""AM Validation — the Asset Management sibling of PIM_Validation.py.

Paste the Server Details (with a "TOMS:" line and the TMS ticket links),
and for every ticket this:

  1. reads the device list out of the ticket description
     ("Perform AM Validation for following devices:");
  2. opens Asset Management > Asset Inventory > General Handbook >
     Stock Item Models on that TOMS;
  3. per device: filters the grid by Name, screenshots the result row
     (Name / SKU / Equipment Type / Brand / State), opens the device and
     screenshots its Parameters page (SKU, Color, Brand, Storage, ...);
  4. puts it all in Downloads\\<TICKET>.docx.

Comparing against the loader (NC.GCI.Polaris.DSG.AM.Configuration.xlsx)
is still done by hand.

Everything TOMS/Jira/Word-related is borrowed from PIM_Validation.py, so
a fix there (login, filter, Word dialogs) fixes this one too.
"""

import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright, Page

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import PIM_Validation as pim


# Headless is faster, but the "click the filter by hand" rescue needs a
# window. Same trade-off as in PIM_Validation.py.
HEADLESS = False

# Only the host changes between environments; the General Handbook
# object and its Stock Item Models tab are the same everywhere.
STOCK_ITEM_MODELS_PATH = (
    "/common/uobject.jsp?tab=_Stock+Item+Models"
    "&object=9142030364013419463"
)

# Heading put above each device's own page — also what a resumed run
# looks for to know a device is done.
DEVICE_HEADING_SUFFIX = " - Stock Item Model"


# -------------------------
# INPUT
# -------------------------

def build_stock_items_url(toms, raw_text):
    """The Stock Item Models URL on the pasted TOMS (same host rules as
    PIM_Validation: host, host:port or any link on the box)."""

    object_url = pim.resolve_object_url(toms, raw_text)

    if not object_url:
        return None

    origin = object_url[: object_url.index(pim.OBJECT_PATH)]

    return origin + STOCK_ITEM_MODELS_PATH


def extract_devices(description):
    """The devices listed in the ticket.

    Reuses PIM's list reader, then keeps only lines that look like a
    device (or a URL) — so a trailing "Validate SKU, Colour, ..." or
    "Refer NC.GCI....xlsx Loader" line is never searched as a device when
    Jira drops the blank line after the list."""

    found = pim.extract_offerings_from_description(description)

    return [
        d for d in found
        if pim.is_offering_url(d) or pim._looks_like_offering(d)
    ]


def resume_skip_count(existing_text, devices):
    """How many devices, from the start, a resumed run can skip. The last
    device showing any progress is always redone."""

    last_seen = -1

    for i, device in enumerate(devices):
        if device in existing_text:
            last_seen = i

    skip = 0

    for i, device in enumerate(devices):
        done = f"{device}{DEVICE_HEADING_SUFFIX}" in existing_text
        if i < last_seen and done:
            skip += 1
        else:
            break

    return skip


# -------------------------
# ONE DEVICE
# -------------------------

async def open_device(context, search_page: Page, device: str):
    """Open the device from the filtered grid in its own tab.

    Looks for the link with EXACTLY that name first, so "IPHONE 18 PRO
    2TB BURGUNDY" never opens "CPO IPHONE 18 PRO 2TB BURGUNDY". Returns
    None if the grid doesn't show it."""

    try:
        link = await pim.locate_in_any_frame(
            search_page,
            lambda f: f.get_by_role("link", name=device, exact=True).first,
            timeout=8000
        )
    except Exception:
        link = None

    if link is not None:
        url = await pim._link_url(link)
        if url:
            return await pim.open_url_tab(context, url)

    try:
        return await pim.open_in_new_tab(
            context, search_page, device, timeout=5000
        )
    except Exception:
        return None


async def process_device(context, search_page: Page, device: str, word_path: str):
    """Search results + the device's Parameters page. Never raises."""

    tab = None
    name = device

    try:
        if pim.is_offering_url(device):

            print(f"\n🔗 Opening device by URL: {device}")

            tab = await pim.open_url_tab(context, device)
            await pim.login_if_needed(tab, probe_timeout=2500)

            try:
                name = (await tab.title()).strip() or device
            except Exception:
                pass

        else:
            await pim.filter_offering(search_page, device)

            await pim.capture_single(
                search_page,
                f"Search results: {device}",
                word_path
            )

            print(f"\n🖱 Opening device: {device}")

            tab = await open_device(context, search_page, device)

            if tab is None:
                print(f"❌ Not found in Stock Item Models: {device}")
                pim._add_heading_to_word(
                    word_path,
                    f"{device} - NOT FOUND in Stock Item Models"
                )
                return

        await pim.capture_full_page(
            tab,
            f"{name}{DEVICE_HEADING_SUFFIX}",
            word_path
        )

    except Exception as e:
        print(f"⚠ Problem on {name}: {e} — screenshot and continuing.")
        await pim._safe_error_shot(tab or search_page, word_path, name)

    finally:
        if tab is not None:
            await pim.close_tab_later(tab)


# -------------------------
# ONE TICKET
# -------------------------

async def open_stock_items(search_page: Page, state: dict):
    """Open + log into Stock Item Models once, the first time it's needed."""

    if state["ready"]:
        return

    if not state["url"]:
        raise RuntimeError(
            "No usable TOMS address in the pasted Server Details. Add a "
            "line like \"TOMS: http://192.0.2.1:6300\"."
        )

    print(f"\n🌐 Opening Stock Item Models: {state['url']}")

    await search_page.goto(
        state["url"],
        wait_until="domcontentloaded",
        timeout=120000
    )

    await pim.login_if_needed(search_page)
    await search_page.wait_for_timeout(1200)

    state["ready"] = True


async def process_ticket(context, search_page, ticket, server_details, state):

    safe_ticket = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", ticket).strip(" .")
    word_path = os.path.join(pim.DOWNLOADS, f"{safe_ticket}.docx")

    print(f"\n🎫 Opening ticket: {ticket}")

    ticket_page = await context.new_page()

    await ticket_page.goto(
        pim.TMS_TICKET_URL + ticket,
        wait_until="domcontentloaded",
        timeout=60000
    )

    await pim.login_tms_if_needed(ticket_page)
    await ticket_page.wait_for_timeout(900)

    title = await pim.extract_jira_title(ticket_page)
    print(f"📌 Title: {title}")

    description = await pim.extract_jira_description(ticket_page)
    devices = extract_devices(description)

    if not devices:
        print("⚠ Could not auto-detect devices from the ticket description.")
        devices = pim.read_offerings()

    try:
        await ticket_page.close()
    except Exception:
        pass

    print("\nDevices:")
    for d in devices:
        print(f"  - {d}")

    if any(not pim.is_offering_url(d) for d in devices):
        await open_stock_items(search_page, state)

    existing_text = pim._read_word_text(word_path)

    if existing_text:
        skip = resume_skip_count(existing_text, devices)
        if skip:
            print(
                f"📄 Existing report found for {ticket} — resuming, "
                f"skipping {skip} already-finished device(s)."
            )
        devices = devices[skip:]

    else:
        pim._add_heading_to_word(
            word_path,
            f"{ticket} - {title}" if title else ticket
        )
        if server_details:
            pim._add_text_block_to_word(word_path, server_details)

    for device in devices:
        await process_device(context, search_page, device, word_path)

    pim._save_word_document(word_path)
    print(f"💾 Saved (left open in Word): {word_path}")


# -------------------------
# MAIN
# -------------------------

async def main():

    pim.HEADLESS = HEADLESS

    raw_text = pim.read_multiline()

    toms = pim.extract_toms(raw_text)
    tickets = pim.extract_tickets(raw_text)

    print("\n================")
    print(f"TOMS: {toms}")

    if not toms:
        print("❌ TOMS not detected.")
        return

    url = build_stock_items_url(toms, raw_text)

    if url:
        print(f"Stock Item Models: {url}")
    else:
        print(
            "⚠ Couldn't read an address out of that TOMS — devices given "
            "by name can't be searched."
        )

    print("\nTickets:")
    for t in tickets:
        print(t)

    if not tickets:
        print("❌ No tickets/links detected.")
        return

    server_details = pim.extract_server_details(raw_text)

    async with async_playwright() as pw:

        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        search_page = await context.new_page()

        state = {"ready": False, "url": url}

        for ticket in tickets:
            try:
                await process_ticket(
                    context, search_page, ticket, server_details, state
                )
            except Exception as e:
                print(
                    f"⚠ Problem on ticket {ticket}: {e} — "
                    "moving on to the next ticket."
                )

        print(
            """
🎉 Done.

Browser and Word left open — the last tab and every report are still
there to look through.
"""
        )

        await asyncio.sleep(9999999)


if __name__ == "__main__":
    asyncio.run(main())
