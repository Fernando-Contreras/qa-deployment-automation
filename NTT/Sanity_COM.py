"""
Sanity_COM.py
=============

Lanza el sanity de COM en ATP Catalogue PROD.

Flujo (el mismo que haces a mano):
  1. Abre la URL del scope (login SAML solo la primera vez).
  2. Marca los checkbox de "seleccionar todo" de Prerequisites y Execution.
  3. Click en Run (el de la barra de abajo) -> se abre el popup.
  4. En el popup: Environments = el env, TA Tools = NTT Main Group.
  5. Click en Run del popup y se queda con el link de la ejecucion.

Los environments salen SOLOS de Daily_Envs.txt: agarra los COM que estan en
PASSED y les dispara sanity a todos, EN PARALELO (una pestaña por env).

Intenta correr HEADLESS. Si el SSO no se puede completar sin ventana (pasa
casi siempre, porque las cookies de Keycloak son de sesion y se borran al
cerrar Chrome), abre una ventana SOLO, entra y sigue: no tienes que hacer
nada ni correr otro comando.

Truco: con --dejar-abierto el Chrome se queda vivo, la sesion se conserva y
las corridas siguientes SI van headless puras y mas rapidas.

Uso:
  python Sanity_COM.py               -> los COM en PASSED de Daily_Envs.txt
  python Sanity_COM.py 25            -> forzar com25 (ignora Daily_Envs)
  python Sanity_COM.py 3 30 2 14     -> forzar varios
  python Sanity_COM.py --dry 25      -> llena todo pero NO dispara. Para probar.
  python Sanity_COM.py --visible     -> con ventana (para entrar con SAML)
  python Sanity_COM.py --jobs 3      -> cuantos en paralelo (default 4)
  python Sanity_COM.py --secuencial  -> uno tras otro, en una sola pestaña
  python Sanity_COM.py --dejar-abierto -> no cerrar Chrome al terminar

Usa SU PROPIO Chrome (puerto 9344 y perfil aparte) y lo cierra al terminar.
No se cuelga de tu Chrome de diario a proposito: ese trae la sesion de
deployment, no la del sanity, y acabarias viendo la pantalla de login.

Por que asyncio y no hilos: cada hilo necesitaria su propia conexion
connect_over_cdp (la API sync de Playwright no se comparte entre hilos), y
varias conexiones al mismo Chrome se saturan entre si -- probado: da
"connect_over_cdp: Timeout 180000ms exceeded". Con asyncio es UNA sola
conexion y N pestañas trabajando a la vez.

NO tienes que dejar el navegador abierto media hora: el sanity corre en el
servidor de ATP. El script dispara, imprime los links y se sale.

OJO: esto NO es como TS_Sanity.py. Aqui la URL es publica (prod), asi que no
hay PuTTY ni tunel SOCKS. Lo unico que necesitas es la VPN/red normal.
"""

import asyncio
import contextvars
import os
import re
import socket
import subprocess
import sys
import time

from playwright.async_api import async_playwright

import ntt_config

# ---------------- CONFIG ----------------
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Puerto EXCLUSIVO de este script. No uses 9222 (TS_Sanity) ni 9333: ese lo
# tiene tu Chrome de diario, y si nos colgamos de ahi acabamos usando la
# sesion de otra cosa (deployment) en vez de la del sanity. Justo eso pasaba:
# el script "reusaba" tu navegador y te salia la pantalla de login.
DEBUG_PORT = 9344

# Perfil dedicado y PERSISTENTE: aqui vive tu sesion SAML. No lo borres o
# tendras que volver a loguearte. No toca tu Chrome personal.
# Perfil nuevo a proposito: el anterior (com_sanity_chrome) quedo tomado por
# procesos de Chrome que Windows no deja cerrar, y con el perfil bloqueado
# Chrome ni siquiera arranca. Si algun dia le pasa lo mismo a este, se puede
# usar otro al vuelo con --perfil <nombre>.
CHROME_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "sanity_com_chrome")

URL = (
    "https://atp.example.com"
    "/project/00000000-0000-4000-8000-000000000001"
    "/plan/00000000-0000-4000-8000-000000000002"
    "/scopes/00000000-0000-4000-8000-000000000003"
)

TA_TOOLS_VALUE = "NTT Main Group"

# Cuando hay nttcomNN_qa y nttcomNN_dev, nos quedamos con _qa.
SUFIJO_PREFERIDO = "_qa"

# Cuantos environments a la vez. Mas de 4-5 y cada pestaña pelea por cargar
# su propio Angular, y deja de ganarse tiempo.
MAX_JOBS = 4

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(BASE_DIR, "com_sanity_shots")

# De aqui salen los envs: los COM que quedaron en PASSED hoy.
DAILY_ENVS_PATH = str(ntt_config.DAILY_ENVS_PATH)

# Mismo formato que usan Results_Sanity.py y Track_Deployments.py.
STATUS_HEADER_RE = re.compile(
    r"^\s*(?:\S+\s+)?\*\*(PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*:\s*$"
)
# Solo cuentan los links markdown [**COM25QA**](...nttcom25_qa...).
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
NTTCOM_RE = re.compile(r"nttcom(\d+)_qa", re.IGNORECASE)
COM_LABEL_RE = re.compile(r"COM(\d+)QA", re.IGNORECASE)


# ---------------- SELECTORES ----------------
# Sacados del DOM real, no adivinados. El popup de Run NO es un cdk-overlay-pane
# (por eso al principio no lo encontrabamos): es un ux-popover.
MODAL = ".atp-execute-scope__inner"

FIELD_ENV = ".atp-execute-scope__field._environments"
FIELD_TA = ".atp-execute-scope__field._ta-tools"
CAMPO_ENV = f"{FIELD_ENV} input.ux-reference-field__input"

# Hay dos botones "Run" y son distintos:
BOTON_RUN_PAGINA = "ux-button.atp-toolbar__control"   # el de la barra de abajo
BOTON_RUN_POPUP = "ux-button.atp-execute-scope__button"  # el de dentro del popup

ITEM_SUGERENCIA = "li.ux-reference-field__suggestion-item"
POPOVER_SUGERENCIAS = ".ux-reference-field__popover"

HEADER_CHECKBOX = "div.ux-table__column._header._checkbox"


# ---------------- LOG ----------------
# ContextVar y no threading.local: con asyncio cada task hereda su copia del
# contexto, asi cada environment etiqueta sus propias lineas.
TAG = contextvars.ContextVar("tag", default="")

# El Chrome que lanzó ESTE script, para poder cerrarlo al final.
CHROME_PROC = None


def log(msg):
    tag = TAG.get()
    print(f"{tag} {msg}" if tag else msg, flush=True)


# ---------------- HELPERS ----------------
def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


async def hasta(condicion, timeout=10000, intervalo=0.1):
    """
    Espera activa: en cuanto se cumple, sigue.
    Sustituye a los sleep fijos, que era donde se iba la mitad del tiempo.
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            if await condicion():
                return True
        except Exception:
            pass
        await asyncio.sleep(intervalo)
    return False


async def shot(page, name):
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=False)
        log(f"   📸 Screenshot: {path}")
    except Exception as e:
        log(f"   ⚠️ No pude tomar el screenshot: {type(e).__name__}")


def _scopes(root):
    """
    La página y todos sus frames. El login de Keycloak vive en un iframe,
    y si solo miramos la página principal no lo vemos.
    """
    try:
        frames = root.frames
        principal = root.main_frame
    except AttributeError:
        return [root]
    return [root] + [f for f in frames if f != principal]


async def visibles(root, selector, limite=30, ordenar=False):
    """
    Elementos visibles que hacen match.
    Con ordenar=True los devuelve como se ven en pantalla (arriba->abajo);
    cuesta una llamada extra por elemento, asi que solo cuando hace falta.
    """
    encontrados = []

    for scope in _scopes(root):
        try:
            items = await scope.locator(selector).all()
        except Exception:
            continue

        for item in items[:limite]:
            try:
                if await item.is_visible():
                    encontrados.append(item)
            except Exception:
                continue

    if not ordenar or len(encontrados) < 2:
        return encontrados

    con_pos = []
    for item in encontrados:
        try:
            box = await item.bounding_box()
        except Exception:
            box = None
        if box:
            con_pos.append((box["y"], box["x"], item))

    con_pos.sort(key=lambda t: (t[0], t[1]))
    return [item for _, _, item in con_pos]


async def esperar(page, selector, timeout=30000):
    """Espera a que algo visible haga match. Devuelve el elemento o None."""
    hallado = []

    async def buscar():
        items = await visibles(page, selector, limite=5)
        if items:
            hallado.append(items[0])
            return True
        return False

    await hasta(buscar, timeout=timeout)
    return hallado[0] if hallado else None


# ---------------- LOGIN ----------------
async def hay_login_visible(page):
    """¿Te está pidiendo credenciales? Entonces no toco nada."""
    for sel in ('input[type="password"]', 'input[name="loginfmt"]', "#username"):
        if await visibles(page, sel, limite=2):
            return True
    return False


async def click_saml(page, timeout=60000):
    """
    El botón es <span>SAML Authentication</span> y vive DENTRO del iframe de
    Keycloak (atp-keycloak.example.com), que tarda unos segundos en aparecer.

    Antes decidíamos "ya hay sesión" por ausencia de login, y como el iframe
    todavía no existía se salía en falso; luego esperaba una tabla que nunca
    llegaba y había que darle click a mano. Ahora esperamos a ver UNA de las
    dos cosas de verdad: la tabla (hay sesión) o el botón SAML (hay que entrar).
    """
    log("🔎 Buscando 'SAML Authentication'...")
    saml = None
    deadline = time.time() + timeout / 1000.0

    while time.time() < deadline:
        if await visibles(page, HEADER_CHECKBOX, limite=2):
            log("🔓 Ya hay sesión (la tabla está cargada). Sigo.")
            return

        for sel in (
            'span:text-is("SAML Authentication")',
            'a:text-is("SAML Authentication")',
            'button:text-is("SAML Authentication")',
            '*:text-is("SAML Authentication")',
        ):
            items = await visibles(page, sel, limite=3)
            if items:
                saml = items[0]
                break

        if saml:
            break
        await asyncio.sleep(0.3)

    if not saml:
        log("⚠️ No vi ni la tabla ni el botón SAML. Sigo y ya veremos.")
        await shot(page, "fail_saml")
        return

    log("🔑 Click en 'SAML Authentication'...")
    try:
        await saml.click(timeout=5000)
    except Exception:
        await saml.click(timeout=5000, force=True)

    # 'load' y no 'networkidle': Angular deja polling abierto y networkidle
    # casi nunca se cumple, se comía 60s de nada.
    try:
        await page.wait_for_load_state("load", timeout=30000)
    except Exception:
        pass


async def wait_for_table(page, timeout=180000, headless=False):
    """Espera la tabla de casos. Si el SSO te saca del scope, regresa solo."""
    inicio = time.time()
    ultimo_aviso = 0.0
    ya_renavegue = False
    ya_recargue = False

    while time.time() - inicio < timeout / 1000.0:
        if await visibles(page, HEADER_CHECKBOX, limite=5):
            return True

        transcurrido = time.time() - inicio
        if time.time() - ultimo_aviso >= 15:
            ultimo_aviso = time.time()
            try:
                actual = page.url
            except Exception:
                actual = "???"
            log(f"   ⏳ {int(transcurrido)}s — estoy en: {actual[:90]}")

            if await hay_login_visible(page):
                if headless:
                    # Sin ventana no se pueden teclear credenciales. No es un
                    # fallo: quien llama vuelve a abrir Chrome CON ventana.
                    return "login"
                log("   ⌨️ Veo una pantalla de login. Termínalo y yo sigo solo.")
            elif not ya_renavegue and transcurrido > 20 and "/scopes/" not in actual:
                # Tras el SSO la app te suelta en la raíz, NO en el scope.
                ya_renavegue = True
                log("   ↩️ Estoy fuera del scope, vuelvo a la URL buena.")
                try:
                    await page.goto(URL, wait_until="load", timeout=60000)
                except Exception as e:
                    log(f"   ⚠️ goto(): {type(e).__name__}")
            elif not ya_recargue and transcurrido > 75:
                ya_recargue = True
                log("   🔄 Sigue sin cargar, recargo una vez.")
                try:
                    await page.reload(wait_until="load", timeout=60000)
                except Exception as e:
                    log(f"   ⚠️ reload(): {type(e).__name__}")

        await asyncio.sleep(0.5)

    log("❌ Nunca apareció la tabla de casos.")
    await shot(page, "fail_no_table")
    return False


# ---------------- CHECKBOXES ----------------
async def marcar_checkbox(page, header, etiqueta):
    """
    El <input> real es invisible (lo tapa el <i> con el SVG), por eso un click
    normal no pega: probamos varias formas hasta que is_checked() diga True.
    """
    box = header.locator('input[type="checkbox"]').first
    label = header.locator("label.ux-checkbox-field__label").first
    icon = header.locator("i.ux-checkbox-field__icon").first

    async def marcado():
        try:
            return await box.is_checked()
        except Exception:
            return False

    if await marcado():
        return True

    intentos = [
        ("label (force)", lambda: label.click(force=True, timeout=4000)),
        ("icono svg (force)", lambda: icon.click(force=True, timeout=4000)),
        ("input.check (force)", lambda: box.check(force=True, timeout=4000)),
        ("dispatch_event", lambda: box.dispatch_event("click")),
        ("JS click nativo", lambda: box.evaluate("el => el.click()")),
    ]

    for nombre, accion in intentos:
        try:
            await accion()
        except Exception:
            continue
        if await hasta(marcado, timeout=1500):
            return True

    log(f"❌ No logré marcar {etiqueta}.")
    await shot(page, f"fail_check_{etiqueta.replace(' ', '_')}")
    return False


async def seleccionar_tablas(page):
    """
    Los 3 grupos son Prerequisites, Execution y Validation.
    Queremos los dos primeros; Validation (0 items) se queda como está.
    """
    headers = await visibles(page, HEADER_CHECKBOX, limite=10, ordenar=True)
    if len(headers) < 2:
        log(f"❌ Esperaba al menos 2 grupos y vi {len(headers)}.")
        await shot(page, "fail_pocos_checkboxes")
        return False

    ok = True
    for i in (0, 1):
        if not await marcar_checkbox(page, headers[i], f"grupo #{i + 1}"):
            ok = False
    if ok:
        log("☑️ Prerequisites y Execution marcados.")
    return ok


# ---------------- CAMPOS DEL POPUP ----------------
async def chips_actuales(page, field_sel):
    """Los valores ya seleccionados en el campo (las 'pastillas' azules)."""
    try:
        return await page.evaluate(
            """(sel) => {
                const campo = document.querySelector(sel);
                if (!campo) return [];
                return [...campo.querySelectorAll('.ux-reference-field__value-caption')]
                    .map(e => (e.textContent || '').trim())
                    .filter(Boolean);
            }""",
            field_sel,
        )
    except Exception:
        return []


async def enfocar_campo(page, field_sel):
    """
    Pone el cursor en el input SIN hacer click.

    Importante: cuando el campo ya trae un valor, su 'pastilla' (y la x de
    quitarla) se montan encima del input y Playwright no puede clickearlo
    ("subtree intercepts pointer events"). Con focus() por JS nos saltamos
    todo ese problema.
    """
    try:
        return await page.evaluate(
            """(sel) => {
                const campo = document.querySelector(sel);
                const i = campo && campo.querySelector('input.ux-reference-field__input');
                if (!i) return false;
                i.focus();
                return document.activeElement === i;
            }""",
            field_sel,
        )
    except Exception:
        return False


async def limpiar_chips(page, field_sel):
    """
    Quita los valores que quedaron del run anterior.

    Estos campos son _multiple: si no borramos el chip de com12, al escribir
    com25 acabaríamos corriendo el sanity con LOS DOS.

    Se hace con Backspace, no clickeando la 'x' del chip: esa x solo aparece
    al pasar el mouse, un click por JS no la activa, y después del hover ella
    misma tapa el input. Backspace sobre el campo enfocado la quita al toque.
    """
    if not await chips_actuales(page, field_sel):
        return True

    if not await enfocar_campo(page, field_sel):
        log("   ⚠️ No pude enfocar el campo para limpiarlo.")
        return False

    for _ in range(12):
        antes = await chips_actuales(page, field_sel)
        if not antes:
            return True
        await page.keyboard.press("Backspace")
        await hasta(
            lambda: menos_chips(page, field_sel, len(antes)), timeout=1500
        )

    quedan = await chips_actuales(page, field_sel)
    if quedan:
        log(f"   ⚠️ Quedó puesto: {quedan}")
        return False
    return True


async def menos_chips(page, field_sel, cuantos):
    return len(await chips_actuales(page, field_sel)) < cuantos


async def sugerencias(page):
    """
    Las opciones del desplegable, como [(texto, elemento)].
    Viven en su propio popover, aparte del modal.
    """
    items = await visibles(page, ITEM_SUGERENCIA, limite=80)
    if not items:
        items = await visibles(page, f"{POPOVER_SUGERENCIAS} li", limite=80)

    out = []
    for item in items:
        try:
            texto = (await item.inner_text() or "").strip()
        except Exception:
            continue
        if texto:
            out.append((texto, item))
    return out


async def esperar_sugerencias(page, predicado, timeout=12000):
    """Espera a que aparezca al menos una opción que cumpla el predicado."""
    encontradas = []

    async def buscar():
        del encontradas[:]
        for texto, elemento in await sugerencias(page):
            if predicado(texto):
                encontradas.append((texto, elemento))
        return bool(encontradas)

    await hasta(buscar, timeout=timeout)
    return list(encontradas)


async def escribir_en_campo(page, field_sel, texto):
    """Enfoca el campo, lo limpia y escribe."""
    if not await enfocar_campo(page, field_sel):
        return False
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await page.keyboard.type(str(texto), delay=25)
    return True


async def elegir_opcion(page, elemento):
    try:
        await elemento.click(timeout=4000)
    except Exception:
        await elemento.click(timeout=4000, force=True)


async def pick_environment(page, numero):
    """
    Escribe el número y elige la opción que sea com<numero>.
    El (?!\\d) evita que 14 agarre a com140. Si hay _qa y _dev, gana _qa.
    """
    if await chips_actuales(page, FIELD_ENV):
        if not await limpiar_chips(page, FIELD_ENV):
            await shot(page, f"fail_env_{numero}_sucio")
            return False

    if not await escribir_en_campo(page, FIELD_ENV, numero):
        log("❌ No pude escribir en Environments.")
        await shot(page, f"fail_env_{numero}_sin_campo")
        return False

    patron = re.compile(rf"com0*{numero}(?!\d)", re.IGNORECASE)
    candidatas = await esperar_sugerencias(page, lambda t: bool(patron.search(t)))

    if not candidatas:
        vistas = [t for t, _ in await sugerencias(page)][:12]
        log(f"❌ Ninguna opción es 'com{numero}'. Vi: {vistas}")
        await shot(page, f"fail_env_{numero}_sin_match")
        return False

    # Si salen _qa y _dev, preferimos _qa.
    preferidas = [c for c in candidatas if c[0].lower().endswith(SUFIJO_PREFERIDO)]
    texto, elemento = (preferidas or candidatas)[0]

    await elegir_opcion(page, elemento)

    # Verificación: que quede EXACTAMENTE ese env y ningún otro. Sin esto
    # podríamos disparar el sanity en el environment equivocado.
    async def solo_ese():
        return await chips_actuales(page, FIELD_ENV) == [texto]

    if not await hasta(solo_ese, timeout=8000):
        log(f"❌ Esperaba solo [{texto}] y hay {await chips_actuales(page, FIELD_ENV)}.")
        await shot(page, f"fail_env_{numero}_chip_raro")
        return False

    log(f"🌎 Environment: {texto}")
    return True


async def pick_ta_tools(page, valor=TA_TOOLS_VALUE):
    """
    Match EXACTO: 'NTT Main Group' no debe agarrar 'NTT Main Group CAMUI'
    ni 'NTT Main Group Custom Actions', que están justo abajo en la lista.
    """
    puestos = await chips_actuales(page, FIELD_TA)
    if [c.lower() for c in puestos] == [valor.lower()]:
        log(f"🧰 TA Tools ya traía '{valor}'.")
        return True

    if puestos and not await limpiar_chips(page, FIELD_TA):
        await shot(page, "fail_ta_tools_sucio")
        return False

    if not await escribir_en_campo(page, FIELD_TA, valor):
        log("❌ No pude escribir en TA Tools.")
        await shot(page, "fail_ta_tools_sin_campo")
        return False

    exactas = await esperar_sugerencias(
        page, lambda t: t.strip().lower() == valor.lower()
    )
    if not exactas:
        vistas = [t for t, _ in await sugerencias(page)][:12]
        log(f"❌ No encontré '{valor}' exacto. Vi: {vistas}")
        await shot(page, "fail_ta_tools")
        return False

    texto, elemento = exactas[0]
    await elegir_opcion(page, elemento)

    async def puesto():
        return await chips_actuales(page, FIELD_TA) == [texto]

    if not await hasta(puesto, timeout=8000):
        log(f"❌ TA Tools quedó como {await chips_actuales(page, FIELD_TA)}.")
        await shot(page, "fail_ta_tools_chip")
        return False

    log(f"🧰 TA Tools: {texto}")
    return True


# ---------------- POPUP ----------------
async def abrir_popup(page):
    """Click en el Run de la barra de abajo. Ojo: hay otro Run dentro del popup."""
    boton = await esperar(page, f"{BOTON_RUN_PAGINA} button", timeout=20000)
    if not boton:
        log("❌ No encontré el botón Run de la pantalla.")
        await shot(page, "fail_boton_run_pagina")
        return False

    try:
        await boton.click(timeout=5000)
    except Exception:
        await boton.click(timeout=5000, force=True)

    async def popup_listo():
        return bool(await visibles(page, CAMPO_ENV, limite=2))

    if not await hasta(popup_listo, timeout=20000):
        log("❌ No se abrió el popup de Run.")
        await shot(page, "fail_modal")
        return False

    return True


async def cerrar_desplegables(page):
    """
    Cierra la lista de sugerencias y suelta el foco del campo.

    Hace falta ANTES de darle al Run: al elegir TA Tools queda el desplegable
    abierto encima, y entonces el click se gasta en cerrarlo en vez de lanzar
    el run. El popup se quedaba tal cual y parecía que el botón no servía.

    Se usa blur() y no Escape: Escape cerraría el popup entero.
    """
    try:
        await page.evaluate(
            "() => { if (document.activeElement) document.activeElement.blur(); }"
        )
    except Exception:
        pass

    async def sin_sugerencias():
        return not await visibles(page, ITEM_SUGERENCIA, limite=3)

    await hasta(sin_sugerencias, timeout=5000)


async def run_del_popup(page, timeout=20000):
    """
    Le da al Run del popup y devuelve el link de la ejecución.

    El botón nace DESHABILITADO (clase _disabled) y solo se habilita cuando
    Environments y TA Tools están llenos, así que hay que esperarlo.

    El link se captura con expect_popup, que amarra la pestaña nueva A ESTA
    página. Comparar "pestañas de antes vs de ahora" se confundiría entre
    environments cuando van en paralelo.
    """
    contenedor = page.locator(BOTON_RUN_POPUP).first

    async def habilitado():
        clases = await contenedor.get_attribute("class") or ""
        return bool(clases) and "_disabled" not in clases

    if not await hasta(habilitado, timeout=timeout):
        log("❌ El botón Run del popup sigue deshabilitado (¿faltó algún campo?).")
        await shot(page, "fail_run_deshabilitado")
        return None

    boton = contenedor.locator("button").first
    nueva = None

    # Dos intentos: el primer click a veces se lo come el desplegable.
    for intento in (1, 2):
        await cerrar_desplegables(page)

        # expect_popup registra el listener de forma síncrona, antes de
        # ejecutar el cuerpo. Con ensure_future la corrutina no arranca hasta
        # el siguiente await y el popup puede dispararse sin que nadie lo oiga.
        click_ok = False
        try:
            async with page.expect_popup(timeout=25000) as info:
                try:
                    await boton.click(timeout=8000)
                    click_ok = True
                except Exception:
                    await boton.click(timeout=8000, force=True)
                    click_ok = True
            nueva = await info.value
        except Exception as e:
            if not click_ok:
                log(f"❌ No pude darle click al Run del popup: {type(e).__name__}")
                await shot(page, "fail_click_run")
                return None

        if nueva is not None:
            break

        # Si el popup sigue abierto es que el click no prendió: reintentamos.
        if not await visibles(page, MODAL, limite=2):
            break
        if intento == 1:
            log("   ↻ El click no prendió (el popup sigue abierto), reintento.")

    if nueva is None:
        log("❌ No se abrió la pestaña de la ejecución: no puedo confirmar el run.")
        await shot(page, "fail_sin_popup")
        return None

    # CUIDADO AQUÍ. La pestaña abre en about:blank y tarda un rato en navegar
    # a /ram/execution-request/<id>. Cerrarla antes de eso ABORTA el run: así
    # se cayeron 10 de 10 y encima se reportaban como lanzados.
    # Solo cerramos cuando ya tiene la URL de la ejecución, porque entonces el
    # run ya existe del lado del servidor.
    async def ya_cargo():
        return "execution-request" in nueva.url

    if not await hasta(ya_cargo, timeout=45000):
        log(f"⚠️ La ejecución no terminó de abrir (url: '{nueva.url}').")
        log("   Dejo esa pestaña ABIERTA: cerrarla podría cancelar el run.")
        return None

    link = nueva.url
    try:
        await nueva.close()
    except Exception:
        pass

    return link


# ---------------- UN RUN ----------------
async def lanzar_run(page, numero, disparar=True):
    """Todo el ciclo para un environment. Nunca lanza: devuelve None si falla."""
    try:
        # Por si quedó un popup abierto: tapa los checkbox.
        if await visibles(page, MODAL, limite=2):
            await page.keyboard.press("Escape")

            async def cerrado():
                return not await visibles(page, MODAL, limite=2)

            await hasta(cerrado, timeout=3000)

        if not await seleccionar_tablas(page):
            return None
        if not await abrir_popup(page):
            return None
        if not await pick_environment(page, numero):
            return None
        if not await pick_ta_tools(page):
            return None

        if not disparar:
            log("🧪 MODO PRUEBA: campos llenos, NO le doy Run.")
            await shot(page, f"dry_run_popup_{numero}")
            return "dry"

        link = await run_del_popup(page)
        if link:
            log(f"🚀 Lanzado: {link}")
        else:
            log("❌ No pude confirmar que se haya lanzado.")
        return link

    except Exception as e:
        # Un env que truena no debe tumbar a los demás.
        log(f"💥 Se cayó: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        try:
            await shot(page, f"fail_env_{numero}_excepcion")
        except Exception:
            pass
        return None


# ---------------- PARALELO ----------------
async def pestana_trabajadora(context, cola, resultados, tiempos, disparar,
                             headless, retraso):
    """
    Una pestaña que va sacando environments de la cola hasta vaciarla.

    La clave está en REUSAR la pestaña: cargar la SPA de Angular cuesta ~7s y
    disparar un environment solo ~4s. Con una pestaña por environment se paga
    esa carga cada vez; así se paga una sola vez por pestaña y de ahí en
    adelante cada env sale a 4s.
    """
    # Escalonado: si todas cargan Angular al mismo instante, se estorban.
    await asyncio.sleep(retraso)

    page = None
    try:
        page = await context.new_page()
        page.set_default_timeout(20000)

        try:
            await page.goto(URL, wait_until="commit", timeout=60000)
        except Exception as e:
            log(f"⚠️ goto(): {type(e).__name__}")

        if not await wait_for_table(page, timeout=150000, headless=headless):
            log("🛑 Esta pestaña no cargó; sus environments se los llevan las otras.")
            return

        while True:
            try:
                numero = cola.get_nowait()
            except asyncio.QueueEmpty:
                return

            TAG.set(f"[com{numero}]")
            inicio = time.time()
            try:
                resultados[numero] = await lanzar_run(page, numero, disparar=disparar)
            except Exception as e:
                log(f"💥 {type(e).__name__}: {str(e).splitlines()[0][:120]}")
                resultados[numero] = None
            tiempos[numero] = time.time() - inicio

    except Exception as e:
        log(f"💥 {type(e).__name__}: {str(e).splitlines()[0][:120]}")

    finally:
        # Cerrarla libera memoria y evita que quede huérfana para la próxima.
        try:
            if page:
                await page.close()
        except Exception:
            pass


async def correr_paralelo(context, envs, disparar, headless, jobs):
    jobs = max(1, min(jobs, len(envs)))
    log(f"\n⚡ {len(envs)} environments en {jobs} pestaña(s) a la vez.\n")

    cola = asyncio.Queue()
    for numero in envs:
        cola.put_nowait(numero)

    # Arrancan en None: si una pestaña muere sin tomar su env, sale como fallo
    # en vez de desaparecer del reporte.
    resultados = {n: None for n in envs}
    tiempos = {}

    await asyncio.gather(
        *[
            pestana_trabajadora(
                context, cola, resultados, tiempos, disparar, headless,
                retraso=i * 0.8,
            )
            for i in range(jobs)
        ],
        return_exceptions=True,
    )

    TAG.set("")
    return resultados, tiempos


async def correr_secuencial(page, envs, disparar):
    resultados, tiempos = {}, {}
    for numero in envs:
        TAG.set(f"[com{numero}]")
        inicio = time.time()
        resultados[numero] = await lanzar_run(page, numero, disparar=disparar)
        tiempos[numero] = time.time() - inicio
    TAG.set("")
    return resultados, tiempos


# ---------------- CHROME ----------------
def liberar_perfil():
    """
    Cierra cualquier Chrome que tenga tomado NUESTRO perfil.

    Chrome no deja abrir dos instancias con el mismo --user-data-dir: la
    segunda se cierra sola y nunca abre el puerto ("Chrome no abrió el puerto
    de depuración"). Pasa cuando quedó uno vivo de una corrida anterior o de
    un --visible.

    Solo toca procesos cuya línea de comandos trae este perfil, que es
    exclusivo del script: tu Chrome de diario ni se entera.
    """
    buscar = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{os.path.basename(CHROME_PROFILE)}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )

    def pids():
        try:
            salida = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", buscar],
                capture_output=True, text=True, timeout=25,
            ).stdout
            return [int(x) for x in salida.split() if x.strip().isdigit()]
        except Exception:
            return []

    vivos = pids()
    if not vivos:
        return

    log(f"🧹 Cierro {len(vivos)} proceso(s) de Chrome que tenían tomado el perfil.")

    # Primero por las buenas (deja que guarde cookies y sesión), luego a la mala.
    for forzar in (False, True):
        restantes = pids()
        if not restantes:
            break
        for pid in restantes:
            cmd = ["taskkill", "/PID", str(pid), "/T"]
            if forzar:
                cmd.append("/F")
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
            except Exception:
                pass
        time.sleep(2.5)

    # Con el perfil ya libre, el lockfile que quedó solo estorba.
    if not pids():
        for basura in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(CHROME_PROFILE, basura))
            except Exception:
                pass


def launch_chrome(headless=True):
    """
    Si ya hay un Chrome con el puerto de depuración abierto, lo reusa y solo
    abre pestañas ahí (y entonces manda el modo de ESE Chrome). Si no, abre
    uno con perfil dedicado.

    Headless funciona igual: la app carga y el sanity se dispara. Lo único que
    NO se puede hacer sin ventana es teclear credenciales, así que la primera
    vez (o cuando caduque la sesión) hay que usar --visible una sola vez.

    NO uses aquí tu Chrome de diario. Este script necesita la sesión de ATP
    del sanity, y tu navegador normal trae la de deployment u otra cosa: si se
    cuelga de ahí, acaba pidiéndote login y no dispara nada. Por eso tiene
    puerto (9344) y perfil propios, separados de todo lo demás.
    """
    if port_open(DEBUG_PORT):
        log(f"♻️ Ya hay un Chrome en el puerto {DEBUG_PORT}: lo reuso.")
        return True

    if not os.path.exists(CHROME_PATH):
        raise FileNotFoundError(f"No encontré Chrome en {CHROME_PATH}")

    # Si quedó uno vivo con este perfil, el nuevo no arrancaría.
    liberar_perfil()

    args = [
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if headless:
        log("🌐 Abriendo Chrome en headless (sin ventana)...")
        # El tamaño importa: en una ventana chica el popup se encima y los
        # elementos dejan de contar como "visibles".
        args += ["--headless=new", "--window-size=1920,1080"]
    else:
        log("🌐 Abriendo Chrome con ventana...")
        args += ["--start-maximized"]

    args.append(URL)

    global CHROME_PROC
    os.makedirs(CHROME_PROFILE, exist_ok=True)
    CHROME_PROC = subprocess.Popen(args)

    for _ in range(100):
        if port_open(DEBUG_PORT):
            break
        time.sleep(0.3)
    else:
        # Casi siempre es el perfil ocupado por un Chrome que Windows no me
        # deja cerrar ("Access is denied"). Mejor decir qué hacer que soltar
        # un traceback.
        raise RuntimeError(
            f"No pude abrir Chrome con el perfil '{os.path.basename(CHROME_PROFILE)}'.\n"
            "   Suele ser porque quedó un Chrome vivo usándolo.\n"
            "   Opciones:\n"
            "     1) Cierra Chrome por completo (o reinicia la máquina) y vuelve a correrlo.\n"
            "     2) Usa otro perfil, entrando una vez con SAML:\n"
            "        python Sanity_COM.py --perfil sanity2 --visible"
        )

    time.sleep(2)
    return False


def limpiar_pestanas_huerfanas(dejar=1):
    """
    Cierra las pestañas del scope que hayan quedado colgadas de una corrida
    anterior, ANTES de conectar Playwright.

    Esto no es opcional: al conectarse, Playwright hace attach a todos los
    targets del navegador, y con media docena de SPAs de Angular abiertas se
    cuelga con "connect_over_cdp: Timeout". Se usa la API HTTP de DevTools
    justamente porque no depende de esa conexión.
    """
    import json
    import urllib.request

    def pedir(ruta):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DEBUG_PORT}{ruta}", timeout=10
        ) as r:
            return r.read().decode("utf-8", "ignore")

    try:
        targets = json.loads(pedir("/json/list"))
    except Exception:
        return  # si no se puede, seguimos: no vale la pena abortar por esto

    # Solo las del scope, que son las que abre este script. Las de
    # /ram/execution-request/ son resultados: esas no se tocan.
    paginas = [
        t for t in targets
        if t.get("type") == "page" and "/scopes/" in t.get("url", "")
    ]

    sobran = paginas[dejar:]
    if not sobran:
        return

    log(f"🧹 Cierro {len(sobran)} pestaña(s) que quedaron de antes.")
    for t in sobran:
        try:
            pedir(f"/json/close/{t['id']}")
        except Exception:
            pass
    time.sleep(1)


async def abrir_sesion(pw, headless):
    """
    Deja una sesión lista para trabajar.

    Si se pidió headless y resulta que hay que iniciar sesión (la de ATP
    caduca cada tanto), no se rinde: cierra ese Chrome, abre otro CON ventana
    para que entres, y sigue solo en cuanto cargue la tabla. Así no hay que
    acordarse de correr nada aparte con --visible.

    Devuelve (context, page, headless_real) o None.
    """
    modos = [headless, False] if headless else [False]

    for sin_ventana in modos:
        launch_chrome(headless=sin_ventana)
        limpiar_pestanas_huerfanas()

        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0]
        page = await pagina_principal(context)
        page.set_default_timeout(20000)

        await click_saml(page)
        # Con ventana damos 5 minutos: ahí eres tú quien teclea.
        estado = await wait_for_table(
            page,
            timeout=180000 if sin_ventana else 300000,
            headless=sin_ventana,
        )

        if estado is True:
            return context, page, sin_ventana

        if estado == "login":
            log("\n🔐 El SSO no se completa sin ventana. Abro Chrome con ventana.")
            log("   Normalmente entra solo; si te pide credenciales, escríbelas")
            log("   y el script sigue en cuanto cargue la tabla.\n")
            await cerrar_chrome(context)
            continue

        return None

    return None


async def cerrar_chrome(context):
    """
    Apaga el Chrome del script.

    Primero cierra las pestañas: eso hace que Chrome guarde bien el perfil
    (cookies y sesión SAML incluidas) y así la próxima vez no te pide login.
    En headless no se apaga solo por quedarse sin pestañas, así que después
    hay que cerrar el proceso.
    """
    try:
        for p in list(context.pages):
            try:
                await p.close()
            except Exception:
                pass
    except Exception:
        pass

    # Browser.close por CDP = cerrar Chrome "por las buenas": guarda cookies y
    # sesión antes de salir. Matarlo con taskkill /F (lo que hacíamos antes)
    # se las llevaba a medio escribir, y a la corrida siguiente te pedía login.
    try:
        sesion = await context.browser.new_browser_cdp_session()
        await sesion.send("Browser.close")
    except Exception:
        pass

    for _ in range(20):
        if not port_open(DEBUG_PORT):
            log("🚪 Chrome cerrado.")
            return
        await asyncio.sleep(0.3)

    if CHROME_PROC is not None:
        for forzar in (False, True):
            cmd = ["taskkill", "/PID", str(CHROME_PROC.pid), "/T"]
            if forzar:
                cmd.append("/F")
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
            except Exception:
                pass
            for _ in range(10):
                if not port_open(DEBUG_PORT):
                    log("🚪 Chrome cerrado.")
                    return
                await asyncio.sleep(0.3)

    # Si reusamos un Chrome de una corrida anterior no tenemos su proceso,
    # pero el perfil es exclusivo del script: se cierra por ahí.
    liberar_perfil()
    if not port_open(DEBUG_PORT):
        log("🚪 Chrome cerrado.")
    else:
        log("🚪 Chrome quedó abierto; la próxima corrida lo reusa.")


async def pagina_principal(context):
    # Tiene que ser una pestaña DEL SCOPE. Antes bastaba con que fuera de ATP
    # y agarraba una de /ram/execution-request/, que no tiene la tabla: se iba
    # medio minuto esperando y luego renavegando.
    for p in context.pages:
        if "/scopes/" in p.url:
            return p
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(URL, wait_until="load", timeout=60000)
    except Exception as e:
        log(f"⚠️ goto(): {type(e).__name__}")
    return page


# ---------------- DAILY ENVS ----------------
def leer_envs_passed():
    """
    Saca de Daily_Envs.txt los COM que quedaron en PASSED.

    El archivo trae varios bloques (## **CI_23 — Build**, etc.), cada uno con
    sus propios encabezados de estado. El estado se resetea en cada bloque
    nuevo y en "Environment lists:", para no arrastrar un PASSED de arriba.

    Solo cuentan las líneas que son link markdown: así ignoramos renglones
    como "- CI_23 SS -> COM15, COM38, COM40" (la lista del release) y
    "nttcom23_ci_48664/..." (un snapshot), que no son envs desplegados.
    """
    encontrados = []
    estado = None

    with open(DAILY_ENVS_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for linea in f:
            limpia = linea.strip()

            if limpia.startswith("##") or limpia.lower().startswith("environment lists"):
                estado = None
                continue

            cabecera = STATUS_HEADER_RE.match(limpia)
            if cabecera:
                estado = cabecera.group(1)
                continue

            if estado != "PASSED":
                continue

            for etiqueta, url in LINK_RE.findall(linea):
                # El número sale de la URL, que es la fuente de verdad.
                m = NTTCOM_RE.search(url) or COM_LABEL_RE.search(etiqueta)
                if m:
                    numero = str(int(m.group(1)))
                    if numero not in encontrados:
                        encontrados.append(numero)

    return encontrados


def envs_del_daily():
    print("=" * 55)
    print("  SANITY COM")
    print("=" * 55)
    print(f"📋 Leyendo {DAILY_ENVS_PATH}")

    try:
        envs = leer_envs_passed()
    except FileNotFoundError:
        print("❌ No encontré Daily_Envs.txt en esa ruta.")
        return []
    except Exception as e:
        print(f"❌ No pude leer Daily_Envs.txt: {type(e).__name__}: {e}")
        return []

    if not envs:
        print("ℹ️ No hay ningún COM en PASSED ahorita.")
        return []

    print(f"✅ COM en PASSED: {', '.join('com' + n for n in envs)}")
    return envs


# ---------------- MAIN ----------------
async def main(envs, dry_run=False, headless=True, jobs=MAX_JOBS, paralelo=True,
               dejar_abierto=False):
    arranque = time.time()
    resultados, tiempos = {}, {}

    async with async_playwright() as pw:
        # El login se hace UNA sola vez aquí; las pestañas de los workers
        # heredan la sesión y entran directo.
        sesion = await abrir_sesion(pw, headless)
        if sesion is None:
            log("🛑 Me detengo: no llegué a la tabla.")
            return

        context, page, headless = sesion

        try:
            log(f"✅ Sesión lista en {time.time() - arranque:.0f}s")

            if paralelo and len(envs) > 1:
                resultados, tiempos = await correr_paralelo(
                    context, envs, not dry_run, headless, jobs
                )
            else:
                resultados, tiempos = await correr_secuencial(page, envs, not dry_run)

        finally:
            # Se cierra pase lo que pase: si no, el Chrome se queda colgado y
            # la próxima corrida se lo encuentra con pestañas viejas.
            if not dejar_abierto:
                await cerrar_chrome(context)

    total = time.time() - arranque
    ok = [n for n, v in resultados.items() if v]
    fallidos = [n for n, v in resultados.items() if not v]

    print(f"\n{'=' * 55}")
    print(f"✅ Lanzados: {len(ok)}/{len(envs)}   ⏱️ {total:.0f}s en total")

    links = [(n, v) for n, v in resultados.items() if v and str(v).startswith("http")]
    if links:
        print("\n🔗 Ejecuciones:")
        for numero, link in links:
            print(f"   com{numero}: {link}")

    if fallidos:
        print(f"\n❌ Fallaron: {', '.join('com' + n for n in fallidos)}")
        print("   Un environment solo cuenta como lanzado si se abrió su")
        print("   ejecución y tenemos el link; si no, hay que revisarlo a mano.")
        print(f"   Screenshots en {SHOT_DIR}")

    if tiempos:
        detalle = "  ".join(f"com{n} {t:.0f}s" for n, t in sorted(tiempos.items()))
        print(f"\n⏱️ Por environment: {detalle}")
    print("=" * 55)


if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry" in args or "--prueba" in args
    headless = not ("--visible" in args or "--ventana" in args)
    paralelo = "--secuencial" not in args
    dejar_abierto = "--dejar-abierto" in args

    # --perfil deja usar un perfil de Chrome aparte, por si el de siempre
    # quedó bloqueado por un proceso que no se puede cerrar.
    if "--perfil" in args:
        try:
            CHROME_PROFILE = os.path.join(
                os.environ.get("LOCALAPPDATA", "."), args[args.index("--perfil") + 1]
            )
            globals()["CHROME_PROFILE"] = CHROME_PROFILE
            print(f"📁 Perfil de Chrome: {CHROME_PROFILE}")
        except IndexError:
            print("⚠️ --perfil necesita un nombre. Uso el default.")

    jobs = MAX_JOBS
    if "--jobs" in args:
        try:
            jobs = int(args[args.index("--jobs") + 1])
        except (IndexError, ValueError):
            print("⚠️ --jobs necesita un número. Uso el default.")

    # Ojo: el número de --jobs no debe colarse como environment.
    desde_argv = [
        a for i, a in enumerate(args)
        if a.isdigit() and not (i and args[i - 1] in ("--jobs", "--perfil"))
    ]

    envs = desde_argv or envs_del_daily()
    if not envs:
        print("\n⚠️ Nada que correr. Si quieres forzar uno: python Sanity_COM.py 25")
        sys.exit(1)

    try:
        asyncio.run(main(envs, dry_run=dry, headless=headless, jobs=jobs,
                         paralelo=paralelo, dejar_abierto=dejar_abierto))
    except KeyboardInterrupt:
        print("\n🛑 Cancelado.")
        sys.exit(1)
    except RuntimeError as e:
        # Problemas esperables (perfil ocupado, etc.): el mensaje ya explica
        # qué hacer, no hace falta soltar el traceback encima.
        print(f"\n❌ {e}")
        sys.exit(1)

    print("✅ TODO COMPLETED")
