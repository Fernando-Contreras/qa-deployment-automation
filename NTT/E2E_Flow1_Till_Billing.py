import re
import asyncio
import time
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page

import ntt_config
from nc_credentials import get_credentials

# Se reusa la lógica de Rerun_Sanity para no tener dos versiones
# del mismo XPath de cycle/Rerun.
try:
    import Rerun_Sanity as rerun_sanity
except Exception:
    rerun_sanity = None

# Para ir escribiendo las cuentas en Daily_Envs conforme salen, sin
# esperar a que terminen todas las tabs.
try:
    import Results_Sanity as results_sanity
except Exception:
    results_sanity = None

ATP_URL = (
    "https://atp.example.com/project/00000000-0000-4000-8000-000000000001/mia/execution?path=E2E%20Flows,Main%20Flows,E2E%20Flow1%20Till%20Billing"
)
RESULTS_SANITY_PATH = Path(__file__).with_name("Results_Sanity.py")

# Cuando Track_Deployments dispara esto sin terminal (Task Scheduler), lo
# hace vía la tarea 'E2E Sanity' — y schtasks /run no deja pasar argumentos
# dinámicos, así que los envs viajan por este archivo en vez de sys.argv.
PENDING_SANITY_ENVS_FILE = ntt_config.PENDING_SANITY_ENVS_FILE

# Cuántas ejecuciones se llenan al mismo tiempo. Se puede cambiar
# al vuelo con --parallel=N sin tocar el archivo.
DEFAULT_PARALLEL = 3
# Segundos entre el arranque de una tab y la siguiente, para no abrir
# todas de golpe contra ATP.
STAGGER_SECONDS = 2.0

# --- Espera del account_number y reruns automáticos ---
ACCOUNT_TIMEOUT_SECONDS = 600      # tope duro: 10 min y sigue de todos modos
ACCOUNT_POLL_SECONDS = 5
# Si ya cayó al menos una cuenta y pasa este rato sin ninguna novedad
# (ni cuenta nueva ni Rerun disparado), no se espera a la rezagada.
STALL_GRACE_SECONDS = 180
RERUN_AFTER_SECONDS = 90           # antes de esto no se toca nada (corrida normal)
RERUN_CHECK_EVERY = 20             # cada cuánto se revisa si hay Rerun disponible
RERUN_COOLDOWN_SECONDS = 90        # tras disparar un Rerun, dale chance de avanzar
MAX_RERUNS_PER_TAB = 2             # tope para no caer en un bucle de reruns

# La tarea 'E2E Sanity' de Task Scheduler es MultipleInstances=IgnoreNew:
# mientras este proceso siga vivo, un segundo trigger no arranca y los
# envs nuevos se quedan esperando en pending_sanity_envs.txt. Este sleep
# final es lo único que lo mantiene vivo (para no cerrar el navegador),
# así que su duración ES el tiempo máximo que un env nuevo puede esperar.
KEEP_BROWSER_OPEN_SECONDS = 600  # 10 min
# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
ATP_USERNAME, ATP_PASSWORD = get_credentials(
    "jenkins", default_username=ntt_config.JENKINS_DEFAULT_USER
)

# -------------------------
# Helpers
# -------------------------
def normalize_envs(txt: str):
    envs = [x.strip() for x in txt.split(",") if x.strip()]
    return [e.zfill(2) for e in envs]


def get_envs_from_args():
    """
    Envs que llegan por línea de comandos: así los manda Track_Deployments
    (uso interactivo) y ya no hay que pegarlos a mano.

    Si no hay argumentos, se revisa PENDING_SANITY_ENVS_FILE — así llegan
    cuando Track_Deployments dispara esto sin terminal, a través de la tarea
    'E2E Sanity' (se consume una sola vez: se borra al leerlo).

    Si tampoco hay archivo, regresa "" y se preguntan normal (uso manual).
    """
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return ", ".join(args).strip()

    try:
        if PENDING_SANITY_ENVS_FILE.exists():
            content = PENDING_SANITY_ENVS_FILE.read_text(encoding="utf-8").strip()
            PENDING_SANITY_ENVS_FILE.unlink()
            if content:
                return content
    except Exception as e:
        print(f"⚠ No pude leer {PENDING_SANITY_ENVS_FILE}: {e}")

    return ""


def get_parallel_from_args():
    """
    Cuántas ejecuciones corren al mismo tiempo: --parallel=4 (o -p4).
    Si algo se pone raro, bajarlo a 2 o 1 (1 = como antes, en fila).
    """
    for a in sys.argv[1:]:
        m = re.match(r"^--parallel=(\d+)$|^-p=?(\d+)$", a)
        if m:
            valor = int(m.group(1) or m.group(2))
            return max(1, min(valor, 8))
    return DEFAULT_PARALLEL


async def click_saml_in_iframe(page: Page, timeout_seconds: int = 120):
    print("⏳ Buscando iframe con SAML...")
    end = time.time() + timeout_seconds

    while time.time() < end:
        for fr in page.frames:
            try:
                btn = fr.get_by_text("SAML Authentication", exact=True)
                if await btn.count() > 0:
                    print("🔵 SAML encontrado en iframe → click")
                    await btn.first.click()
                    return True
            except Exception:
                pass

        await asyncio.sleep(0.5)

    print("⚠️ No se encontró SAML en el tiempo dado.")
    return False


async def _find_input_from_node(frame_or_page, node_locator_text: str):
    try:
        exact = frame_or_page.locator(
            f'xpath=//*[normalize-space(text())="{node_locator_text}"]/following::input[1]'
        )
        if await exact.count() > 0:
            return exact.first

        contains = frame_or_page.locator(
            f'xpath=//*[contains(normalize-space(.),"{node_locator_text}")]/following::input[1]'
        )
        if await contains.count() > 0:
            return contains.first
    except Exception:
        return None

    return None


async def find_input_for_label_across_frames(page: Page, label_text: str, timeout_seconds: int = 120):
    end = time.time() + timeout_seconds

    while time.time() < end:
        try:
            loc = await _find_input_from_node(page, label_text)
            if loc:
                return loc
        except Exception:
            pass

        for fr in page.frames:
            try:
                loc = await _find_input_from_node(fr, label_text)
                if loc:
                    return loc
            except Exception:
                pass

        await asyncio.sleep(0.5)

    return None


async def click_and_fill(locator, value: str):
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass

    try:
        await locator.wait_for(state="visible", timeout=5000)
    except Exception:
        pass

    try:
        await locator.click()
    except Exception:
        try:
            await locator.focus()
        except Exception:
            pass

    await locator.fill(value)


async def find_and_click_env_option(page: Page, option_text: str, timeout_seconds: int = 20):
    end = time.time() + timeout_seconds
    while time.time() < end:
        try:
            opt = page.get_by_text(option_text, exact=True)
            if await opt.count() > 0:
                await opt.first.scroll_into_view_if_needed()
                await opt.first.click()
                return True
        except Exception:
            pass

        for fr in page.frames:
            try:
                opt = fr.get_by_text(option_text, exact=True)
                if await opt.count() > 0:
                    await opt.first.scroll_into_view_if_needed()
                    await opt.first.click()
                    return True
            except Exception:
                pass

        await asyncio.sleep(0.4)
    return False


# 🔥 BUSCADOR ROBUSTO DE BREAKDOWN (ANTI VIRTUAL SCROLL)
async def find_breakdown_input(page: Page, code_name: str, timeout_seconds: int = 60):
    """
    Scrollea desde ARRIBA hacia abajo poco a poco hasta que el span del breakdown
    aparezca en el DOM. Cuando aparece, baja al input con following::input[1].
    """
    end = time.time() + timeout_seconds

    # XPath correcto según tu DOM real
    xpath_input_from_label = (
        f'//span[contains(@class,"taLabel") and contains(normalize-space(.), "{code_name}")]'
        f'/following::input[1]'
    )

    # Empieza desde arriba siempre
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass

    while time.time() < end:
        # 1) Intentar en la página principal
        try:
            loc = page.locator(f'xpath={xpath_input_from_label}')
            if await loc.count() > 0:
                inp = loc.first
                await inp.scroll_into_view_if_needed()
                await inp.wait_for(state="visible", timeout=5000)
                return inp
        except Exception:
            pass

        # 2) Intentar en iframes
        for fr in page.frames:
            try:
                loc = fr.locator(f'xpath={xpath_input_from_label}')
                if await loc.count() > 0:
                    inp = loc.first
                    await inp.scroll_into_view_if_needed()
                    await inp.wait_for(state="visible", timeout=5000)
                    return inp
            except Exception:
                pass

        # 3) Scroll progresivo hacia abajo
        try:
            current_scroll = await page.evaluate("() => window.scrollY")
            await page.mouse.wheel(0, 600)
            await asyncio.sleep(0.3)

            new_scroll = await page.evaluate("() => window.scrollY")
            if new_scroll == current_scroll:
                # Ya no puede scrollear más → no va a aparecer
                break
        except Exception:
            pass

    return None


async def scroll_and_click_sba_uni(page: Page, tag: str = ""):
    def log(msg):
        print(f"{tag} {msg}".strip())

    log("🔽 Scrolling to SBA_UNI option...")

    TARGET_TEXT = "SBA_UNI Create Customer Account Product Event Source Common"
    LOAD_ERROR_TEXTS = ("LOAD FILE FAILED", "Http failure response")

    end = time.time() + 60
    while time.time() < end:
        locator = page.get_by_text(TARGET_TEXT, exact=True)
        if await locator.count() > 0:
            await locator.first.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
            await locator.first.click()
            log("✅ SBA_UNI selected.")
            return

        for fr in page.frames:
            try:
                loc = fr.get_by_text(TARGET_TEXT, exact=True)
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await loc.first.click()
                    log("✅ SBA_UNI selected (iframe).")
                    return
            except Exception:
                pass

        load_error_visible = False
        try:
            page_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            load_error_visible = any(error_text in page_text for error_text in LOAD_ERROR_TEXTS)
        except Exception:
            pass

        for fr in page.frames:
            try:
                frame_text = await fr.evaluate("() => document.body ? document.body.innerText : ''")
                if any(error_text in frame_text for error_text in LOAD_ERROR_TEXTS):
                    load_error_visible = True
                    break
            except Exception:
                pass

        if load_error_visible:
            log("⚠️ LOAD FILE FAILED visible; scrolling a bit lower to find the account section...")
            await page.mouse.wheel(0, 450)
        else:
            await page.mouse.wheel(0, 1000)

        await asyncio.sleep(0.4)

    log("⚠️ SBA_UNI option not found.")


# -------------------------
# Espera inteligente: en vez de dormir 5 min fijos, revisa cuándo
# cada tab ya terminó SBA_UNI y muestra su account_number.
# Se usan los mismos patrones que Results_Sanity.py para que
# "listo aquí" signifique "Results_Sanity ya lo puede leer".
# -------------------------
ACCOUNT_JSON_RE = re.compile(r'"account_number"\s*:\s*"(\d+)"')
ACCOUNT_NUM_HEADER_RE = re.compile(r"\bACCOUNT_NUM\b", re.IGNORECASE)
ACCOUNT_NUM_VALUE_RE = re.compile(r"\b(?:101|201)\d{6,}\b")


async def read_all_text(page: Page) -> str:
    texts = []
    try:
        t = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if t:
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

    return "\n".join(texts)


def extract_account_from_text(text: str):
    m = ACCOUNT_JSON_RE.search(text)
    if m:
        return m.group(1)

    header = ACCOUNT_NUM_HEADER_RE.search(text)
    if not header:
        return None

    value = ACCOUNT_NUM_VALUE_RE.search(text[header.end():])
    return value.group(0) if value else None


async def try_rerun(page: Page) -> bool:
    """
    Si ATP dejó un cycle habilitado en esta tab, le da click y luego al
    botón Rerun. Es lo mismo que hace Rerun_Sanity, pero SIN
    bring_to_front: aquí hay varias tabs esperando y no queremos que
    anden brincando al frente solas.
    """
    if rerun_sanity is None:
        return False

    frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]

    for frame in frames:
        try:
            if await frame.locator(rerun_sanity.CYCLE_ICON_XPATH).count() == 0:
                continue

            cycle = await rerun_sanity.find_enabled_cycle(frame)
            if not cycle:
                continue

            await cycle.click(timeout=5000)
            await asyncio.sleep(1)

            return await rerun_sanity.click_rerun(page)
        except Exception:
            continue

    return False


def guardar_en_daily_envs(acumulado):
    """
    Manda a Daily_Envs lo que se lleva hasta ahora. Se pasa el acumulado
    COMPLETO (no solo lo nuevo) porque la línea se reescribe entera: si
    mandara solo el EAST, borraría el WEST ya puesto.
    """
    if results_sanity is None or not acumulado:
        return

    try:
        tags = results_sanity.update_daily_envs(acumulado)
        if tags:
            print(f"💾 Daily_Envs actualizado: {', '.join(sorted(tags))}")
    except Exception as e:
        print(f"⚠️ No pude escribir Daily_Envs ahorita ({e}); se reintenta luego.")


async def wait_for_accounts(
    tagged_pages,
    timeout_seconds: int = ACCOUNT_TIMEOUT_SECONDS,
    poll_seconds: float = ACCOUNT_POLL_SECONDS,
):
    """
    Revisa cada pocos segundos si cada tab ya tiene account_number.

    Cada cuenta que aparece se escribe de una vez en Daily_Envs, para que
    una tab atorada no se lleve entre las patas a todas las demás.

    A las tabs que llevan rato sin dar cuenta les busca un Rerun y lo
    dispara. Y si ya no hay novedades por un buen rato, deja de esperar
    a las rezagadas en vez de agotar el timeout completo.

    Recibe triples (tag, page, env_num) de las ejecuciones que sí llegaron
    a Execute.
    """
    items = [(tag, p, env) for tag, p, env in tagged_pages if p and not p.is_closed()]
    if not items:
        print("⚠️ No hay tabs válidas para revisar.")
        return False

    labels = {p: tag for tag, p, _ in items}
    envs_por_page = {p: env for _, p, env in items}
    todas = [p for _, p, _ in items]

    ready = {}          # page -> account_number
    reruns = {}         # page -> cuántos Rerun se le dispararon
    next_check = {}     # page -> segundo a partir del cual se le puede revisar Rerun
    acumulado = {}      # env_num -> {"EAST": .., "WEST": ..}
    start = time.time()
    ultimo_avance = 0.0

    print(
        f"⏳ Esperando account_number en {len(todas)} tab(s) "
        f"(máx {timeout_seconds // 60} min; reruns automáticos tras {RERUN_AFTER_SECONDS}s)..."
    )

    while time.time() - start < timeout_seconds:
        activos = [p for p in todas if not p.is_closed()]
        pendientes = [p for p in activos if p not in ready]

        # Todas las tabs se leen a la vez: con 12 tabs, leerlas en fila
        # se comía varios segundos de cada vuelta.
        textos = await asyncio.gather(
            *(read_all_text(p) for p in pendientes),
            return_exceptions=True,
        )

        hubo_cuenta_nueva = False

        for p, texto in zip(pendientes, textos):
            if isinstance(texto, Exception):
                continue
            account = extract_account_from_text(texto)
            if not account:
                continue

            ready[p] = account
            hubo_cuenta_nueva = True
            print(
                f"✅ {len(ready)}/{len(activos)} {labels.get(p, '')} "
                f"→ account {account} ({int(time.time() - start)}s)"
            )

            # 101... es EAST y 201... es WEST, igual que en Results_Sanity.
            lado = "EAST" if account.startswith("101") else "WEST" if account.startswith("201") else None
            env_num = envs_por_page.get(p)
            if lado and env_num:
                acumulado.setdefault(env_num, {})[lado] = account

        transcurrido = time.time() - start

        if hubo_cuenta_nueva:
            ultimo_avance = transcurrido
            # Se guarda de inmediato: si esto truena o le das Ctrl+C,
            # las cuentas que ya salieron no se pierden.
            guardar_en_daily_envs(acumulado)

        if activos and len(ready) >= len(activos):
            print(f"🎯 Todos los account_number listos en {int(transcurrido)}s.")
            await asyncio.sleep(2)  # deja que el DOM termine de asentar
            return True

        # --- Reruns automáticos para las que se quedaron atoradas ---
        for p in [q for q in activos if q not in ready]:
            if reruns.get(p, 0) >= MAX_RERUNS_PER_TAB:
                continue
            if transcurrido < next_check.get(p, RERUN_AFTER_SECONDS):
                continue

            # Se marca antes de intentar, para no reintentar en cada vuelta.
            next_check[p] = transcurrido + RERUN_CHECK_EVERY
            try:
                if await try_rerun(p):
                    reruns[p] = reruns.get(p, 0) + 1
                    next_check[p] = transcurrido + RERUN_COOLDOWN_SECONDS
                    ultimo_avance = transcurrido  # un rerun cuenta como avance
                    print(
                        f"🔁 {labels.get(p, '')} Rerun disparado "
                        f"({reruns[p]}/{MAX_RERUNS_PER_TAB}) a los {int(transcurrido)}s."
                    )
            except Exception:
                pass

        # --- Ya no vale la pena seguir esperando a las rezagadas ---
        if ready and (transcurrido - ultimo_avance) >= STALL_GRACE_SECONDS:
            faltantes = [labels.get(p, "?") for p in activos if p not in ready]
            print(
                f"⏭️ {int(transcurrido)}s: {len(ready)}/{len(activos)} listas y "
                f"{STALL_GRACE_SECONDS // 60} min sin novedades. No espero más a: "
                + ", ".join(faltantes)
            )
            return False

        await asyncio.sleep(poll_seconds)

    faltantes = [labels.get(p, "?") for p in todas if not p.is_closed() and p not in ready]
    if faltantes:
        print(
            f"⚠️ Timeout de {timeout_seconds}s. Sin account_number: "
            + ", ".join(faltantes)
            + ". Sigo de todos modos."
        )
    return False


async def select_payment_method(page: Page, tag: str = ""):
    def log(msg):
        print(f"{tag} {msg}".strip())

    log("💳 Selecting Payment Method → General Invoice")

    pm = await find_input_for_label_across_frames(page, "Payment Method", timeout_seconds=60)
    if not pm:
        log("⚠️ Payment Method not found")
        return

    await pm.scroll_into_view_if_needed()
    await pm.click()
    await asyncio.sleep(0.5)

    opt = page.get_by_text("General Invoice", exact=True)
    if await opt.count() > 0:
        await opt.first.scroll_into_view_if_needed()
        await opt.first.click()
        return

    for fr in page.frames:
        try:
            opt = fr.get_by_text("General Invoice", exact=True)
            if await opt.count() > 0:
                await opt.first.scroll_into_view_if_needed()
                await opt.first.click()
                return
        except Exception:
            pass

    log("⚠️ Could not select General Invoice")


async def handle_basic_auth(page: Page):
    try:
        if "atp.example.com" in page.url:
            if ATP_USERNAME not in page.url:
                auth_url = re.sub(
                    r"^https://",
                    f"https://{ATP_USERNAME}:{ATP_PASSWORD}@",
                    page.url,
                    count=1
                )
                await page.goto(auth_url, timeout=120000)
    except Exception:
        pass


async def full_flow(page: Page, env_numeric: str, do_tenant: bool, tag: str = ""):
    def log(msg):
        print(f"{tag} {msg}".strip())

    # ENV
    env_locator = await find_input_for_label_across_frames(page, "Environment", timeout_seconds=120)
    if not env_locator:
        log("⚠️ Could not locate Environment input. Refreshing and retrying...")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=120000)
        except Exception:
            try:
                await page.reload(timeout=120000)
            except Exception:
                pass

        env_locator = await find_input_for_label_across_frames(page, "Environment", timeout_seconds=120)
        if not env_locator:
            log("❌ Could not locate Environment input after refresh")
            raise RuntimeError("Environment input no encontrado")

    await click_and_fill(env_locator, env_numeric)
    await asyncio.sleep(0.4)

    env_option_text = re.compile(rf"nttcw{env_numeric}_qa", re.IGNORECASE)
    await find_and_click_env_option(page, env_option_text)
    # Al elegir el env la UI vuelve a armar los campos de abajo; este es
    # de los pocos sleeps que sí hacen falta, para no agarrar el DOM viejo.
    await asyncio.sleep(1.2)

    # TENANT
    if do_tenant:
        tenant = await find_input_for_label_across_frames(page, "tenant", timeout_seconds=120)
        if tenant:
            await tenant.scroll_into_view_if_needed()
            await tenant.click()
            await asyncio.sleep(0.3)
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.2)
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

    # IF
    IF_LABEL = "IF (for IF51 Prime Flag should always be Y)"
    if_locator = await find_input_for_label_across_frames(page, IF_LABEL, timeout_seconds=120)
    if if_locator:
        await click_and_fill(if_locator, "84")
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")

    await asyncio.sleep(0.5)

    # PAYMENT METHOD
    await select_payment_method(page, tag=tag)
    await asyncio.sleep(0.5)

    # BREAKDOWN CODES
    log("🧩 Filling breakdown codes...")

    b1 = await find_breakdown_input(page, "breakdown_code1")
    if b1:
        await click_and_fill(b1, "0001")
    else:
        log("⚠️ breakdown_code1 not found")

    b3 = await find_breakdown_input(page, "breakdown_code3")
    if b3:
        await click_and_fill(b3, "0020")
    else:
        log("⚠️ breakdown_code3 not found")

    b2 = await find_breakdown_input(page, "breakdown_code2")
    if b2:
        await click_and_fill(b2, "0020")
    else:
        log("⚠️ breakdown_code2 not found")

    await asyncio.sleep(0.5)

    # EXECUTE
    btn = page.get_by_role("button", name="Execute")
    if await btn.count() > 0:
        await btn.first.click()
    else:
        for fr in page.frames:
            b = fr.get_by_role("button", name="Execute")
            if await b.count() > 0:
                await b.first.click()
                break
            
    # SBA_UNI
    await scroll_and_click_sba_uni(page, tag=tag)
    await asyncio.sleep(0.5)

    log("✅ Execute clicked.")


async def ensure_logged_in(context) -> Page:
    """
    El SAML se hace UNA sola vez. Las cookies quedan en el context,
    así que todas las tabs que se abran después ya entran logueadas.
    Devuelve la tab lista, que se reusa para la primera ejecución.
    """
    page = await context.new_page()
    print("🌍 Opening ATP page...")
    await page.goto(ATP_URL, timeout=1200000)
    await handle_basic_auth(page)

    print("🔍 Checking iframe SAML...")
    await click_saml_in_iframe(page)
    await asyncio.sleep(2)
    try:
        page = context.pages[-1]
    except Exception:
        pass

    # Espera activa a que la app quede usable, en vez de dormir a ciegas.
    if not await find_input_for_label_across_frames(page, "Environment", timeout_seconds=180):
        print("⚠️ La app no terminó de cargar tras el login; sigo de todos modos.")

    # El login puede dejar la tab vieja abierta; estorba al contar accounts.
    for p in list(context.pages):
        if p is not page and not p.is_closed():
            try:
                await p.close()
            except Exception:
                pass

    print("🔓 Sesión lista.")
    return page


async def run_execution(context, env_numeric, do_tenant, sem, tag, stagger, page=None):
    """
    Una ejecución completa. Devuelve (tag, page, env_numeric) si llegó a
    Execute, o None si tronó (para no contarla al esperar el account_number).
    """
    if stagger:
        await asyncio.sleep(stagger)

    async with sem:
        try:
            if page is None:
                page = await context.new_page()
                await page.goto(ATP_URL, timeout=300000)
                await handle_basic_auth(page)

            kind = "SECOND (tenant)" if do_tenant else "FIRST"
            print(f"\n{tag} ▶️ Env {env_numeric} — {kind} EXECUTION")
            await full_flow(page, env_numeric, do_tenant=do_tenant, tag=tag)
            return (tag, page, env_numeric)
        except Exception as e:
            print(f"{tag} ❌ Falló esta ejecución: {e}")
            return None


# -------------------------
# MAIN
# -------------------------
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--remote-debugging-port=9222",
                # Sin estos flags Chromium congela las tabs que no están
                # al frente, y el paralelo saldría más lento que el serial.
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion",
            ],
        )
        context = await browser.new_context(
            viewport=None,
            http_credentials={"username": ATP_USERNAME, "password": ATP_PASSWORD}
        )

        env_raw = get_envs_from_args()

        if env_raw:
            print(f"\n🧩 Envs recibidos de Track_Deployments: {env_raw}")
        else:
            env_raw = input("\n🧩 Envs (e.g. 06,11,74): ").strip()

        envs = normalize_envs(env_raw)
        if not envs:
            print("❌ No envs provided.")
            return

        parallel = get_parallel_from_args()

        # Login una sola vez; esa tab se reusa para la primera ejecución.
        first_page = await ensure_logged_in(context)

        # Intercalado por env (15A, 15B, 61A, 61B, ...) para que las tabs
        # queden ordenadas. Nota: con --parallel >= 2 las dos ejecuciones
        # del mismo env pueden correr al mismo tiempo (no hay dependencia
        # de datos entre A y B, cada una llena su propio formulario).
        specs = [pair for env in envs for pair in ((env, False), (env, True))]
        total = len(specs)
        sem = asyncio.Semaphore(parallel)

        print(f"\n🚀 {total} ejecuciones, {parallel} al mismo tiempo.")

        jobs = []
        for i, (env_numeric, do_tenant) in enumerate(specs):
            tag = f"[{i + 1}/{total} {env_numeric}{'W' if do_tenant else 'E'}]"
            jobs.append(
                run_execution(
                    context,
                    env_numeric,
                    do_tenant,
                    sem,
                    tag,
                    stagger=min(i, parallel - 1) * STAGGER_SECONDS,
                    page=first_page if i == 0 else None,
                )
            )

        started = time.time()
        results = await asyncio.gather(*jobs)
        ok_pages = [r for r in results if r is not None]

        fallidas = total - len(ok_pages)
        print(f"\n🎉 {len(ok_pages)}/{total} ejecuciones lanzadas en {int(time.time() - started)}s.")
        if fallidas:
            print(f"⚠️ {fallidas} no llegaron a Execute; revisa sus tabs.")

        await wait_for_accounts(ok_pages)

        print("Running Results_Sanity.py...")
        try:
            subprocess.run([sys.executable, str(RESULTS_SANITY_PATH)], check=True)
        except Exception as e:
            print("Could not run Results_Sanity.py")
            print(e)

        await asyncio.sleep(KEEP_BROWSER_OPEN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
