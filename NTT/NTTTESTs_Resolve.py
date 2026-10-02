import argparse
import asyncio
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from nc_credentials import get_credentials

# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
USERNAME, PASSWORD = get_credentials("nttests", default_username="tests_user")

# Cuántos tickets se resuelven al mismo tiempo (una pestaña por ticket).
MAX_CONCURRENT = 5

# Cuánto esperar a que aparezca el botón "Resolve Issue" antes de asumir
# que el ticket ya está resuelto (no bloquea el resto del batch).
RESOLVE_ACTION_TIMEOUT_MS = 20000


def normalize_account(input_text: str) -> str:
    input_text = input_text.strip()

    # Si ya es URL, usarla directo
    if input_text.lower().startswith("http"):
        return input_text

    # Si es ticket, construir URL
    return f"https://jira.example.com/browse/{input_text}"


async def get_ticket_state(page) -> str:
    """Best-effort read of the current status/resolution, for the skip log line."""
    for selector in ("#status-val", "#resolution-val"):
        locator = page.locator(selector)
        try:
            if await locator.count() > 0:
                text = (await locator.first.inner_text()).strip()
                if text:
                    return text
        except Exception:
            pass
    return "unknown"


async def resolve_ticket(page, prefix="🛠️") -> str:
    """Returns 'resolved' if it just transitioned the ticket, 'already_resolved' if skipped."""
    action = page.locator("#action_id_21")
    try:
        await action.wait_for(state="visible", timeout=RESOLVE_ACTION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        state = await get_ticket_state(page)
        print(f"{prefix} ℹ️ Already in Resolve (status: {state}) — skipping\n")
        return "already_resolved"

    print(f"{prefix} Opening Resolve form...")
    await action.click()
    await asyncio.sleep(2)

    # -------- RESOLUTION = Fixed --------
    print(f"{prefix} ⬇️ Setting Resolution = Fixed")
    resolution = page.locator("select#resolution")
    await resolution.wait_for(state="visible", timeout=1000000)
    await resolution.select_option(label="Fixed")

    # -------- ROOT CAUSE = Software Issue --------
    print(f"{prefix} 🧩 Ensuring Root Cause = Software Issue")
    root_cause = page.locator(
        "//label[contains(text(),'Root Cause')]/following::select[1]"
    )
    await root_cause.wait_for(state="visible", timeout=1000000)

    selected = await root_cause.input_value()
    if "software" not in selected.lower():
        await root_cause.select_option(label="Software Issue")

    await asyncio.sleep(0.5)

    # -------- TRY CLICK RESOLVE BUTTON --------
    print(f"{prefix} 🎯 Trying to click Resolve button")
    resolve_submit = page.locator(
        "//input[@type='submit' and (contains(@value,'Resolve') or @accesskey='s')]"
    )

    if await resolve_submit.count() > 0:
        await resolve_submit.first.scroll_into_view_if_needed()
        await resolve_submit.first.focus()
        await asyncio.sleep(0.2)
        await resolve_submit.first.click()
    else:
        print(f"{prefix} ⚠️ Resolve button not found, using Alt+S fallback")
        await page.keyboard.down("Alt")
        await page.keyboard.press("KeyS")
        await page.keyboard.up("Alt")

    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    print(f"{prefix} ✅ Ticket resolved\n")
    return "resolved"


async def process_ticket(
    context,
    url: str,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
    page: Page | None = None,
) -> str:
    prefix = f"[{index}/{total}]"
    async with semaphore:
        try:
            if page is None:
                print(f"{prefix} 🌍 Opening ticket: {url}")
                page = await context.new_page()
                await page.goto(url, timeout=12000000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)

            result = await resolve_ticket(page, prefix=prefix)
            await page.close()  # ya quedó en Resolve (o ya lo estaba); no hace falta dejarla abierta
            return result
        except Exception as exc:
            print(f"{prefix} ❌ FAILED: {exc}\n")
            return "failed"  # se deja la pestaña abierta para poder revisarla


async def main(headless: bool):
    print("\n📄 Paste the accounts (one per line)")
    print("👉 Press ENTER twice when done:\n")

    accounts = []
    while True:
        line = input()
        if not line.strip():
            break
        accounts.append(normalize_account(line))  # 🔥 NORMALIZACIÓN AQUÍ

    if not accounts:
        print("❌ No accounts provided.")
        return

    total = len(accounts)

    print(f"🖥️ Modo: {'headless (sin ventana)' if headless else 'headed (con ventana)'}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()

        # -------- FIRST TAB --------
        page = await context.new_page()
        await page.goto(accounts[0], timeout=12000000)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)

        # -------- LOGIN ONCE --------
        print("⌨️ Logging in...")
        await page.keyboard.type(USERNAME, delay=40)
        await page.keyboard.press("Tab")
        await page.keyboard.type(PASSWORD, delay=40)
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)
        print("✅ Logged in\n")

        # -------- RESOLVE ALL TICKETS IN PARALLEL --------
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        tasks = [process_ticket(context, accounts[0], 1, total, semaphore, page=page)]
        for i, account_url in enumerate(accounts[1:], start=2):
            tasks.append(process_ticket(context, account_url, i, total, semaphore))

        results = await asyncio.gather(*tasks)

        resolved = results.count("resolved")
        already = results.count("already_resolved")
        failed = results.count("failed")
        print(
            f"🏁 DONE: {resolved} resolved, {already} already in Resolve, "
            f"{failed} failed (of {total})"
        )

        if failed and not headless:
            print("⚠️ Failed tickets were left open for review.")
            input("Press ENTER here to close the browser...")

        await context.close()
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve NTT Jira tickets (parallel).")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless (default: headless).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(headless=not args.headed))