"""
Una página de Playwright de mentira que anota todo lo que le hacen.

Sirve para probar el llenado de formularios de Jenkins sin Jenkins: se corre
el código contra FakePage y luego se revisa la bitácora (`page.calls`). Si
un refactor cambia el orden de los clicks, un timeout o un texto, la
bitácora cambia y la prueba lo detecta.
"""


def _describe(value):
    if callable(value):
        return "<callable>"
    return value


class FakeLocator:
    def __init__(self, page, path):
        self.page = page
        self.path = path

    def _record(self, method, *args, **kwargs):
        self.page.calls.append(
            [self.path, method, [_describe(a) for a in args], {k: _describe(v) for k, v in sorted(kwargs.items())}]
        )

    # Navegación del árbol: no se anota, sólo arma el "path".
    @property
    def first(self):
        return FakeLocator(self.page, f"{self.path}.first")

    def nth(self, index):
        return FakeLocator(self.page, f"{self.path}.nth({index})")

    def locator(self, selector):
        return FakeLocator(self.page, f"{self.path} >> {selector}")

    # Consultas
    async def count(self):
        self._record("count")
        return self.page.counts.get(self._key(), 1)

    async def is_checked(self):
        self._record("is_checked")
        return self.page.checked.get(self._key(), False)

    async def is_visible(self, **kwargs):
        self._record("is_visible", **kwargs)
        return True

    async def is_enabled(self):
        self._record("is_enabled")
        return True

    async def input_value(self):
        self._record("input_value")
        return self.page.values.get(self._key(), "")

    # Acciones
    async def click(self, **kwargs):
        self._record("click", **kwargs)
        key = self._key()
        if key.endswith(" >> label"):
            # Clickear el <label> de un checkbox lo prende a el, salvo que se
            # este simulando el caso "el click no truena pero tampoco pega".
            checkbox_key = key[: -len(" >> label")] + ' >> input[type="checkbox"]'
            if checkbox_key not in self.page.stays_unchecked:
                self.page.checked[checkbox_key] = True

    async def check(self, **kwargs):
        self._record("check", **kwargs)
        key = self._key()
        if key in self.page.fail_check:
            raise RuntimeError("check() no pegó")
        if key not in self.page.stays_unchecked:
            self.page.checked[key] = True

    async def uncheck(self, **kwargs):
        self._record("uncheck", **kwargs)
        self.page.checked[self._key()] = False

    async def fill(self, value):
        self._record("fill", value)
        key = self._key()
        if key in self.page.stays_empty:
            return
        self.page.values[key] = value

    async def select_option(self, **kwargs):
        self._record("select_option", **kwargs)

    async def scroll_into_view_if_needed(self, **kwargs):
        self._record("scroll_into_view_if_needed", **kwargs)

    async def wait_for(self, **kwargs):
        self._record("wait_for", **kwargs)

    def _key(self):
        """Clave sin '.first' ni '.nth(n)', para configurar respuestas por selector."""
        return self.path.replace(".first", "").split(".nth(")[0]


class _Device:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    def __getattr__(self, method):
        async def action(*args, **kwargs):
            self.page.calls.append([self.name, method, list(args), dict(sorted(kwargs.items()))])

        return action


class FakePage:
    """
    counts:        {selector: n}  cuántos elementos "hay" (default 1)
    checked:       {selector: True}  checkboxes que ya vienen marcados
    fail_check:    {selector}  checkboxes cuyo check() truena (para probar el plan B)
    stays_unchecked: {selector}  checkboxes donde check()/click() NO avientan
                     excepción pero tampoco prenden nada (el bug que reporto
                     Alex: "a veces no hace click en SD file")
    stays_empty:   {selector}  campos donde fill() no avienta excepción pero
                     el valor nunca queda escrito
    """

    def __init__(
        self,
        counts=None,
        checked=None,
        fail_check=None,
        stays_unchecked=None,
        stays_empty=None,
        url_after_submit="https://jenkins/job/x/123/",
    ):
        self.calls = []
        self.counts = counts or {}
        self.checked = dict(checked or {})
        self.values = {}
        self.fail_check = set(fail_check or ())
        self.stays_unchecked = set(stays_unchecked or ())
        self.stays_empty = set(stays_empty or ())
        self.url_after_submit = url_after_submit
        self.keyboard = _Device(self, "keyboard")
        self.mouse = _Device(self, "mouse")

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, role, **kwargs):
        return FakeLocator(self, f"role={role}[{kwargs.get('name')}]")

    async def wait_for_load_state(self, *args, **kwargs):
        self.calls.append(["page", "wait_for_load_state", list(args), dict(sorted(kwargs.items()))])

    async def wait_for_selector(self, *args, **kwargs):
        self.calls.append(["page", "wait_for_selector", list(args), dict(sorted(kwargs.items()))])

    async def wait_for_timeout(self, ms):
        self.calls.append(["page", "wait_for_timeout", [ms], {}])

    async def wait_for_url(self, predicate, **kwargs):
        self.calls.append(["page", "wait_for_url", ["<callable>"], dict(sorted(kwargs.items()))])
        if not predicate(self.url_after_submit):
            raise TimeoutError("Jenkins no redirigió")
