import os
import socket
import subprocess
import time

import pyautogui
from playwright.sync_api import sync_playwright

from nc_credentials import get_credentials

# 🔥 Evitar fail-safe
pyautogui.FAILSAFE = False

# ---------------- CONFIG ----------------
PUTTY_PATH = r"C:\Program Files\PuTTY\putty.exe"

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9222
# Perfil dedicado: NO toca tu Chrome personal y permite el puerto de debug.
CHROME_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "Temp", "ts_sanity_chrome")

# 192.0.2.4 NO es alcanzable directo: se llega por el tunel SOCKS5 que abre
# el PuTTY de ATP_SIT_Step2. En tu Chrome normal eso lo hacia la extension
# SwitchyOmega (perfil "SIT ATP"); aqui se lo pasamos a Chrome por linea de
# comandos, asi no dependemos de ninguna extension.
TUNNEL_PORT = 8082
SOCKS_PROXY = f"socks5://127.0.0.1:{TUNNEL_PORT}"

URL = "https://192.0.2.4:8082/project/00000000-0000-4000-8000-000000000004/plan/00000000-0000-4000-8000-000000000005/scopes/00000000-0000-4000-8000-000000000006"

# Login de Keycloak del ATP de SIT. Ya no va escrito aquí: vive cifrado como
# las demás cuentas (py Setup_Credentials.py atp_sit_web). La primera vez se pide.
ATP_SIT_WEB_ACCOUNT = "atp_sit_web"

# Valores del modal de Run
ENVIRONMENT_VALUE = "TS"
TA_TOOLS_VALUE = "Main Group"

# El "modal" de Run en realidad es un ux-popover, no un dialog ni un
# cdk-overlay. Sirve para distinguir el boton Run del popover del de la pagina
# (los dos se llaman igual).
MODAL = 'contains(@class,"atp-execute-scope")'

# La tabla tiene 3 secciones con checkbox de "seleccionar todo":
# Prerequisites, Execution y Validation. Validation NO se marca.
# None = marcar todas.
SECCIONES = ["Prerequisites", "Execution"]

# Casos que NO deben ejecutarse: se marca toda la sección y luego se
# destildan estos uno por uno. Basta con parte del nombre.
FILAS_A_EXCLUIR = [
    "[WEST] E2E Payment IF903 IF904",
]

SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ts_sanity_shots")


# ---------------- KEYBOARD ----------------
def switch_keyboard_to_english():
    print("🌎 Switching keyboard to EN...")
    pyautogui.keyDown("alt")
    pyautogui.keyDown("shift")
    pyautogui.press("9")
    pyautogui.keyUp("shift")
    pyautogui.keyUp("alt")
    time.sleep(0.8)


def switch_keyboard_to_spanish():
    print("🌎 Switching keyboard to ES...")
    pyautogui.keyDown("alt")
    pyautogui.keyDown("shift")
    pyautogui.press("0")
    pyautogui.keyUp("shift")
    pyautogui.keyUp("alt")
    time.sleep(0.8)


# ---------------- RED ----------------
def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_for_port(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(1)
    return False


# ---------------- PUTTY ----------------
def open_putty(session, username, password, tunnel_port=None):
    # Si el tunel de esa sesion ya esta arriba, no abrimos otro PuTTY.
    if tunnel_port and port_open(tunnel_port):
        print(f"♻️ PuTTY '{session}' ya tiene el túnel {tunnel_port} arriba, lo reuso.")
        return

    print(f"🚀 Opening PuTTY: {session}")
    subprocess.Popen([PUTTY_PATH, "-load", session])

    time.sleep(2)

    pyautogui.write(username, interval=0.04)
    pyautogui.press("enter")
    time.sleep(1)

    for c in password:
        if c == "@":
            pyautogui.keyDown("shift")
            pyautogui.press("2")
            pyautogui.keyUp("shift")
        else:
            pyautogui.write(c)
        time.sleep(0.03)

    pyautogui.press("enter")
    time.sleep(1.5)


# ---------------- HELPERS PLAYWRIGHT ----------------
def check_tunnel(step):
    """
    Si PuTTY se cae, Chrome empieza a dar ERR_PROXY_CONNECTION_FAILED y todo
    lo demás falla con errores raros. Mejor detectarlo y decirlo claro.
    """
    if port_open(TUNNEL_PORT):
        return True
    print(f"❌ El túnel SOCKS5 ({TUNNEL_PORT}) se cayó durante: {step}")
    print("   → Revisa el PuTTY de ATP_SIT_Step2 (y que la VPN siga arriba).")
    return False


def shot(page, name):
    """Screenshot de debug cuando algo falla."""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, f"{name}.png")
        page.screenshot(path=path, full_page=False)
        print(f"   📸 Screenshot: {path}")
    except Exception:
        pass


def scopes(page):
    """La página y todos sus frames (por si la app mete iframes)."""
    result = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            result.append(frame)
    return result


def find_visible(page, selectors, timeout=20000):
    """Devuelve el primer elemento VISIBLE que haga match con alguno de los selectores."""
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        for scope in scopes(page):
            for sel in selectors:
                try:
                    loc = scope.locator(sel)
                    total = loc.count()
                except Exception:
                    continue
                for i in range(min(total, 15)):
                    item = loc.nth(i)
                    try:
                        if item.is_visible():
                            return item
                    except Exception:
                        continue
        time.sleep(0.4)
    return None


def ci(text):
    """XPath case-insensitive para comparar texto."""
    low = text.lower()
    return (
        'translate(normalize-space(.),'
        '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'
        f'="{low}"'
    )


def click_ux_button(page, text, donde="cualquiera", timeout=20000):
    """
    Click en un botón del design system UX.
    OJO: hay DOS botones 'Run' — el de la página (abajo) y el del popover de
    ejecución. Por eso 'donde' elige cuál:
      "pagina" = el que NO está en el popover (abre el modal)
      "modal"  = el de adentro del popover (lanza la ejecución)
    """
    if donde == "modal":
        cond = f"[ancestor::*[{MODAL}]]"
    elif donde == "pagina":
        cond = f"[not(ancestor::*[{MODAL}])]"
    else:
        cond = ""

    span = f'//span[contains(@class,"ux-button__text") and normalize-space(.)="{text}"]{cond}'
    any_span = f'//span[normalize-space(.)="{text}"]{cond}'

    selectors = [
        f"xpath={span}/ancestor::button[1]",
        f"xpath={any_span}/ancestor::button[1]",
        f'xpath=//button[normalize-space(.)="{text}"]{cond}',
        f"xpath={span}",
    ]

    element = find_visible(page, selectors, timeout=timeout)
    if not element:
        print(f"❌ No encontré el botón '{text}' ({donde})")
        shot(page, f"fail_button_{text.replace(' ', '_')}_{donde}")
        return False

    try:
        element.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        element.click(timeout=5000)
    except Exception:
        element.click(timeout=5000, force=True)

    print(f"✅ Click en '{text}' ({donde})")
    page.wait_for_timeout(1200)
    return True


# ---------------- PASO 1: AVISO DE CERTIFICADO ----------------
def bypass_ssl_warning(page):
    """
    Chrome muestra 'Your connection is not private'.
    Click en 'Advanced' (#details-button) y luego en 'Proceed ... (unsafe)' (#proceed-link).
    """
    try:
        details = page.locator("#details-button")
        if details.count() == 0 or not details.first.is_visible():
            print("🔓 No hay aviso de certificado (o ya fue aceptado).")
            return True

        print("🔒 Aviso de certificado detectado → click en 'Advanced'")
        details.first.click(timeout=5000)
        page.wait_for_timeout(600)

        proceed = page.locator("#proceed-link")
        proceed.wait_for(state="visible", timeout=8000)
        print("➡️  Click en 'Proceed ... (unsafe)'")
        proceed.click(timeout=5000)

        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        return True
    except Exception as e:
        print(f"⚠️ Problema con el aviso de certificado: {e}")
        shot(page, "fail_ssl_warning")
        return False


# ---------------- PASO 2: LOGIN ----------------
def login(page, username, password):
    """
    Llena las credenciales DENTRO de la página (nada de coordenadas ni tabs a ciegas).
    OJO: el formulario de Keycloak vive en un IFRAME (192.0.2.4:8081),
    por eso find_visible() recorre todos los frames y no solo la página.
    """
    user_input = find_visible(
        page,
        [
            "#username",
            'input[name="username"]',
            "input.input_login",
        ],
        timeout=30000,
    )

    if not user_input:
        print("🔓 No apareció el login (¿ya hay sesión?).")
        return True

    print("🔑 Llenando credenciales...")
    user_input.click()
    user_input.fill("")
    user_input.type(username, delay=60)

    pass_input = find_visible(
        page,
        [
            "#password",
            'input[name="password"]',
            'input[type="password"]',
        ],
        timeout=5000,
    )

    if pass_input:
        pass_input.click()
        pass_input.fill("")
        pass_input.type(password, delay=60)
    else:
        # Fallback: tab desde username, tal cual el flujo manual
        page.keyboard.press("Tab")
        page.keyboard.type(password, delay=60)

    # Sign In: mejor el botón real (#kc-login) que confiar en Enter.
    sign_in = find_visible(
        page,
        ["#kc-login", 'input[name="login"]', 'input[type="submit"]'],
        timeout=4000,
    )
    if sign_in:
        sign_in.click()
    else:
        page.keyboard.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    page.wait_for_timeout(4000)

    print("✅ Login enviado.")
    return True


# ---------------- PASO 3: CHECKBOX 'SELECCIONAR TODO' ----------------
HEADER_CHECKBOX = "div.ux-table__column._header._checkbox"


def section_names(page):
    """Nombre de la sección de cada header (Prerequisites / Execution / ...)."""
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll(
                    'div.ux-table__column._header._checkbox')).map(h => {
                let el = h, titulo = '(?)';
                while (el && el !== document.body) {
                    let sib = el.previousElementSibling;
                    while (sib) {
                        const t = (sib.innerText || '').trim();
                        if (t && t.length < 60) { titulo = t.split('\\n')[0]; break; }
                        sib = sib.previousElementSibling;
                    }
                    if (titulo !== '(?)') break;
                    el = el.parentElement;
                }
                return titulo;
            })"""
        )
    except Exception:
        return []


def check_one_header(page, header):
    """
    Marca UN checkbox de header. El <input> real es invisible (lo tapa el <i>
    con el SVG), por eso un click normal nunca pegaba: se prueban varias formas
    hasta que is_checked() diga True.
    """
    try:
        header.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    box = header.locator('input[type="checkbox"]').first
    label = header.locator("label.ux-checkbox-field__label").first
    icon = header.locator("i.ux-checkbox-field__icon").first

    def is_checked():
        try:
            return box.is_checked()
        except Exception:
            return False

    if is_checked():
        return True, "ya estaba marcado"

    attempts = [
        ("label (force)", lambda: label.click(force=True, timeout=4000)),
        ("icono svg (force)", lambda: icon.click(force=True, timeout=4000)),
        ("input.check (force)", lambda: box.check(force=True, timeout=4000)),
        ("dispatch_event click", lambda: box.dispatch_event("click")),
        ("header (force)", lambda: header.click(force=True, timeout=4000)),
        ("JS click nativo", lambda: box.evaluate("el => { el.click(); }")),
    ]

    for name, action in attempts:
        try:
            action()
        except Exception:
            continue
        page.wait_for_timeout(700)
        if is_checked():
            return True, name

    return False, "ningún método funcionó"


def select_all_rows(page):
    """
    Marca el checkbox 'seleccionar todo' de CADA sección de la tabla.
    Son 3 (Prerequisites, Execution, Validation), no una sola: antes solo se
    marcaba la primera porque se tomaba el primer elemento encontrado.
    """
    if not find_visible(page, [HEADER_CHECKBOX], timeout=45000):
        print("❌ No encontré ningún header de tabla con checkbox.")
        shot(page, "fail_header_checkbox")
        return False

    total = page.locator(HEADER_CHECKBOX).count()
    nombres = section_names(page)
    print(f"📋 Secciones encontradas: {total} → {nombres}")

    marcados = 0
    for i in range(total):
        nombre = nombres[i] if i < len(nombres) else f"#{i}"

        if SECCIONES is not None and nombre not in SECCIONES:
            print(f"   ⏭️ '{nombre}' omitida (no está en SECCIONES)")
            continue

        header = page.locator(HEADER_CHECKBOX).nth(i)
        ok, como = check_one_header(page, header)
        if ok:
            marcados += 1
            print(f"   ✅ '{nombre}' marcada ({como})")
        else:
            print(f"   ❌ '{nombre}' NO se pudo marcar ({como})")

    if marcados == 0:
        print("❌ No logré marcar ninguna sección.")
        shot(page, "fail_check_all")
        return False

    print(f"✅ {marcados} de {total} secciones marcadas.")
    return True


def uncheck_rows(page, nombres):
    """
    Destilda casos concretos después de haber marcado la sección completa.
    Se usa para los que no deben ejecutarse (p.ej. el que no corre).
    """
    if not nombres:
        return True

    todo_ok = True
    for nombre in nombres:
        seguro = nombre.replace('"', '')
        selectors = [
            f'xpath=//ux-table-row[.//a[normalize-space(.)="{seguro}"]]',
            f'xpath=//ux-table-row[.//a[contains(normalize-space(.),"{seguro}")]]',
            f'xpath=//ux-table-row[contains(normalize-space(.),"{seguro}")]',
        ]

        row = find_visible(page, selectors, timeout=10000)
        if not row:
            print(f"   ⚠️ No encontré el caso '{nombre}' para destildarlo.")
            todo_ok = False
            continue

        box = row.locator('input[type="checkbox"]').first
        label = row.locator("label.ux-checkbox-field__label").first

        try:
            if not box.is_checked():
                print(f"   ☐ '{nombre}' ya estaba destildado.")
                continue
        except Exception:
            pass

        try:
            row.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        attempts = [
            lambda: label.click(force=True, timeout=4000),
            lambda: box.uncheck(force=True, timeout=4000),
            lambda: box.dispatch_event("click"),
            lambda: box.evaluate("el => { el.click(); }"),
        ]

        destildado = False
        for action in attempts:
            try:
                action()
            except Exception:
                continue
            page.wait_for_timeout(700)
            try:
                if not box.is_checked():
                    destildado = True
                    break
            except Exception:
                continue

        if destildado:
            print(f"   ✅ '{nombre}' destildado.")
        else:
            print(f"   ❌ No pude destildar '{nombre}'.")
            shot(page, "fail_destildar")
            todo_ok = False

    return todo_ok


# ---------------- PASO 4: CAMPOS DE REFERENCIA DEL MODAL ----------------
def fill_reference_field(page, label, value):
    """
    Llena 'Environments' o 'TA Tools' del popover de ejecución.

    OJO con dos trampas que hacían que se escribiera en el campo equivocado:
      1. Esas etiquetas NO son <label>, son <span class="ux-label__caption">.
      2. input.ux-reference-field__input hay MUCHOS en la página (el selector
         de Project del header y el 'Depends On' de cada fila de la tabla),
         así que buscar "el primero" o "el segundo" apuntaba a cualquier cosa.
    Por eso se ancla al texto de la etiqueta y se toma su input asociado.
    """
    print(f"🔽 {label} → '{value}'")

    caption = f'//span[contains(@class,"ux-label__caption")][normalize-space(.)="{label}"]'
    ref_input = 'input[contains(@class,"ux-reference-field__input")]'

    selectors = [
        # el input que sigue a la etiqueta, dentro del popover
        f"xpath={caption}/following::{ref_input}[1]",
        # o el input del contenedor al que pertenece la etiqueta
        f"xpath={caption}/ancestor::*[.//{ref_input}][1]//{ref_input}[1]",
        # por si el texto trae espacios o cambia de mayúsculas
        f'xpath=//span[contains(@class,"ux-label__caption")][contains(normalize-space(.),"{label}")]'
        f"/following::{ref_input}[1]",
    ]

    field = find_visible(page, selectors, timeout=10000)

    if not field:
        print(f"❌ No encontré el campo '{label}'.")
        shot(page, f"fail_field_{label.replace(' ', '_')}")
        return False

    field.click()
    page.wait_for_timeout(400)
    try:
        field.fill("")
    except Exception:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")

    field.type(value, delay=90)
    page.wait_for_timeout(1500)

    # La lista de sugerencias es un ux-popover aparte:
    #   ux-popover.ux-field-suggestion-popup__inner
    #     ul.ux-reference-field__suggestions-list
    #       li.ux-reference-field__suggestion-item   <- esto es lo que se clickea
    item = 'li[contains(@class,"ux-reference-field__suggestion-item")]'
    option_selectors = [
        f"xpath=//{item}[{ci(value)}]",
        f'xpath=//*[contains(@class,"ux-reference-field__suggestions-list")]//li[{ci(value)}]',
        f'xpath=//*[contains(@class,"ux-field-suggestion-popup__inner")]//li[{ci(value)}]',
        # último recurso: la primera sugerencia de la lista
        f"xpath=//{item}",
    ]

    option = find_visible(page, option_selectors, timeout=8000)
    if option:
        try:
            option.click(timeout=4000)
        except Exception:
            option.click(timeout=4000, force=True)
        print(f"   ✅ Opción '{value}' seleccionada.")
    else:
        print("   ↪️ No vi la lista de sugerencias, confirmo con Enter.")
        page.keyboard.press("Enter")

    page.wait_for_timeout(1200)

    # Verificar que quedó en ESTE campo y no en el de al lado.
    quedo = field_value(page, label)
    if quedo and value.lower() in quedo.lower():
        print(f"   ✔️ '{label}' quedó en: '{quedo}'")
        return True

    print(f"   ⚠️ '{label}' quedó en '{quedo}', se esperaba '{value}'.")
    shot(page, f"fail_valor_{label.replace(' ', '_')}")
    return False


def field_value(page, label):
    """Lee lo que quedó seleccionado en un campo del popover (chip o texto)."""
    try:
        return page.evaluate(
            """(etiqueta) => {
                let sp = null;
                document.querySelectorAll('span.ux-label__caption').forEach(s => {
                    if (s.innerText.trim() === etiqueta) sp = s;
                });
                if (!sp) return null;
                // subir al contenedor del campo y leer su texto (incluye los chips)
                let node = sp;
                for (let k = 0; k < 8 && node; k++) {
                    node = node.parentElement;
                    if (node && node.querySelector('input.ux-reference-field__input')) {
                        const inp = node.querySelector('input.ux-reference-field__input');
                        const txt = (node.innerText || '').replace(etiqueta, '').trim();
                        return txt || inp.value || '';
                    }
                }
                return null;
            }""",
            label,
        )
    except Exception:
        return None


# ---------------- CHROME ----------------
def launch_chrome():
    """
    Lanza Chrome REAL (no Chromium) con puerto de depuración y apuntando al
    túnel SOCKS5 de PuTTY. Queda abierto al terminar el script, así que
    Rerun_Sanity.py puede reusarlo por el puerto 9222.
    """
    if port_open(DEBUG_PORT):
        print(f"♻️ Ya hay un Chrome escuchando en {DEBUG_PORT}, lo reuso.")
        return

    if not os.path.exists(CHROME_PATH):
        raise FileNotFoundError(f"No encontré Chrome en {CHROME_PATH}")

    print(f"🌐 Abriendo Chrome (proxy {SOCKS_PROXY})...")
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    subprocess.Popen(
        [
            CHROME_PATH,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={CHROME_PROFILE}",
            # Esto reemplaza a SwitchyOmega: sin el túnel el sitio no responde.
            f"--proxy-server={SOCKS_PROXY}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            URL,
        ]
    )

    for _ in range(40):
        if port_open(DEBUG_PORT):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Chrome no abrió el puerto de depuración.")

    time.sleep(3)


def run_sanity(web_user, web_password):
    # Sin el túnel SOCKS5 arriba, Chrome da ERR_CONNECTION_TIMED_OUT.
    print(f"🔎 Esperando el túnel SOCKS5 en 127.0.0.1:{TUNNEL_PORT}...")
    if not wait_for_port(TUNNEL_PORT, timeout=60):
        print(f"❌ El túnel {TUNNEL_PORT} no levantó. Revisa el PuTTY de ATP_SIT_Step2.")
        return
    print("✅ Túnel arriba.")

    launch_chrome()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]

        page = None
        for candidate in context.pages:
            if "192.0.2.4" in candidate.url or candidate.url.startswith("chrome-error"):
                page = candidate
                break
        if page is None:
            page = context.pages[0] if context.pages else context.new_page()
            if "192.0.2.4" not in page.url:
                try:
                    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    # El cert es invalido: goto() truena pero Chrome SI muestra
                    # la interstitial, que bypass_ssl_warning() resuelve abajo.
                    print(f"⚠️ goto() reportó: {type(e).__name__} (sigo, es el cert)")

        page.bring_to_front()
        page.set_default_timeout(20000)
        page.wait_for_timeout(2000)

        bypass_ssl_warning(page)
        login(page, web_user, web_password)

        if not check_tunnel("login"):
            return

        if not select_all_rows(page):
            print("🛑 Me detengo: sin selección no tiene sentido ejecutar.")
            return

        # Quitar los casos que no deben correr.
        print("🚫 Destildando casos excluidos...")
        uncheck_rows(page, FILAS_A_EXCLUIR)

        # El botón de la página se llama "Run" (no "Run All") y abre el popover.
        if not click_ux_button(page, "Run", donde="pagina", timeout=30000):
            return

        page.wait_for_timeout(2500)

        ok_env = fill_reference_field(page, "Environments", ENVIRONMENT_VALUE)
        ok_tools = fill_reference_field(page, "TA Tools", TA_TOOLS_VALUE)

        if not (ok_env and ok_tools):
            print("🛑 Me detengo: los campos del popover no quedaron bien.")
            print("   (Son obligatorios; el botón Run seguiría deshabilitado.)")
            return

        # Este "Run" es el de adentro del popover: ESTE sí lanza la ejecución.
        if not click_ux_button(page, "Run", donde="modal", timeout=20000):
            return

        page.wait_for_timeout(3000)
        print("🚀 Run lanzado.")


# ---------------- MAIN ----------------
if __name__ == "__main__":
    # Antes de cambiar el teclado: si es la primera vez, aquí se piden.
    web_user, web_password = get_credentials(ATP_SIT_WEB_ACCOUNT, default_username="admin")

    switch_keyboard_to_english()

    sit1_user, sit1_pass = get_credentials("putty_sit1", default_username="sit1_user")
    open_putty(
        session="SIT1",
        username=sit1_user,
        password=sit1_pass,
        tunnel_port=7060,
    )

    # Esta sesión es la que abre el SOCKS5 en 8082 que usa Chrome.
    atp_user, atp_pass = get_credentials("putty_atp_sit", default_username="atp_sit_user")
    open_putty(
        session="ATP_SIT_Step2",
        username=atp_user,
        password=atp_pass,
        tunnel_port=TUNNEL_PORT,
    )

    try:
        run_sanity(web_user, web_password)
    finally:
        switch_keyboard_to_spanish()

    print("✅ TODO COMPLETED")
