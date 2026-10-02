"""
Triggerea deployments de envs nttcw (QA).

Por default va en FULL AUTO: lee el Daily_Envs, ve que envs bajo IN PROGRESS
les hace falta build, y los dispara sin preguntar nada.

    py QA_Envs.py          -> full auto, cero preguntas
    py QA_Envs.py --ask    -> automatico pero confirmando antes de disparar
    py QA_Envs.py --m      -> manual, te pregunta envs y SD como antes
    py QA_Envs.py --f      -> fuerza TODO lo que este en IN PROGRESS,
                               sin checar Jenkins (ni fecha, ni si ya corrio)

En full auto se disparan los que no tienen build de hoy, los que ya llevan
6h+ desde el de hoy, y los que no dieron fecha. NO se tocan los que estan
corriendo ahorita ni los que ya corrieron hace poco.

Con --f no se consulta Jenkins para nada: se dispara TODO lo que el Daily_Envs
marque como IN PROGRESS, este corriendo o no, sea de hoy o no. Es mas rapido
(no hay chequeo previo) pero tambien mas bruto -- puede reintentar algo que
ya esta corriendo.

Si un env trae un SD imposible (typo, o el bloque no dice cual), ese se salta
y se reporta al final: no hay forma de adivinarle sin arriesgar el patch. Esto
aplica siempre, incluso con --f.

Los envs COM no se tocan aqui: para eso esta COM_Envs.py.
"""

import asyncio
import os
import sys
from datetime import datetime

from playwright.async_api import async_playwright, BrowserContext, Page

from nc_credentials import LoginError, get_credentials

import Daily_Envs_Plan as plan
import Track_Deployments as td
from jenkins_web import JenkinsForm
from ntt_config import SNAPCI, SDCI, JENKINS_DEFAULT_USER, JENKINS_FOLDER

# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
USERNAME, PASSWORD = get_credentials("jenkins", default_username=JENKINS_DEFAULT_USER)

# -------------------------
# Config
# -------------------------
# Cuantos envs se configuran al mismo tiempo.  QA_ENVS_PARALLEL=1 -> secuencial.
MAX_PARALLEL = int(os.environ.get("QA_ENVS_PARALLEL", "6"))
# Por default corre sin ventana.  QA_ENVS_HEADLESS=0 para verlo.
HEADLESS = os.environ.get("QA_ENVS_HEADLESS", "1") != "0"

# En full auto no hay quien conteste el tenant. Si un env resulta ser de
# snapshot y esto esta vacio, ese env se salta en vez de adivinarle.
AUTO_TENANT = os.environ.get("QA_ENVS_TENANT", "").strip()

SD_HOST = SDCI
SNAPSHOT_HOST = SNAPCI
FOLDER_PATH = JENKINS_FOLDER

# El formulario de Jenkins es el mismo que el de COM_Envs.py; vive en
# jenkins_web.py. Aquí sólo cambia el prefijo del log.
_form = JenkinsForm("nttcw", USERNAME, PASSWORD)
log = _form.log
format_progress = _form.format_progress
wait_for_job_form = _form.wait_for_job_form
do_login = _form.do_login
click_checkbox_by_label = _form.click_checkbox_by_label
click_build = _form.click_build
get_editable_value_field = _form.get_editable_value_field
configure_sd_file = _form.configure_sd_file


def parse_args(argv):
    """--m (manual), --ask (automatico preguntando) y --f (fuerza sin Jenkins)."""
    manual = False
    ask = False
    force = False

    for arg in argv:
        key = arg.strip().lower()
        if key in ("--m", "-m", "--manual"):
            manual = True
        elif key in ("--ask", "-ask", "--confirm"):
            ask = True
        elif key in ("--f", "-f", "--force"):
            force = True
        else:
            print(f"⚠️ Flag desconocido, lo ignoro: {arg}")

    return manual, ask, force


# -------------------------
# Helpers
# -------------------------
def normalize_envs(txt: str):
    """Los nttcw si llevan cero de relleno: 6 -> 06 -> nttcw06_qa."""
    return [e.strip().zfill(2) for e in txt.split(",") if e.strip()]


def build_sdci_url(env: str) -> str:
    return f"{SD_HOST}{FOLDER_PATH}job/nttcw{env}_qa/job/Deploy-Parallel/build?delay=0sec"


def build_snapshot_url(env: str) -> str:
    return (
        f"{SNAPSHOT_HOST}{FOLDER_PATH}"
        f"job/nttcw{env}_qa/job/Restore-Eatlon-SnapShot/build?delay=0sec"
    )


async def fill_nitro_snapshot(page: Page, env: str, snapshot: str):
    container = page.locator('div.jenkins-form-item:has-text("NITRO_SNAPSHOT_NAME")')
    await container.wait_for(state="visible", timeout=150000)
    log(env, "Finding NITRO_SNAPSHOT_NAME input...")
    snapshot_input = await get_editable_value_field(container, env, "NITRO_SNAPSHOT_NAME")
    await _form.fill_and_verify(snapshot_input, env, "NITRO_SNAPSHOT_NAME", snapshot)


async def select_tenant(page: Page, env: str, tenant_number: str):
    tenant_value = f"QATENANT_{tenant_number}"
    container = page.locator('div.jenkins-form-item:has-text("OS_Tenant_Name")')
    await container.wait_for(state="visible", timeout=150000)
    await container.locator("select").select_option(label=tenant_value)
    log(env, f"✅ Tenant selected: {tenant_value}")


async def configure_snapshot(
    page: Page, snapshot: str, tenant: str, env: str, current_index: int, total_envs: int
):
    await click_checkbox_by_label(page, env, "Restore_Snapshot")
    await fill_nitro_snapshot(page, env, snapshot)
    await select_tenant(page, env, tenant)
    await click_build(page, env, current_index, total_envs)


async def run_env(
    context: BrowserContext,
    semaphore: asyncio.Semaphore,
    target: dict,
    current_index: int,
    total_envs: int,
):
    """Configura y dispara un env. Devuelve (env, ok, error)."""
    env = target["env"]

    async with semaphore:
        try:
            page = await context.new_page()
            url = (
                build_snapshot_url(env)
                if target["snapshot_mode"]
                else build_sdci_url(env)
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=1200000)
            await wait_for_job_form(page)

            if target["snapshot_mode"]:
                await configure_snapshot(
                    page, target["payload"], target["tenant"],
                    env, current_index, total_envs,
                )
            else:
                await configure_sd_file(
                    page, target["payload"], env, current_index, total_envs
                )

            return (env, True, None)
        except Exception as exc:
            log(env, f"❌ FALLO: {exc}")
            return (env, False, str(exc))


# -------------------------
# QUE TRIGGEREAR
# -------------------------
def manual_targets():
    """Modo --m: se pregunta todo, como antes."""
    envs = normalize_envs(input("\n🧩 Envs (comma separated): ").strip())
    if not envs:
        print("❌ No diste ningun env.")
        return []

    payload = input("\n📄 SD File OR Snapshot: ").strip()
    if not payload:
        print("❌ No diste SD file ni snapshot.")
        return []

    snapshot_mode = plan.is_snapshot(payload)
    tenant = input("🏢 Tenant (2 / 3 / 4): ").strip() if snapshot_mode else None

    return [
        {
            "env": env,
            "payload": payload,
            "snapshot_mode": snapshot_mode,
            "tenant": tenant,
        }
        for env in envs
    ]


def targets_from_deployments(to_trigger, interactive=True):
    """De cada deployment saca env + SD/snapshot.

    El tenant solo se pregunta en modo interactivo; en full auto sale de
    QA_ENVS_TENANT y, si no esta, ese env se salta.
    """
    targets = []
    skipped = []
    tenant_by_patch = {}

    for deployment in to_trigger:
        label = td.extract_env_label(deployment["url"])
        env = td.extract_env_number(deployment["url"])
        patch = deployment["patch"]

        if not env:
            skipped.append((label, "no pude sacar el numero de env de su link"))
            continue

        snapshot_mode = plan.is_snapshot(patch)
        tenant = None

        if snapshot_mode:
            if patch not in tenant_by_patch:
                if interactive:
                    envs_here = ", ".join(
                        td.extract_env_label(d["url"])
                        for d in to_trigger
                        if d["patch"] == patch
                    )
                    print(f"\n🏢 Snapshot: {patch}")
                    print(f"   Envs: {envs_here}")
                    tenant_by_patch[patch] = input("   Tenant (2 / 3 / 4): ").strip()
                else:
                    tenant_by_patch[patch] = AUTO_TENANT

            tenant = tenant_by_patch[patch]

            if not tenant:
                skipped.append(
                    (label, "es snapshot y no hay tenant (pon QA_ENVS_TENANT=2/3/4)")
                )
                continue

        targets.append(
            {
                "env": env,
                "payload": patch,
                "snapshot_mode": snapshot_mode,
                "tenant": tenant,
            }
        )

    if skipped:
        print(f"\n⚠️ {len(skipped)} env(s) se quedaron fuera:")
        for label, reason in skipped:
            print(f"   {label:<9} {reason}")

    return targets


def force_targets(confirm=False):
    """Modo --f: todo lo que este bajo IN PROGRESS, sin consultar Jenkins.

    Ni fecha, ni si ya esta corriendo, ni si ya se triggereo hoy: se toma tal
    cual lo dice el Daily_Envs. Lo unico que sigue filtrando es el SD/snapshot
    dudoso (typo, bloque ambiguo) -- eso no depende de Jenkins, depende del
    archivo, y triggerear con un SD roto no tiene arreglo despues.

    Devuelve la lista de targets, [] si no hay nada que hacer, o None si tu
    cancelaste a proposito con --ask (para no caer al modo manual).
    """
    print(f"📄 Leyendo {td.FILE_PATH}")

    deployments, ignored = plan.read_in_progress("cw")
    if ignored:
        print(f"ℹ️ {ignored} env(s) COM en IN PROGRESS: los ve COM_Envs.py, aqui se ignoran.")

    if not deployments:
        print("\n✅ No hay envs nttcw en IN PROGRESS.")
        return []

    ok = []
    blocked = []
    for deployment in deployments:
        problem = plan.payload_problem(deployment)
        if problem:
            deployment["problem"] = problem
            blocked.append(deployment)
        else:
            ok.append(deployment)

    if blocked:
        print(f"\n⛔ {len(blocked)} env(s) NO se triggerean (SD dudoso en el Daily_Envs):")
        for deployment in blocked:
            label = td.extract_env_label(deployment["url"])
            print(f"   {label:<9} ↳ {deployment['problem']}")
            print(f"      ↳ {deployment.get('patch') or '(nada)'}")

    if not ok:
        print("\n✅ Nada que triggerear.")
        return []

    print("\n" + "=" * 60)
    print(f"🚀 --f: se disparan {len(ok)} env(s) SIN checar Jenkins:")
    for deployment in ok:
        label = td.extract_env_label(deployment["url"])
        print(f"   {label:<9} → {deployment['patch']}")
    print("=" * 60)

    if confirm and not plan.confirm_plan(ok):
        print("🚫 Cancelado. No se triggereo nada.")
        return None

    return targets_from_deployments(ok, interactive=False)


async def auto_targets(pw, ask=False):
    """Modo automatico: sale todo del Daily_Envs + el estado real en Jenkins.

    Con ask=False (default) no pregunta nada: dispara todo lo que le haga
    falta build. Con ask=True confirma antes, como antes.

    Devuelve la lista de targets, [] si no hay nada que hacer, o None si tu
    cancelaste a proposito (para no volverte a preguntar por el modo manual).
    """
    print(f"📄 Leyendo {td.FILE_PATH}")

    deployments, ignored = plan.read_in_progress("cw")
    if ignored:
        print(f"ℹ️ {ignored} env(s) COM en IN PROGRESS: los ve COM_Envs.py, aqui se ignoran.")

    if not deployments:
        print("\n✅ No hay envs nttcw en IN PROGRESS.")
        return []

    print(f"\n⏳ Revisando en Jenkins el ultimo build de {len(deployments)} env(s)...\n")

    # El chequeo va sin ventana: no hay nada que mirar y es mas rapido.
    check_browser = await pw.chromium.launch(headless=True)
    check_context = await check_browser.new_context()
    try:
        await td.check_all_deployments(check_context, deployments)
    except LoginError as exc:
        print(exc)
        return None
    finally:
        await check_browser.close()

    now = datetime.now()
    buckets, blocked = plan.split_buckets(deployments, now)
    plan.print_plan(buckets, blocked, now)

    if ask:
        to_trigger = buckets["trigger"] + plan.ask_about_stale(buckets["ask"], now)
    else:
        # Full auto: entra todo lo que le haga falta build. Los "unknown" no
        # dieron fecha, pero un env IN PROGRESS sin build conocido casi
        # siempre es uno que nunca arranco, asi que tambien va.
        to_trigger = buckets["trigger"] + buckets["ask"] + buckets["unknown"]

        if buckets["ask"]:
            print(f"\n🕗 {len(buckets['ask'])} env(s) con 6h+ desde su build de hoy: van tambien.")
        if buckets["unknown"]:
            print(f"❔ {len(buckets['unknown'])} env(s) sin fecha: van tambien (a la buena de Dios).")

    if not to_trigger:
        print("\n✅ Nada que triggerear automaticamente.")
        return []

    if ask and not plan.confirm_plan(to_trigger):
        print("🚫 Cancelado. No se triggereo nada.")
        return None

    if not ask:
        print("\n" + "=" * 60)
        print(f"🚀 Full auto: se disparan {len(to_trigger)} env(s) sin preguntar.")
        for deployment in to_trigger:
            label = td.extract_env_label(deployment["url"])
            print(f"   {label:<9} → {deployment['patch']}")
        print("=" * 60)

    return targets_from_deployments(to_trigger, interactive=ask)


# -------------------------
# MAIN
# -------------------------
async def main():
    manual, ask, force = parse_args(sys.argv[1:])

    async with async_playwright() as pw:
        targets = []

        if force:
            targets = force_targets(confirm=ask)
        elif not manual:
            targets = await auto_targets(pw, ask=ask)

        if targets is None:
            # Login roto, o tu cancelaste a proposito. En ninguno de los
            # dos casos tiene caso ofrecerte el manual.
            print("\nNo se triggereo nada.")
            return

        # Caer al manual solo si hay quien conteste: en full auto (con o sin
        # --f) quedarse esperando un input seria justo lo que no quieres.
        if not targets and (manual or ask):
            targets = manual_targets()

        if not targets:
            print("\n✅ Nada que triggerear.")
            return

        total_envs = len(targets)
        parallel = min(MAX_PARALLEL, total_envs)
        print(f"\n⚡ {total_envs} env(s), hasta {parallel} en paralelo (headless={HEADLESS}).")
        print("   Envs: " + ", ".join(f"nttcw{t['env']}" for t in targets) + "\n")

        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()

        # Login una sola vez sobre el folder: la cookie queda en el context
        # y todas las pestanas paralelas la heredan.
        login_page = await context.new_page()
        await login_page.goto(
            f"{SD_HOST}{FOLDER_PATH}", wait_until="domcontentloaded", timeout=1200000
        )
        print("🔐 Logging in...")
        await do_login(login_page)
        print("✅ Logged in.\n")
        await login_page.close()

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
            + (", ".join(f"nttcw{e}" for e in ok) or "-")
        )
        if failed:
            print(f"❌ Fallaron ({len(failed)}):")
            for env, err in failed:
                print(f"   nttcw{env}: {err}")
        print("=" * 50)

        if HEADLESS:
            await browser.close()
            print("\n🎉 DONE.")
        else:
            print("\n🎉 DONE - browser left open.")
            await asyncio.sleep(9999999)


if __name__ == "__main__":
    asyncio.run(main())
