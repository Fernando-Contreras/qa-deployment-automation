import asyncio
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from playwright.async_api import async_playwright, BrowserContext, Page

from nc_credentials import LoginError, get_credentials

# El parseo del Daily_Envs y el chequeo de status son de Track_Deployments.
# La decision de QUE triggerear vive en Daily_Envs_Plan, compartida con
# QA_Envs.py para que los dos decidan igual.
import Daily_Envs_Plan as plan
import Track_Deployments as td
from pages import JenkinsBuildPage, JenkinsLoginPage
from jenkins_web import (
    JenkinsForm,
    new_context_with_session,
    persist_session,
    saved_session,
)
from ntt_config import SNAPCI, SDCI, JENKINS_DEFAULT_USER, JENKINS_FOLDER

# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
USERNAME, PASSWORD = get_credentials("jenkins", default_username=JENKINS_DEFAULT_USER)

# -------------------------
# Config
# -------------------------
# Cuantos envs se configuran al mismo tiempo.  COM_ENVS_PARALLEL=1 -> secuencial.
MAX_PARALLEL = int(os.environ.get("COM_ENVS_PARALLEL", "6"))
# Por default corre sin ventana.  COM_ENVS_HEADLESS=0 para verlo.
HEADLESS = os.environ.get("COM_ENVS_HEADLESS", "1") != "0"

SD_HOST = SDCI
SNAPSHOT_HOST = SNAPCI
FOLDER_PATH = JENKINS_FOLDER

# El formulario de Jenkins es el mismo que el de QA_Envs.py; vive en
# jenkins_web.py. Aquí sólo cambia el prefijo del log.
_form = JenkinsForm("nttcom", USERNAME, PASSWORD)
log = _form.log
format_progress = _form.format_progress
wait_for_job_form = _form.wait_for_job_form
do_login = _form.do_login
login_if_needed = _form.login_if_needed
click_checkbox_by_label = _form.click_checkbox_by_label
click_build = _form.click_build
get_editable_value_field = _form.get_editable_value_field
configure_sd_file = _form.configure_sd_file

# Checkboxes que se activan en el job de snapshot de COM.
# SEND_EMAIL_REPORT ya viene palomeado por default, por eso no esta aqui.
SNAPSHOT_CHECKBOXES = [
    "RESTORE_SNAPSHOT",
    "RUN_BO_CLEANUP",
    "FIX_HOST_ENV_NAME",
    "INSTALL_BILLOPS_GS",
    "START_DB_AFTER",
    "START_APPS_AFTER",
]

# Aunque el env sea COM, el tenant sigue nombrandose QATENANT_N.
TENANT_PREFIX = "QATENANT_"

# El equipo a veces renombra el folder del job de snapshot. Primero se usa el
# link que venga en el Daily_Envs; estos son el plan B.
SNAPSHOT_JOB_NAMES = [
    "NTT-Create-Eatlon-Snapshot",
    "Restore-Eatlon-SnapShot",
]

FALSEY = ("false", "0", "no", "off")


def parse_args(argv):
    """Lee --headless=false / --no-headless / --parallel=N / --manual."""
    headless = HEADLESS
    parallel = MAX_PARALLEL
    manual = False

    for arg in argv:
        key, _, value = arg.partition("=")
        key = key.strip().lower()
        value = value.strip().lower()

        if key in ("--headless", "-headless"):
            # "--headless" solo => True; "--headless=false" => False
            headless = value not in FALSEY if value else True
        elif key in ("--no-headless", "--show", "--gui"):
            headless = False
        elif key in ("--m", "-m", "--manual", "-manual"):
            manual = True
        elif key in ("--parallel", "-parallel", "-p"):
            if value.isdigit() and int(value) > 0:
                parallel = int(value)
            else:
                print(f"⚠️ Ignoro '{arg}': --parallel necesita un numero mayor a 0.")
        else:
            print(f"⚠️ Flag desconocido, lo ignoro: {arg}")

    return headless, parallel, manual


# -------------------------
# Helpers
# -------------------------
def normalize_envs(txt: str):
    # Los COM no llevan cero de relleno: COM3 -> nttcom3_qa (no nttcom03_qa).
    # Acepta "COM26, COM2" o "26,2" -> ["26", "2"].
    envs = []
    for raw in txt.split(","):
        digits = re.sub(r"\D", "", raw)
        if digits:
            envs.append(str(int(digits)))
    return envs


def build_sdci_url(env: str) -> str:
    return f"{SD_HOST}{FOLDER_PATH}job/nttcom{env}_qa/job/Deploy-Parallel/build?delay=0sec"


def build_snapshot_url(env: str, job_name: str) -> str:
    return f"{SNAPSHOT_HOST}{FOLDER_PATH}job/nttcom{env}_qa/job/{job_name}/build?delay=0sec"


# Agarra el host + todos los /job/... seguidos, ignorando numero de build o
# cualquier cola (/45/, /console, etc).
JOB_BASE_RE = re.compile(r"^(https://[^/]+(?:/job/[^/]+)+)", re.IGNORECASE)


def job_base_from_url(url: str):
    match = JOB_BASE_RE.match(url.strip().rstrip("/"))
    return match.group(1) if match else None


def candidate_build_urls(source_url: str, env: str, snapshot_mode: bool):
    """URLs de build a probar, en orden.

    Primero la que sale del propio link del Daily_Envs (esa es la que tu ves
    funcionando), y luego los nombres conocidos como plan B.
    """
    urls = []

    base = job_base_from_url(source_url)
    if base:
        urls.append(f"{base}/build?delay=0sec")

    if snapshot_mode:
        urls.extend(build_snapshot_url(env, name) for name in SNAPSHOT_JOB_NAMES)
    else:
        urls.append(build_sdci_url(env))

    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


# La regla del tenant (sufijo _T<numero> del snapshot) vive en Daily_Envs_Plan,
# compartida con QA_Envs.py.
tenant_from_snapshot = plan.tenant_from_snapshot


def alternate_snapshot_urls(url: str):
    """Los otros nombres posibles del mismo job de snapshot.

    Sirve cuando el link del Daily_Envs apunta a un nombre que ya no existe:
    el status sale UNKNOWN no porque el env este raro, sino porque el link
    esta mal.
    """
    base = job_base_from_url(url)
    if not base:
        return []

    trailing = "/" if url.strip().endswith("/") else ""
    prefix, _, current = base.rpartition("/job/")
    if current not in SNAPSHOT_JOB_NAMES:
        return []

    return [
        f"{prefix}/job/{name}{trailing}"
        for name in SNAPSHOT_JOB_NAMES
        if name != current
    ]


async def retry_unknown_links(context, deployments):
    """Reintenta los UNKNOWN con el otro nombre de job y corrige el Daily_Envs.

    Un env sin fecha suele ser un link viejo, no un env descompuesto.
    """
    pendientes = [
        d for d in deployments
        if d.get("started") is None and alternate_snapshot_urls(d["url"])
    ]
    if not pendientes:
        return

    print(f"\n🔁 {len(pendientes)} env(s) sin fecha; pruebo con el otro nombre de job...")

    fixes = []
    session_pages = {}

    for deployment in pendientes:
        label = td.extract_env_label(deployment["url"])

        for alt_url in alternate_snapshot_urls(deployment["url"]):
            host = urlparse(alt_url).netloc
            if host not in session_pages:
                try:
                    session_pages[host] = await td.open_session_page(context, alt_url)
                except Exception:
                    session_pages[host] = None

            try:
                info = await td.check_status(context, session_pages[host], alt_url)
            except Exception:
                continue

            if info.get("started") is None:
                continue

            job = alt_url.rstrip("/").rsplit("/job/", 1)[-1]
            print(f"   ✅ {label}: si tiene builds en {job} ({info.get('status')}).")
            fixes.append((td.extract_env_number(deployment["url"]), deployment["url"], alt_url))
            deployment["url"] = alt_url
            deployment.update(info)
            break
        else:
            print(f"   ❔ {label}: tampoco con el otro nombre; se queda sin fecha.")

    fix_links_in_daily_envs(fixes)


def link_for_daily_envs(used_build_url: str, source_url: str) -> str:
    """De la URL de build que si sirvio, saca el link como va en el Daily_Envs.

    O sea sin /build?delay=0sec, y respetando si el link original terminaba
    en diagonal o no.
    """
    base = job_base_from_url(used_build_url)
    if not base:
        return None
    return base + "/" if source_url.strip().endswith("/") else base


def fix_links_in_daily_envs(fixes):
    """Corrige en el Daily_Envs los links cuyo job resulto tener otro nombre.

    'fixes' es una lista de (env, link_viejo, link_nuevo).
    """
    if not fixes:
        return

    with open(td.FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    applied = []
    missing = []
    for env, old_link, new_link in fixes:
        if old_link in text:
            text = text.replace(old_link, new_link)
            applied.append((env, old_link, new_link))
        else:
            missing.append((env, old_link))

    if applied:
        with open(td.FILE_PATH, "w", encoding="utf-8") as f:
            f.write(text)

        print(f"\n📝 {td.FILE_PATH} actualizado ({len(applied)} link(s) corregido(s)):")
        for env, old_link, new_link in applied:
            old_job = old_link.rstrip("/").rsplit("/job/", 1)[-1]
            new_job = new_link.rstrip("/").rsplit("/job/", 1)[-1]
            print(f"   COM{env}QA: {old_job} → {new_job}")

    for env, old_link in missing:
        print(f"⚠️ COM{env}QA: ya no encontre su link en el Daily_Envs; no lo toque.")
        print(f"      ↳ {old_link}")


async def is_build_form(page: Page, timeout_ms: int = 30000) -> bool:
    """True si la pagina realmente trae el formulario de parametros."""
    try:
        await page.wait_for_selector(
            'form[name="parameters"], div.jenkins-form-item', timeout=timeout_ms
        )
        return True
    except Exception:
        return False


async def open_job_form(page: Page, env: str, source_url: str, snapshot_mode: bool) -> str:
    """Abre el formulario de build del env. Devuelve la URL que si sirvio."""
    urls = candidate_build_urls(source_url, env, snapshot_mode)
    problems = []

    for url in urls:
        job_name = url.rstrip("/").split("/job/")[-1].split("/")[0]
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=1200000)
        except Exception as exc:
            problems.append(f"{job_name}: {exc}")
            log(env, f"⚠️ {job_name} no abrio, pruebo el siguiente...")
            continue

        # OJO: no se puede juzgar por el status code. Jenkins responde
        # HTTP 405 en el GET de /build y AUN ASI pinta el formulario de
        # parametros. El unico juez confiable es si el formulario aparece.
        status = response.status if response else 0
        if status == 404:
            problems.append(f"{job_name}: HTTP 404 (no existe)")
            log(env, f"⚠️ {job_name} no existe (404), pruebo el siguiente...")
            continue

        if not await is_build_form(page):
            problems.append(f"{job_name}: HTTP {status}, sin formulario de build")
            log(env, f"⚠️ {job_name} no mostro el formulario, pruebo el siguiente...")
            continue

        log(env, f"✅ Job encontrado: {job_name}")
        return url

    raise RuntimeError("❌ Ningun job funciono -> " + " | ".join(problems))


async def fill_nitro_snapshot(page: Page, env: str, snapshot: str):
    container = page.locator('div.jenkins-form-item:has-text("NITRO_SNAPSHOT_NAME")').first
    await container.scroll_into_view_if_needed(timeout=150000)
    await container.wait_for(state="visible", timeout=150000)
    log(env, "Finding NITRO_SNAPSHOT_NAME input...")
    snapshot_input = await get_editable_value_field(container, env, "NITRO_SNAPSHOT_NAME")
    await _form.fill_and_verify(snapshot_input, env, "NITRO_SNAPSHOT_NAME", snapshot)


async def select_tenant(page: Page, env: str, tenant_number: str):
    tenant_value = f"{TENANT_PREFIX}{tenant_number}"
    container = page.locator('div.jenkins-form-item:has-text("OS_TENANT_NAME")').first
    await container.scroll_into_view_if_needed(timeout=150000)
    await container.wait_for(state="visible", timeout=150000)

    # En algunos jobs OS_TENANT_NAME es un <select> y en otros un input de texto;
    # intentamos dropdown y si no hay, lo escribimos.
    dropdown = container.locator("select")
    if await dropdown.count() > 0:
        try:
            await dropdown.first.select_option(label=tenant_value)
        except Exception:
            await dropdown.first.select_option(value=tenant_value)
        log(env, f"✅ Tenant selected: {tenant_value}")
        return

    tenant_input = await get_editable_value_field(container, env, "OS_TENANT_NAME")
    await _form.fill_and_verify(tenant_input, env, "OS_TENANT_NAME", tenant_value)


async def configure_snapshot(
    page: Page, snapshot: str, tenant: str, env: str, current_index: int, total_envs: int
):
    for label in SNAPSHOT_CHECKBOXES:
        await click_checkbox_by_label(page, env, label)
    await fill_nitro_snapshot(page, env, snapshot)
    await select_tenant(page, env, tenant)
    await click_build(page, env, current_index, total_envs)


def note_link_fix(target: dict, used_build_url: str):
    """Si el job que sirvio no es el del Daily_Envs, anota la correccion."""
    source_url = target.get("source_url") or ""
    if not source_url:
        # Modo manual: no salio del Daily_Envs, no hay link que corregir.
        return

    source_base = job_base_from_url(source_url)
    used_base = job_base_from_url(used_build_url)
    if not source_base or not used_base or source_base == used_base:
        return

    new_link = link_for_daily_envs(used_build_url, source_url)
    if not new_link:
        return

    target["link_fix"] = (source_url, new_link)
    log(target["env"], "📝 El Daily_Envs apuntaba a otro job; anoto la correccion.")


async def run_env(
    context: BrowserContext,
    semaphore: asyncio.Semaphore,
    target: dict,
    current_index: int,
    total_envs: int,
):
    """Configura y dispara un env. Devuelve (env, ok, error).

    Si el job resulto llamarse distinto a como dice el Daily_Envs, deja el
    link corregido en target["link_fix"] para arreglarlo despues.
    """
    env = target["env"]

    async with semaphore:
        try:
            jenkins = JenkinsBuildPage(await context.new_page(), _form, env)
            used_url = await open_job_form(
                jenkins.page, env, target["source_url"], target["snapshot_mode"]
            )
            note_link_fix(target, used_url)

            if target["snapshot_mode"]:
                await configure_snapshot(
                    jenkins.page, target["payload"], target["tenant"],
                    env, current_index, total_envs,
                )
            else:
                await jenkins.deploy_sd_file(target["payload"], current_index, total_envs)

            return (env, True, None)
        except Exception as exc:
            log(env, f"❌ FALLO: {exc}")
            return (env, False, str(exc))


# -------------------------
# PLAN (que triggerear y con que)
# -------------------------
def build_targets(to_trigger):
    """De cada deployment saca env + SD file/snapshot. Pide tenant si hace falta."""
    targets = []
    tenant_by_patch = {}

    for deployment in to_trigger:
        env = td.extract_env_number(deployment["url"])
        patch = deployment.get("patch")

        if not env or not patch:
            label = td.extract_env_label(deployment["url"])
            print(f"⚠️ {label}: sin SD file/snapshot en su bloque del Daily_Envs; se salta.")
            continue

        snapshot_mode = plan.is_snapshot(patch)
        tenant = None

        if snapshot_mode:
            if patch not in tenant_by_patch:
                envs_here = ", ".join(
                    td.extract_env_label(d["url"])
                    for d in to_trigger
                    if d.get("patch") == patch
                )
                print(f"\n🏢 Snapshot: {patch}")
                print(f"   Envs: {envs_here}")

                auto = tenant_from_snapshot(patch)
                if auto:
                    print(f"   Tenant {auto} (del _T{auto} con que termina el nombre)")
                    tenant_by_patch[patch] = auto
                else:
                    print("   El nombre no termina en _T<numero>, asi que no lo puedo deducir.")
                    tenant_by_patch[patch] = input("   Tenant (2 / 3 / 4 / 5): ").strip()
            tenant = tenant_by_patch[patch]

        targets.append(
            {
                "env": env,
                "payload": patch,
                "snapshot_mode": snapshot_mode,
                "tenant": tenant,
                # El link del Daily_Envs ya trae el nombre correcto del job,
                # que en COM no es igual en todos los envs.
                "source_url": deployment["url"],
            }
        )

    return targets


def manual_targets():
    """Modo de toda la vida: tu dictas los envs y el SD/snapshot."""
    print("\n" + "=" * 60)
    print("✍️  MODO MANUAL")
    print("=" * 60)

    envs = normalize_envs(input("\n🧩 COM Envs (comma separated): ").strip())
    if not envs:
        print("🚫 No pusiste ningun env.")
        return []

    payload = input("\n📄 SD File OR Snapshot: ").strip()
    if not payload:
        print("🚫 No pusiste SD file ni snapshot.")
        return []

    # En manual siempre se pregunta el tenant, aunque el nombre traiga _T<n>:
    # aqui la ruta la escribiste tu a mano y puede que quieras otro tenant.
    tenant = None
    if plan.is_snapshot(payload):
        auto = tenant_from_snapshot(payload)
        pregunta = "🏢 Tenant (2 / 3 / 4 / 5)" +(f" [enter = {auto}]: " if auto else ": ")
        tenant = input(pregunta).strip() or auto

    snapshot_mode = plan.is_snapshot(payload)

    print("\n" + "=" * 60)
    print(f"🚀 Listo para triggerear {len(envs)} env(s):")
    for env in envs:
        print(f"   COM{env}QA → {payload}")
    print("=" * 60)

    return [
        {
            "env": env,
            "payload": payload,
            "snapshot_mode": snapshot_mode,
            "tenant": tenant,
            # Sin link del Daily_Envs: se prueban los nombres de job conocidos.
            "source_url": "",
        }
        for env in envs
    ]


async def plan_from_daily_envs(pw):
    """Decide que triggerear leyendo el Daily_Envs.

    Devuelve la lista de targets, o None si tu cancelaste a proposito
    (para no volverte a preguntar por el modo manual).
    """
    print(f"📄 Leyendo {td.FILE_PATH}")

    # Solo IN PROGRESS: ON HOLD / BLOCKED / FAILED / PASSED ni se miran.
    deployments, cw_count = plan.read_in_progress("com")

    if cw_count:
        print(f"ℹ️ {cw_count} env(s) nttcw en IN PROGRESS: los ve QA_Envs.py, aqui se ignoran.")

    if not deployments:
        print("\n✅ No hay envs COM en IN PROGRESS.")
        return []

    print(f"\n⏳ Revisando en Jenkins el ultimo build de {len(deployments)} env(s)...\n")

    # El chequeo va sin ventana: no hay nada que mirar y es mas rapido.
    check_browser = await pw.chromium.launch(headless=True)
    check_context = await new_context_with_session(check_browser, saved_session())
    try:
        await td.check_all_deployments(check_context, deployments)
        # Ya autenticados: se guarda para el siguiente script (y para el
        # trigger de abajo, que lo carga del disco).
        await persist_session(check_context)
        # Un env sin fecha casi siempre es un link viejo en el Daily_Envs,
        # asi que antes de rendirse se prueba el otro nombre de job.
        await retry_unknown_links(check_context, deployments)
    except LoginError as exc:
        print(exc)
        return None
    finally:
        await check_browser.close()

    now = datetime.now()
    buckets, blocked = plan.split_buckets(deployments, now)
    plan.print_plan(buckets, blocked, now)

    to_trigger = buckets["trigger"] + plan.ask_about_stale(buckets["ask"], now)

    if not to_trigger:
        print("\n✅ Nada que triggerear automaticamente.")
        return []

    if not plan.confirm_plan(to_trigger):
        print("🚫 Cancelado. No se triggereo nada.")
        return None

    return build_targets(to_trigger)


async def trigger_targets(pw, targets, headless, max_parallel):
    """Abre el navegador, se loguea y dispara todos los targets en paralelo."""
    total_envs = len(targets)
    parallel = min(max_parallel, total_envs)
    print(f"\n⚡ {total_envs} env(s), hasta {parallel} en paralelo (headless={headless}).\n")

    browser = await pw.chromium.launch(headless=headless)
    context = await new_context_with_session(browser, saved_session())

    # Login una sola vez por host: la cookie queda en el context y todas
    # las pestanas paralelas la heredan. Los snapshots viven en snapci y
    # los SD en sdci, asi que se loguea donde haga falta. Con la sesion
    # guardada ni aparece el formulario (login_if_needed no espera a que salga).
    hosts = {
        SNAPSHOT_HOST if target["snapshot_mode"] else SD_HOST
        for target in targets
    }
    logged_in_now = False
    for host in sorted(hosts):
        login_page = JenkinsLoginPage(await context.new_page(), _form)
        await login_page.open(f"{host}{FOLDER_PATH}")
        print(f"🔐 Logging in ({host})...")
        if await login_page.sign_in():
            logged_in_now = True
        print("✅ Logged in.")
        await login_page.close()
    if logged_in_now:
        await persist_session(context)
    print("")

    semaphore = asyncio.Semaphore(parallel)
    results = await asyncio.gather(
        *(
            run_env(context, semaphore, target, index, total_envs)
            for index, target in enumerate(targets, start=1)
        )
    )

    ok = [env for env, success, _ in results if success]
    failed = [(env, err) for env, success, err in results if not success]

    print("\n" + "=" * 50)
    print(
        f"✅ Disparados ({len(ok)}/{total_envs}): "
        + (", ".join(f"nttcom{e}" for e in ok) or "-")
    )
    if failed:
        print(f"❌ Fallaron ({len(failed)}):")
        for env, err in failed:
            print(f"   nttcom{env}: {err}")
    print("=" * 50)

    # Hasta aqui, ya sin pestanas corriendo en paralelo: el archivo se toca
    # una sola vez y desde un solo lado.
    fix_links_in_daily_envs(
        [
            (target["env"], *target["link_fix"])
            for target in targets
            if target.get("link_fix")
        ]
    )

    if headless:
        await browser.close()
        print("\n🎉 DONE.")
    else:
        print("\n🎉 DONE - browser left open.")
        await asyncio.sleep(9999999)


# -------------------------
# MAIN
# -------------------------
async def main():
    headless, max_parallel, manual = parse_args(sys.argv[1:])

    async with async_playwright() as pw:
        targets = []

        if not manual:
            targets = await plan_from_daily_envs(pw)
            if targets is None:
                # Login roto, o tu cancelaste a proposito. En ninguno de los
                # dos casos tiene caso ofrecerte el manual.
                print("\nNo se triggereo nada.")
                return

        # El automatico se rinde cuando el Daily_Envs esta ambiguo o los envs
        # no cuadran. En vez de dejarte sin nada, se cae al modo de siempre.
        if not targets:
            targets = manual_targets()

        if not targets:
            print("\n✅ Nada que triggerear.")
            return

        await trigger_targets(pw, targets, headless, max_parallel)


if __name__ == "__main__":
    asyncio.run(main())
