import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DEBUG_URL = "http://localhost:9222"

CYCLE_ICON_XPATH = (
    'xpath=//*[local-name()="svg"]'
    '[.//*[local-name()="use" and @*[local-name()="href"]="#icon-cycle"]]'
)

RERUN_BUTTON_XPATH = (
    'xpath=//span[normalize-space(.)="Rerun"]'
    '/ancestor::button[not(@disabled) and not(@aria-disabled="true")][1]'
)


def tab_label(page, index):
    title = page.url
    try:
        title = page.url.split("?")[0]
    except Exception:
        pass
    return f"Tab #{index} - {title}"


async def is_enabled_click_target(locator):
    try:
        return await locator.evaluate(
            """(node) => {
                const target = node.closest('button, [role="button"], a') || node;
                const disabled = target.disabled
                    || target.hasAttribute('disabled')
                    || target.getAttribute('aria-disabled') === 'true'
                    || target.classList.contains('disabled')
                    || target.classList.contains('_disabled');

                const style = window.getComputedStyle(target);
                return !disabled
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.pointerEvents !== 'none';
            }"""
        )
    except Exception:
        return False


async def get_visible_text(page):
    try:
        return await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""


async def find_enabled_cycle(frame):
    cycle_icons = frame.locator(CYCLE_ICON_XPATH)
    count = await cycle_icons.count()

    for idx in range(count):
        icon = cycle_icons.nth(idx)
        try:
            if await icon.is_visible(timeout=500) and await is_enabled_click_target(icon):
                return icon
        except Exception:
            continue

    return None


async def click_rerun(page):
    # First try the normal accessible button. If the app renders only the span,
    # fall back to the exact DOM shape used by the ux-button component.
    candidates = [
        page.get_by_role("button", name="Rerun"),
        page.locator(RERUN_BUTTON_XPATH),
    ]

    for frame in page.frames:
        candidates.append(frame.get_by_role("button", name="Rerun"))
        candidates.append(frame.locator(RERUN_BUTTON_XPATH))

    for candidate in candidates:
        try:
            if await candidate.count() == 0:
                continue

            button = candidate.first
            if await button.is_visible(timeout=1500) and await is_enabled_click_target(button):
                await button.click(timeout=5000)
                return True
        except Exception:
            continue

    return False


async def rerun_tab(page, index):
    print(f"\n🔍 Revisando {tab_label(page, index)}")

    try:
        await page.bring_to_front()
    except Exception:
        pass

    frames = [page.main_frame] + [frame for frame in page.frames if frame != page.main_frame]

    for frame in frames:
        try:
            text = await get_visible_text(frame)
            if "Execute" not in text and await frame.locator(CYCLE_ICON_XPATH).count() == 0:
                continue

            cycle = await find_enabled_cycle(frame)
            if not cycle:
                continue

            print("🔁 Cycle disponible. Dando click...")
            await cycle.click(timeout=5000)
            await asyncio.sleep(1)

            if await click_rerun(page):
                print("✅ Rerun clicked.")
                return "rerun"

            print("⚠️ Cycle clicked, pero no encontré botón Rerun disponible.")
            return "cycle_only"

        except PlaywrightTimeoutError as e:
            print(f"⚠️ Timeout revisando frame: {e}")
        except Exception as e:
            print(f"⚠️ No pude revisar un frame: {e}")

    print("⏭️ Sin cycle disponible en este tab.")
    return "skipped"


async def main():
    print("🔌 Conectando a Chromium existente (puerto 9222)...")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(DEBUG_URL)
        except Exception as e:
            print("❌ No pude conectar a Chromium por CDP en puerto 9222.")
            print(e)
            return

        pages = []
        for context in browser.contexts:
            pages.extend(context.pages)

        if not pages:
            print("❌ No hay tabs abiertos en Chromium.")
            return

        print(f"📑 Tabs encontrados: {len(pages)}")

        results = {
            "rerun": 0,
            "cycle_only": 0,
            "skipped": 0,
        }

        for index, page in enumerate(pages, start=1):
            status = await rerun_tab(page, index)
            results[status] = results.get(status, 0) + 1
            await asyncio.sleep(0.5)

        print("\n📌 Summary")
        print(f"✅ Rerun ejecutados: {results['rerun']}")
        print(f"⚠️ Cycle sin Rerun: {results['cycle_only']}")
        print(f"⏭️ Tabs skipped: {results['skipped']}")
        print("\n🔥 DONE")


if __name__ == "__main__":
    asyncio.run(main())
