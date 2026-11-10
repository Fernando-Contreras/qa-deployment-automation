"""
Piezas para manejar Jenkins desde una página de Playwright.

QA_Envs.py (nttcw) y COM_Envs.py (nttcom) llenaban el formulario de build con
el mismo código copiado y pegado, que ya se empezaba a separar con el tiempo.
Ahora vive aquí una sola vez: cada script crea su JenkinsForm con su prefijo
de log y sus credenciales, y lo demás es idéntico para los dos.

También vive aquí la llamada a la API de Jenkins "desde adentro" de la página,
que usan Track_Deployments, MailQAs y Deployment_Defect.
"""

import asyncio
import json

from nc_credentials import LoginError, load_session_state, save_session_state

# Lo que indica que el formulario de parámetros ya se pintó.
FORM_READY_SELECTOR = 'div.jenkins-form-item, form[name="parameters"], button[type="submit"]'

# -------------------------
# SESIÓN COMPARTIDA (sdci + snapci)
# -------------------------
# Track_Deployments corre cada 10 min de 3 a 11 pm y mantiene viva la sesión de
# Jenkins; QA_Envs, COM_Envs y MailQAs la reusan en vez de loguearse cada vez.
# Va cifrada con DPAPI (ver nc_credentials.save_session_state): una cookie de
# sesión equivale a una contraseña mientras dure.
SESSION_NAME = "jenkins"


async def new_context_with_session(browser, state):
    """
    new_context() cargando la sesión guardada. Si no hay, o no sirve, cae a
    un contexto limpio — y el login de siempre se hace solo.
    """
    if state:
        try:
            return await browser.new_context(storage_state=state)
        except Exception as e:
            print(f"⚠ La sesión guardada no sirvió ({e}); se hace login normal.")
    return await browser.new_context()


async def persist_session(context, name=SESSION_NAME):
    """
    Guarda la sesión ya autenticada para la próxima corrida (o para otro
    script). Es de cortesía: si algo falla aquí, se sigue sin ella, nunca se
    rompe la corrida. Regresa el state (para reusarlo) o None.
    """
    try:
        state = await context.storage_state()
        save_session_state(name, state)
        return state
    except Exception as e:
        print(f"⚠ No pude guardar la sesión para la próxima corrida: {e}")
        return None


def saved_session(name=SESSION_NAME):
    """La sesión guardada (o None). Atajo para quien arma su propio contexto."""
    return load_session_state(name)


async def fetch_json_from_browser(page, url):
    """
    GET a la API de Jenkins con las cookies de la pestaña ya logueada.
    Se hace con fetch() dentro de la página para no tener que volver a
    loguear con otro cliente.
    """
    response_text = await page.evaluate(
        """async (apiUrl) => {
            const response = await fetch(apiUrl, { credentials: 'same-origin' });
            if (!response.ok) {
                throw new Error(`${response.status} ${response.statusText}`);
            }
            return await response.text();
        }""",
        url,
    )
    return json.loads(response_text)


class JenkinsForm:
    """El formulario de "Build with Parameters" de un job de deployment."""

    def __init__(
        self,
        log_prefix,
        username,
        password,
        checkbox_deadline_ms=180000,
        fill_attempts=3,
    ):
        self.log_prefix = log_prefix
        self.username = username
        self.password = password
        # Configurables para que las pruebas no tengan que esperar minutos
        # reales antes de ver el error que estan probando.
        self.checkbox_deadline_ms = checkbox_deadline_ms
        self.fill_attempts = fill_attempts

    def log(self, env: str, message: str):
        # Con varios envs corriendo a la vez, el prefijo dice quien habla.
        print(f"[{self.log_prefix}{env}] {message}")

    @staticmethod
    def format_progress(current_index: int, total_envs: int) -> str:
        return f"({current_index}/{total_envs})"

    async def wait_for_job_form(self, page):
        await page.wait_for_load_state("domcontentloaded", timeout=180000)
        try:
            await page.wait_for_selector(FORM_READY_SELECTOR, timeout=180000)
        except Exception:
            await page.wait_for_load_state("networkidle", timeout=180000)
            await page.wait_for_selector(FORM_READY_SELECTOR, timeout=180000)

    async def login_if_needed(self, page):
        """
        Como do_login, pero sin su espera de hasta 60 s cuando ya hay sesión.

        do_login espera a que APAREZCA el formulario de login; con una sesión
        guardada no aparece nunca, y esa espera costaría más que el login que
        se quiere evitar. Aquí se revisa directo: la página ya cargó su DOM
        (los callers hicieron goto con domcontentloaded), así que si no hay
        formulario, ya estás dentro. Regresa True si tuvo que loguearse.
        """
        if await page.locator('input[name="j_username"]').count() == 0:
            print("ℹ️ Ya hay sesión (no apareció el login).")
            return False

        await self.do_login(page)
        return True

    async def do_login(self, page):
        try:
            await page.wait_for_selector('input[name="j_username"]', timeout=60000)
        except Exception:
            print("ℹ️ No aparecio el login (parece que ya hay sesion).")
            return

        await page.locator('input[name="j_username"]').fill(self.username)
        await page.locator('input[name="j_password"]').fill(self.password)
        await page.keyboard.press("Enter")

        try:
            await page.wait_for_selector(
                'input[name="j_username"]', state="detached", timeout=180000
            )
        except Exception:
            raise LoginError("jenkins", "Jenkins no acepto usuario/contrasena")

    async def click_checkbox_by_label(self, page, env: str, label_text: str):
        """Marca el checkbox de 'label_text' y SE ASEGURA de que haya quedado marcado.

        Sin ventana no hay quien note un click que "truena bien" pero no prende
        el checkbox (pasa: overlay encima, el elemento se mueve, Jenkins tarda
        en habilitarlo). Por eso aqui no basta con que check()/click() no avienten
        excepcion: se relee is_checked() y, si sigue sin marcar, se reintenta
        desde cero hasta el deadline. Si nunca prende, truena en vez de seguir
        como si nada.
        """
        self.log(env, f"🔎 Looking for: {label_text}")

        deadline_ms = self.checkbox_deadline_ms
        started = asyncio.get_running_loop().time()
        attempt = 0

        while (asyncio.get_running_loop().time() - started) * 1000 < deadline_ms:
            attempt += 1
            container = page.locator(
                f'div.jenkins-form-item:has-text("{label_text}")'
            ).first

            if await container.count() > 0:
                try:
                    await container.scroll_into_view_if_needed(timeout=10000)
                    checkbox = container.locator('input[type="checkbox"]').first
                    label = container.locator("label").first

                    if await checkbox.count() > 0:
                        if await checkbox.is_checked():
                            self.log(env, f"✅ {label_text} clicked.")
                            return

                        try:
                            await checkbox.check(force=True, timeout=10000)
                        except Exception:
                            pass

                        if await checkbox.is_checked():
                            self.log(env, f"✅ {label_text} clicked.")
                            return

                        # El check() directo no prendio: se prueba el label.
                        try:
                            await label.click(timeout=10000)
                        except Exception:
                            pass

                        if await checkbox.is_checked():
                            self.log(env, f"✅ {label_text} clicked.")
                            return

                        self.log(
                            env,
                            f"⚠️ {label_text}: el click no lo dejo marcado "
                            f"(intento {attempt}), reintento...",
                        )
                except Exception:
                    pass

            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(600)

        raise RuntimeError(
            f"❌ No pude dejar marcado el checkbox tras {deadline_ms // 1000}s: {label_text}"
        )

    async def fill_and_verify(self, field, env: str, label_text: str, value: str):
        """Llena 'field' y relee el valor para confirmar que si quedo escrito.

        Un .fill() que no avienta excepcion no siempre significa que el campo
        quedo con el texto correcto (campo que se deshabilito a medio llenado,
        el foco se lo robo otro elemento, etc). Se reintenta unas cuantas veces
        y, si el valor nunca cuadra, truena en vez de disparar el build con un
        SD_FILE o snapshot vacio o a medias.
        """
        last_seen = None

        for attempt in range(1, self.fill_attempts + 1):
            await field.fill(value)
            try:
                last_seen = await field.input_value()
            except Exception:
                last_seen = None

            if last_seen == value:
                self.log(env, f"✅ {label_text} filled.")
                return

            self.log(
                env,
                f"⚠️ {label_text}: el valor no cuadro tras llenarlo "
                f"(intento {attempt}/{self.fill_attempts}), reintento...",
            )

        raise RuntimeError(
            f"❌ {label_text}: el campo no quedo con el valor esperado "
            f"tras {self.fill_attempts} intento(s) (quedo: {last_seen!r})"
        )

    async def click_build(self, page, env: str, current_index: int, total_envs: int):
        btn = page.get_by_role("button", name="Build").first
        submit_btn = page.locator('button[type="submit"]').first

        if await btn.count() > 0:
            await btn.click(no_wait_after=True, timeout=10000)
        else:
            await submit_btn.click(no_wait_after=True, timeout=10000)

        self.log(env, f"🚀 Build {self.format_progress(current_index, total_envs)} clicked.")

        # Esperar a que Jenkins acepte el POST y redirija fuera del formulario.
        # Sin esto, cerrar el navegador podria abortar el trigger.
        try:
            await page.wait_for_url(lambda url: "/build" not in url, timeout=60000)
            self.log(env, "✅ Build submitted (Jenkins respondio).")
        except Exception:
            self.log(env, "⚠️ No pude confirmar la redireccion; revisa este env en Jenkins.")

    async def get_editable_value_field(self, target, env: str, label_text: str):
        self.log(env, f"🔎 Waiting for editable field: {label_text}")

        selectors = [
            'input[name="value"][type="text"]:not([disabled])',
            'input[type="text"]:not([disabled])',
            'textarea:not([disabled])',
        ]
        deadline_ms = 180000
        started = asyncio.get_running_loop().time()
        attempt = 0

        while (asyncio.get_running_loop().time() - started) * 1000 < deadline_ms:
            attempt += 1
            for selector in selectors:
                locator = target.locator(selector)
                count = await locator.count()
                for idx in range(count):
                    candidate = locator.nth(idx)
                    try:
                        if await candidate.is_visible() and await candidate.is_enabled():
                            self.log(env, f"✅ Found editable field for {label_text}")
                            return candidate
                    except Exception:
                        pass

            if attempt % 5 == 1:
                self.log(env, f"⏳ Still waiting editable field: {label_text} ({attempt})")
            await asyncio.sleep(0.5)

        raise RuntimeError(f"❌ Editable field not found after {deadline_ms // 1000}s: {label_text}")

    async def configure_sd_file(
        self, page, sd_file: str, env: str, current_index: int, total_envs: int
    ):
        """Deploy-Parallel con SD file: SD_FILE + las tres casillas de siempre + Build."""
        await self.click_checkbox_by_label(page, env, "SD_FILE")
        sd_file_container = page.locator('div.jenkins-form-item:has-text("SD_FILE")').first
        await sd_file_container.wait_for(state="visible", timeout=180000)
        # El input de SD_FILE aparece despues del checkbox; sin esta pausa
        # get_editable_value_field puede agarrar el input de otro parametro.
        await page.wait_for_timeout(3000)
        self.log(env, "Finding SD_FILE input...")
        sd_file_input = await self.get_editable_value_field(page, env, "SD_FILE")
        await self.fill_and_verify(sd_file_input, env, "SD_FILE", sd_file)
        self.log(env, "Enabling CLEANUP_FS...")
        await self.click_checkbox_by_label(page, env, "CLEANUP_FS")
        self.log(env, "Enabling STOP_APPS_BEFORE...")
        await self.click_checkbox_by_label(page, env, "STOP_APPS_BEFORE")
        self.log(env, "Enabling START_APPS...")
        await self.click_checkbox_by_label(page, env, "START_APPS")
        await self.click_build(page, env, current_index, total_envs)
