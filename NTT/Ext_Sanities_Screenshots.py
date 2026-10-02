import asyncio
import keyboard
import pyautogui
from playwright.async_api import async_playwright

DEBUG_URL = "http://localhost:9222"

ROWS_MAIN = 8   # CAM → REM
ROWS_EXTRA = 4  # BillOps + APIs


# -------------------------
# SCREENSHOT (FULL SCREEN)
# -------------------------
def take_screenshot_to_clipboard():
    img = pyautogui.screenshot()
    img.save("temp.png")

    # copiar al clipboard usando ctrl+c simulando drag (más estable workaround)
    pyautogui.hotkey("win", "shift", "s")
    # aquí tú haces click rápido o puedes ignorarlo si no lo usas


# -------------------------
# PEGAR
# -------------------------
async def paste(page):
    await page.keyboard.press("Control+V")
    await asyncio.sleep(1)


# -------------------------
# LIMPIAR CELDA
# -------------------------
async def clean(page):
    await page.keyboard.press("Delete")
    await page.keyboard.press("Delete")
    await asyncio.sleep(0.2)


# -------------------------
# MOVIMIENTO
# -------------------------
async def down(page):
    await page.keyboard.press("ArrowDown")

async def up(page):
    await page.keyboard.press("ArrowUp")

async def right(page):
    await page.keyboard.press("ArrowRight")


# -------------------------
# FLOW
# -------------------------
async def run(page):
    print("\n🎮 ALT+S = screenshot + paste")

    # ---------- EAST ----------
    print("➡️ EAST")

    for _ in range(ROWS_MAIN):
        keyboard.wait("alt+s")

        take_screenshot_to_clipboard()
        await clean(page)
        await paste(page)
        await down(page)

    # ---------- WEST ----------
    print("➡️ WEST")

    await right(page)

    for _ in range(ROWS_MAIN):
        await up(page)

    for _ in range(ROWS_MAIN):
        keyboard.wait("alt+s")

        take_screenshot_to_clipboard()
        await clean(page)
        await paste(page)
        await down(page)

    # ---------- EXTRA EAST ----------
    print("➡️ EXTRA EAST")

    await right(page)

    for _ in range(ROWS_EXTRA):
        keyboard.wait("alt+s")

        take_screenshot_to_clipboard()
        await clean(page)
        await paste(page)
        await down(page)

    # ---------- EXTRA WEST ----------
    print("➡️ EXTRA WEST")

    await right(page)

    for _ in range(ROWS_EXTRA):
        keyboard.wait("alt+s")

        take_screenshot_to_clipboard()
        await clean(page)
        await paste(page)
        await down(page)

    print("\n🔥 DONE")


# -------------------------
# MAIN
# -------------------------
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(DEBUG_URL)
        context = browser.contexts[0]

        # 👉 usa el tab activo
        page = context.pages[-1]

        print("✅ Connected to current Chrome tab")

        await run(page)

        await asyncio.sleep(999999)


if __name__ == "__main__":
    asyncio.run(main())