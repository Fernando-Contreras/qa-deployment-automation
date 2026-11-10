"""
Page Objects: una clase por página, con sus localizadores y sus acciones.

La idea (Page Object Model): el script que usa la página NO sabe cómo está
armado su HTML. Pide "dispara este deployment" y la clase sabe dónde están
los botones. Si Jenkins cambia un selector, se arregla aquí, en un solo
lugar, y no en cada script que toca esa página.

JenkinsBuildPage es la primera. No reescribe nada: guarda la pestaña y el env
una sola vez y delega en JenkinsForm (jenkins_web.py), que ya estaba probado.
Por eso hace exactamente los mismos clicks, y tests/test_pages.py lo demuestra
comparándolo con la grabación original.
"""


class JenkinsLoginPage:
    """La pantalla de inicio de sesión de Jenkins (usuario y contraseña)."""

    def __init__(self, page, form):
        self.page = page
        self.form = form

    # ---------- localizadores ----------
    @property
    def username_field(self):
        return self.page.locator('input[name="j_username"]')

    @property
    def password_field(self):
        return self.page.locator('input[name="j_password"]')

    # ---------- acciones ----------
    async def open(self, url: str):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=1200000)

    async def sign_in(self) -> bool:
        """Entra si hace falta. True = tuvo que escribir usuario y contraseña;
        False = ya había sesión guardada (no apareció el formulario)."""
        return await self.form.login_if_needed(self.page)

    async def close(self):
        await self.page.close()


class JenkinsBuildPage:
    """La página 'Build with Parameters' de un job de deployment, para UN env."""

    def __init__(self, page, form, env: str):
        self.page = page      # la pestaña de Playwright
        self.form = form      # JenkinsForm: sabe llenar el formulario
        self.env = env        # "06", "101", "26"...: sale en los logs

    # ---------- localizadores (lo único que sabe del HTML) ----------
    def parameter(self, name: str):
        """El renglón del formulario cuyo texto trae 'name' (SD_FILE, CLEANUP_FS...)."""
        return self.page.locator(f'div.jenkins-form-item:has-text("{name}")')

    def checkbox(self, name: str):
        return self.parameter(name).first.locator('input[type="checkbox"]')

    @property
    def build_button(self):
        return self.page.get_by_role("button", name="Build").first

    # ---------- acciones (lo que el script quiere hacer) ----------
    async def open(self, url: str):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=1200000)
        await self.wait_until_ready()

    async def wait_until_ready(self):
        await self.form.wait_for_job_form(self.page)

    async def login_if_needed(self):
        return await self.form.login_if_needed(self.page)

    async def enable(self, name: str):
        """Marca un checkbox y se asegura de que haya quedado marcado."""
        await self.form.click_checkbox_by_label(self.page, self.env, name)

    async def deploy_sd_file(self, sd_file: str, index: int, total: int):
        """Deployment con SD file: llena el formulario completo y da Build."""
        await self.form.configure_sd_file(self.page, sd_file, self.env, index, total)

    async def build(self, index: int, total: int):
        await self.form.click_build(self.page, self.env, index, total)
