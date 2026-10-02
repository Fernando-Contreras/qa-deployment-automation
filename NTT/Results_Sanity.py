import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright

import ntt_config

DAILY_ENVS_PATH = str(ntt_config.DAILY_ENVS_PATH)
ORGANIZE_RESULTS_PATH = Path(__file__).with_name("OrganizeResults.py")
RERUN_SANITY_PATH = Path(__file__).with_name("Rerun_Sanity.py")

ACCOUNT_REGEX = re.compile(r'"account_number"\s*:\s*"(\d+)"')
ACCOUNT_NUM_HEADER_RE = re.compile(r"\bACCOUNT_NUM\b", re.IGNORECASE)
ACCOUNT_NUM_VALUE_RE = re.compile(r"\b(?:101|201)\d{6,}\b")
STATUS_HEADER_RE = re.compile(
    r"^\s*(?:\S+\s+)?\*\*(PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*:\s*$"
)
PASSED_ENV_WITH_ACCOUNTS_RE = re.compile(
    r"\[(?:\*\*)?((?:COM)?\d+QA)(?:\*\*)?\]\([^)]+\)(?:\*\*)?"
    r".*?\(([^)]+)\)"
    r".*?\bEAST:\s*(\d+)"
    r".*?\bWEST:\s*(\d+)",
    re.IGNORECASE,
)
ENV_LABEL_RE = re.compile(r"\[(?:\*\*)?((?:COM)?\d+QA)(?:\*\*)?\]", re.IGNORECASE)

PASSED_ENVS_BEFORE = set()
UPDATED_ENV_TAGS = set()


def extract_account_from_text(text: str):
    match = ACCOUNT_REGEX.search(text)
    if match:
        return match.group(1)

    header_match = ACCOUNT_NUM_HEADER_RE.search(text)
    if not header_match:
        return None

    text_after_header = text[header_match.end():]
    value_match = ACCOUNT_NUM_VALUE_RE.search(text_after_header)
    return value_match.group(0) if value_match else None

async def extract_env_and_account(page):
    env_value = None
    account_value = None

    # 1) Leer input Environment
    try:
        env_input = page.locator(
            'xpath=//span[normalize-space(.)="Environment"]/ancestor::label//input'
        )
        if await env_input.count() > 0:
            env_value = await env_input.first.input_value()
    except Exception:
        pass

    if not env_value:
        for fr in page.frames:
            try:
                env_input = fr.locator(
                    'xpath=//span[normalize-space(.)="Environment"]/ancestor::label//input'
                )
                if await env_input.count() > 0:
                    env_value = await env_input.first.input_value()
                    break
            except Exception:
                pass

    # 2) Buscar account_number en el DOM
    texts = []
    try:
        t = await page.evaluate("() => document.body.innerText || ''")
        texts.append(t)
    except Exception:
        pass

    for fr in page.frames:
        try:
            t = await fr.evaluate("() => document.body ? document.body.innerText : ''")
            if t:
                texts.append(t)
        except Exception:
            pass

    full_text = "\n".join(texts)
    account_value = extract_account_from_text(full_text)

    return env_value, account_value

def env_to_tag(env_value: str):
    # nttcw119_qa -> 119QA
    m = re.search(r"nttcw(\d+)_qa", env_value, re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).zfill(2)
    return f"{num}QA", num

def get_passed_envs_with_accounts():
    passed_envs = []
    current_status = None

    with open(DAILY_ENVS_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            status_match = STATUS_HEADER_RE.match(line.strip())
            if status_match:
                current_status = status_match.group(1)
                continue

            if current_status != "PASSED":
                continue

            env_match = PASSED_ENV_WITH_ACCOUNTS_RE.search(line)
            if env_match:
                env, version, east, west = env_match.groups()
                passed_envs.append((env, version, east, west))

    return passed_envs

def print_passed_envs_with_accounts():
    try:
        passed_envs = get_passed_envs_with_accounts()
    except Exception as e:
        print("⚠️ No pude leer PASSED envs desde Daily_Envs.txt")
        print(e)
        return []

    new_passed_envs = [
        (env, version, east, west)
        for env, version, east, west in passed_envs
        if env in UPDATED_ENV_TAGS and env not in PASSED_ENVS_BEFORE
    ]

    print("\n✅ Recién entraron a PASSED en Daily_Envs con EAST y WEST:")

    if not new_passed_envs:
        print("N/A")
        return []

    for env, version, east, west in new_passed_envs:
        print(f"- {env} ({version}) EAST: {east} WEST: {west}")

    return new_passed_envs

async def close_tabs_for_envs(env_tags):
    target_envs = {env for env, _, _, _ in env_tags}
    if not target_envs:
        return

    print("\n🧹 Cerrando tabs correspondientes en Chromium...")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print("⚠️ No pude conectar a Chromium para cerrar tabs.")
            print(e)
            return

        closed = []
        for context in browser.contexts:
            for page in list(context.pages):
                try:
                    env_value, _ = await extract_env_and_account(page)
                    if not env_value:
                        continue

                    env_info = env_to_tag(env_value)
                    if not env_info:
                        continue

                    tag, _ = env_info
                    if tag in target_envs:
                        await page.close()
                        closed.append(tag)
                        print(f"✅ Tab cerrada: {tag}")
                except Exception as e:
                    print(f"⚠️ No pude revisar/cerrar una tab: {e}")

        missing = sorted(target_envs - set(closed))
        if missing:
            print("⚠️ No encontré tabs abiertas para: " + ", ".join(missing))

def update_daily_envs(results):
    """
    Escribe EAST/WEST en Daily_Envs.txt.

    `results` es {env_num: {"EAST": "...", "WEST": "..."}} y debe venir
    COMPLETO por env: la línea se reescribe quitando los EAST/WEST previos,
    así que mandar solo EAST borraría el WEST que ya estuviera puesto.
    Es idempotente, se puede llamar varias veces.

    Devuelve el set de tags (ej. {"06QA"}) que sí se encontraron y actualizaron.

    Ojo: esto NO organiza el archivo a propósito. Es OrganizeResults quien
    promueve a PASSED las líneas que ya traen EAST y WEST, y de ese orden
    depende la detección de "recién entraron a PASSED".
    """
    if not results:
        return set()

    # --- Forzar "guardar" del archivo antes de modificarlo (equivalente a Ctrl+S) ---
    with open(DAILY_ENVS_PATH, "r", encoding="utf-8") as f:
        original = f.read()

    with open(DAILY_ENVS_PATH, "w", encoding="utf-8") as f:
        f.write(original)

    # Leer archivo ya "guardado"
    with open(DAILY_ENVS_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated = set()

    # Actualizar cada línea
    for i, line in enumerate(lines):
        for env_num, tag_accounts in results.items():
            tag = f"{env_num}QA"
            if re.search(rf"\[(?:\*\*)?{re.escape(tag)}(?:\*\*)?\]", line, re.IGNORECASE):
                updated.add(tag)

                # Extrae EAST y WEST si existen
                east = tag_accounts.get("EAST")
                west = tag_accounts.get("WEST")

                # Quitar EAST/WEST previos sin tocar espacios normales
                line = re.sub(r'\s*EAST:\s*\d+', '', line)
                line = re.sub(r'\s*WEST:\s*\d+', '', line)

                addition = ""
                if east:
                    addition += f" EAST: {east}"
                if west:
                    addition += f" WEST: {west}"

                # Quitar solo el salto de línea final, no espacios del medio
                line = line.rstrip("\n")

                lines[i] = line + addition + "\n"

    # Escribir archivo
    with open(DAILY_ENVS_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

    return updated


async def main():
    global PASSED_ENVS_BEFORE
    global UPDATED_ENV_TAGS

    PASSED_ENVS_BEFORE = {env for env, _, _, _ in get_passed_envs_with_accounts()}
    UPDATED_ENV_TAGS = set()

    print("🔌 Conectando a Chromium existente (puerto 9222)...")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print("❌ No pude conectar a Chromium por CDP en puerto 9222.")
            print(e)
            return

        contexts = browser.contexts
        if not contexts:
            print("❌ No hay contextos en el browser.")
            return

        pages = contexts[0].pages
        print(f"📑 Tabs encontrados: {len(pages)}")

        results = {}  # env_num -> {"EAST": ..., "WEST": ...}

        for idx, page in enumerate(pages, start=1):
            print(f"\n🔍 Revisando tab #{idx}...")

            env_value, account_value = await extract_env_and_account(page)

            if not env_value:
                print("⚠️ No pude leer el Environment de este tab.")
                continue

            # env_to_tag regresa None si no es nttcwNN_qa. Antes se desempacaba
            # directo y un env raro (p.ej. una pestaña COM) tronaba todo main():
            # las pestañas que venían después ya no se leían.
            env_info = env_to_tag(env_value)
            if not env_info:
                print(f"⚠️ Environment raro: {env_value}")
                continue
            tag, env_num = env_info

            if not account_value:
                print(f"⚠️ No pude leer account_number para {env_value}")
                continue

            # Determinar EAST/WEST por prefijo
            if account_value.startswith("101"):
                key = "EAST"
            elif account_value.startswith("201"):
                key = "WEST"
            else:
                print(f"⚠️ Prefijo desconocido para {account_value}")
                continue

            if env_num not in results:
                results[env_num] = {}
            results[env_num][key] = account_value

            print(f"✅ Env: {env_value} | {key}: {account_value}")

        if not results:
            print("\n⚠️ No se extrajo ningún env/account.")
            print("🎉 Listo.")
            return

        UPDATED_ENV_TAGS |= update_daily_envs(results)

        print("\n💾 Archivo actualizado correctamente con EAST y WEST en la misma línea.")
        print("🎉 Listo.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        print("\n🧹 Ejecutando OrganizeResults.py...")
        try:
            subprocess.run([sys.executable, str(ORGANIZE_RESULTS_PATH)], check=True)
        except Exception as e:
            print("⚠️ No se pudo ejecutar OrganizeResults.py")
            print(e)
        new_passed_envs = print_passed_envs_with_accounts()

        print("\nEjecutando Rerun_Sanity.py...")
        try:
            subprocess.run([sys.executable, str(RERUN_SANITY_PATH)], check=True)
        except Exception as e:
            print("No se pudo ejecutar Rerun_Sanity.py")
            print(e)

        if new_passed_envs:
            asyncio.run(close_tabs_for_envs(new_passed_envs))

        # Webex_Update.py ya corrió solo, colgado del OrganizeResults.py
        # de arriba (OrganizeResults es quien centraliza ese sync).

        print("\n🏁 Listo. Cerrando.")
        # La conexión CDP a Chromium deja hilos de fondo vivos y el
        # proceso no termina solo. Como ya no queda nada pendiente,
        # se fuerza la salida en vez de esperar un Ctrl+C manual.
        sys.stdout.flush()
        os._exit(0)

