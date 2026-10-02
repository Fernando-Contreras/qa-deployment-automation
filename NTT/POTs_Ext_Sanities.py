import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright

ENV_URLS = {
    "TRAINING": "https://wiki.example.com/display/QAPRJ/TRAINING+%7C+T2.3.30.2+-+07-APR",
    "PT2": "https://wiki.example.com/display/QAPRJ/PT2+%7C+T2.3.30.3+-+08-APR",
    "PT3": "https://wiki.example.com/display/QAPRJ/PT3+%7C+T2.3.30.3+-+08-APR",
    "PT4": "https://wiki.example.com/display/QAPRJ/PT4+%7C+T2.3.30.3+-+08-APR",
    "PT5": "https://wiki.example.com/display/QAPRJ/PT5+%7C+T2.3.30.3+-+08-APR",
    "SIT2": "https://wiki.example.com/display/QAPRJ/SIT2+East++%7C+T2.3.30.3+-+08-APR",
    "SIT4": "https://wiki.example.com/display/QAPRJ/SIT4+%7C+T2.3.30.3+-+08-APR",
    "UAT": "https://wiki.example.com/display/QAPRJ/UAT+%7C+T2.3.30.2+-+07-APR",
    "TS": "https://wiki.example.com/display/QAPRJ/TS+%7C+T2.3.30.1+-+06-APR",
}


# -------------------------
# PARSE INPUT (ORDEN REAL)
# -------------------------
def parse_input(text):
    version_match = re.search(r"T\d+\.\d+\.\d+(?:\.\d+)?", text)
    version = version_match.group(0) if version_match else "UNKNOWN"

    env_order = []
    seen = set()

    for line in text.splitlines():
        l = line.upper()

        def add(env):
            if env not in seen:
                env_order.append(env)
                seen.add(env)

        if "TRN" in l or "TRAINING" in l:
            add("TRAINING")
        elif "PT4" in l:
            add("PT4")
        elif "PT2" in l:
            add("PT2")
        elif "PT3" in l:
            add("PT3")
        elif "PT5" in l:
            add("PT5")
        elif "SIT2" in l:
            add("SIT2")
        elif "SIT4" in l:
            add("SIT4")
        elif "UAT" in l:
            add("UAT")
        elif re.search(r"\bTS\b", l):
            add("TS")

    return version, env_order


# -------------------------
# HELPERS
# -------------------------
def get_today():
    return datetime.now().strftime("%d-%b").upper()


def build_title(env, version, date):
    if env == "SIT2":
        return f"SIT2 East | {version} - {date}"
    return f"{env} | {version} - {date}"


# -------------------------
# COPY FLOW (SECUENCIAL)
# -------------------------
async def copy_page(context, url, title):
    page = await context.new_page()

    print(f"\n➡️ {title}")

    await page.goto(url)
    await page.wait_for_load_state("networkidle")

    # 3 dots
    await page.locator('[aria-label="More options"]').first.click()
    await asyncio.sleep(1)

    # Copy
    await page.locator('#action-copy-page-link').click()
    await page.wait_for_load_state("networkidle")

    # Checkbox
    await page.get_by_text("Include attached files and images").click()

    # Copy confirm
    await page.locator('#copy-dialog-next').click()
    await page.wait_for_load_state("networkidle")

    # Title
    await page.fill('input[name="title"]', title)

    print("📝 Ready (NOT published)")


# -------------------------
# MAIN
# -------------------------
async def main():
    print("\n📋 Paste input (triple Enter):\n")

    lines, empty = [], 0
    while True:
        line = input()
        if not line.strip():
            empty += 1
            if empty == 3:
                break
        else:
            empty = 0
        lines.append(line)

    raw_text = "\n".join(lines)

    version, envs = parse_input(raw_text)
    date = get_today()

    print(f"\n🧠 Version: {version}")
    print(f"🧩 Ordered Envs: {envs}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()

        # 🔥 SECUENCIAL (uno por uno)
        for env in envs:
            url = ENV_URLS.get(env)
            if not url:
                print(f"⚠️ No mapping for {env}")
                continue

            title = build_title(env, version, date)

            try:
                await copy_page(context, url, title)
                await asyncio.sleep(2)  # 🔥 anti-race condition
            except Exception as e:
                print(f"❌ {env} failed: {e}")

        print("\n🎉 DONE (all tabs ready, nothing published)")
        await asyncio.sleep(999999)


if __name__ == "__main__":
    asyncio.run(main())