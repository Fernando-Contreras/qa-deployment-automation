"""Diagnostic companion for PIM_Validation.py — READ-ONLY.

Opens a TOMS Product Offerings page, then watches while you work the
filter by hand ONCE. It records:

  * exactly which element you clicked (plus its parents, with their
    onclick/href handlers) — that's what's needed to drive the filter
    from JavaScript instead of an OS-level mouse click;
  * the network requests the page fires — which shows whether the search
    can be done straight from a URL.

Nothing here changes PIM_Validation.py or touches Word.
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import PIM_Validation as p

OUT_PATH = os.path.join(p.DOWNLOADS, "pim_filter_probe.txt")

# Registered in every frame, before any page script runs. It has to be
# self-invoking: add_init_script EVALUATES this text, so a bare arrow
# function would just be created and thrown away.
CLICK_HOOK = """
(() => {
  if (window.__pimHooked) return;
  window.__pimHooked = true;

  document.addEventListener('click', (e) => {
    const chain = [];
    let el = e.target;

    for (let i = 0; el && i < 6; i++) {
      const attrs = {};
      if (el.attributes) {
        for (const a of el.attributes) {
          attrs[a.name] = a.value.length > 300
            ? a.value.slice(0, 300) + '...' : a.value;
        }
      }
      chain.push({
        tag: el.tagName,
        attrs: attrs,
        text: (el.textContent || '').trim().slice(0, 80),
        html: (el.outerHTML || '').slice(0, 500)
      });
      el = el.parentElement;
    }

    try {
      window.__pimReport(JSON.stringify({
        frame: location.href,
        chain: chain
      }));
    } catch (err) {}
  }, true);
})();
"""

SKIP_SUFFIXES = (
    ".gif", ".png", ".jpg", ".jpeg", ".css", ".ico", ".woff", ".woff2"
)


def write(handle, text):
    print(text)
    handle.write(text + "\n")
    handle.flush()


async def main():

    target = input(
        "\nPaste the TOMS address or any URL on that box "
        "(e.g. http://192.0.2.4:6300): "
    ).strip()

    object_url = p.build_object_url(target)

    if not object_url:
        print("❌ Couldn't turn that into a URL.")
        return

    out = open(OUT_PATH, "w", encoding="utf-8")

    write(out, f"# PIM filter probe — {datetime.now():%Y-%m-%d %H:%M:%S}")
    write(out, f"# target: {object_url}\n")

    async with async_playwright() as pw:

        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        clicks = {"n": 0}

        async def on_click(payload):
            clicks["n"] += 1
            data = json.loads(payload)

            write(out, "\n" + "=" * 70)
            write(out, f"CLICK #{clicks['n']}")
            write(out, f"frame: {data['frame']}")

            for depth, node in enumerate(data["chain"]):
                write(out, f"\n  [{depth}] <{node['tag']}>  text={node['text']!r}")
                for name, value in node["attrs"].items():
                    write(out, f"        {name} = {value!r}")
                write(out, f"        html: {node['html']}")

        await page.expose_function("__pimReport", on_click)
        await page.add_init_script(CLICK_HOOK)

        def on_request(request):
            url = request.url
            if url.lower().endswith(SKIP_SUFFIXES):
                return
            line = f"[net] {request.method} {url}"
            if request.method == "POST":
                data = request.post_data
                if data:
                    line += f"\n      body: {data[:600]}"
            write(out, line)

        page.on("request", on_request)

        await page.goto(object_url, wait_until="domcontentloaded", timeout=120000)
        await p.login_if_needed(page)

        print(
            """
==========================================================
Now, in the browser window that just opened:

  1. Get to the Product Offerings list as you normally would.
  2. Click the FILTER icon (the one the script can never hit).
  3. Type an offering name into it.
  4. Click Apply.

Everything you click is being recorded, along with the requests
the page makes.

When the results come back, press Ctrl+C here.
==========================================================
"""
        )

        try:
            await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            write(out, f"\n# clicks captured: {clicks['n']}")
            out.close()
            print(f"\n💾 Written to: {OUT_PATH}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
