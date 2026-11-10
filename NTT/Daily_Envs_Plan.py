"""
Decide QUE envs hace falta triggerear, leyendo el Daily_Envs.

Aqui no se triggerea nada ni se sabe de formularios de Jenkins: eso es de
QA_Envs.py (nttcw) y COM_Envs.py (nttcom). Lo unico que vive aqui es la
decision, para que los dos scripts decidan igual y no se separen con el tiempo.

El parseo del archivo y el chequeo de status son de Track_Deployments.
"""

import os
import re
from datetime import datetime

import Track_Deployments as td

# Horas que puede tener el ultimo build de HOY antes de que deje de "contar".
# Debajo de esto se asume que ya hubo intento hoy y no se retriggerea; arriba
# se pregunta, porque suele querer decir que ya hay patches nuevos.
# Se aceptan tambien los nombres viejos, por si alguno ya estaba puesto.
STALE_HOURS = float(
    os.environ.get("DAILY_ENVS_STALE_HOURS")
    or os.environ.get("QA_ENVS_STALE_HOURS")
    or os.environ.get("COM_ENVS_STALE_HOURS")
    or "6"
)

BUCKET_NAMES = ("trigger", "ask", "fresh", "running", "unknown")


def ask_yes(question: str) -> bool:
    return input(question).strip().lower() in ("y", "yes", "s", "si", "sí")


def hours_since(moment, now=None) -> float:
    return ((now or datetime.now()) - moment).total_seconds() / 3600.0


def is_snapshot(value: str) -> bool:
    return not value.lower().startswith("file://")


# Los snapshots terminan en _T<numero> y ese numero es el tenant:
# ..._nitro_snapshot_ci_20261103_T4  ->  QATENANT_4
TENANT_SUFFIX_RE = re.compile(r"_T(\d+)\s*$", re.IGNORECASE)


def tenant_from_snapshot(snapshot: str):
    match = TENANT_SUFFIX_RE.search((snapshot or "").strip())
    return match.group(1) if match else None


def classify(deployment, now):
    """Que hacer con un env que el Daily_Envs marca IN PROGRESS.

    Lo que decide es la FECHA del ultimo build real en Jenkins, no si paso o
    fallo: si ya hubo intento hoy, ese env ya esta atendido.
    """
    if deployment.get("status") == "IN_PROGRESS":
        return "running"

    started = deployment.get("started")
    if started is None:
        return "unknown"

    if not td.is_today(started, now):
        return "trigger"

    return "ask" if hours_since(started, now) >= STALE_HOURS else "fresh"


def payload_problem(deployment):
    """Razon por la que NO se puede confiar en el SD/snapshot, o None.

    El Daily_Envs se escribe a mano, asi que aqui se desconfia a proposito:
    mejor dejar un env sin triggerear que triggerearlo con la ruta equivocada.
    """
    patch = deployment.get("patch")
    if not patch:
        return "su bloque no tiene ningun SD file ni snapshot"

    # Si la ruta salio de su propio grupo de "Environment lists", es confiable
    # aunque el bloque tenga varias. Si se cayo al fallback, no.
    if deployment.get("patch_source") != "env_list":
        candidates = deployment.get("patch_candidates") or [patch]
        if len(candidates) > 1:
            return (
                f"su bloque tiene {len(candidates)} rutas distintas y el env no "
                f"aparece en ninguna lista de 'Environment lists'"
            )

    if not is_snapshot(patch) and not patch.lower().endswith((".yaml", ".yml")):
        return "el SD file no termina en .yaml (posible typo en el Daily_Envs)"

    return None


def read_in_progress(family):
    """Envs bajo IN PROGRESS del Daily_Envs, de una sola familia.

    family: "cw" o "com". Devuelve (deployments, cuantos se ignoraron).
    ON HOLD / PASSED / FAILED / BLOCKED ni se miran.
    """
    with open(td.FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    everything = td.extract_in_progress_deployments(text)
    want_com = family == "com"
    mine = [d for d in everything if td.is_com_url(d["url"]) == want_com]

    return mine, len(everything) - len(mine)


def split_buckets(deployments, now):
    """Reparte por decision y saca aparte los que traen SD dudoso."""
    buckets = {name: [] for name in BUCKET_NAMES}
    for deployment in deployments:
        buckets[classify(deployment, now)].append(deployment)

    blocked = []
    for name in ("trigger", "ask"):
        keep = []
        for deployment in buckets[name]:
            problem = payload_problem(deployment)
            if problem:
                deployment["problem"] = problem
                blocked.append(deployment)
            else:
                keep.append(deployment)
        buckets[name] = keep

    return buckets, blocked


# -------------------------
# REPORTE
# -------------------------
def describe(deployment, now):
    label = td.extract_env_label(deployment["url"])
    started = deployment.get("started")
    age = td.format_age(started, now) if started else "sin fecha"
    return f"{label:<9} {deployment.get('status', '?'):<12} {age}"


def print_group(title, deployments, now):
    if not deployments:
        return
    print(f"\n{title}")
    for deployment in deployments:
        print(f"   {describe(deployment, now)}")


def print_plan(buckets, blocked, now):
    print("\n" + "=" * 60)
    print_group("🔄 Corriendo ahora mismo — no se tocan:", buckets["running"], now)
    print_group(
        f"🟢 Ya se triggerearon hoy (menos de {STALE_HOURS:g}h) — no hace falta:",
        buckets["fresh"], now,
    )
    print_group(
        "❔ Sin fecha confiable — revisalos a mano, no se tocan:",
        buckets["unknown"], now,
    )
    print_group("🕸 Sin build de hoy — se van a triggerear:", buckets["trigger"], now)

    if blocked:
        print("\n⛔ Les tocaba, pero NO se triggerean (arregla el Daily_Envs):")
        for deployment in blocked:
            print(f"   {describe(deployment, now)}")
            print(f"      ↳ {deployment['problem']}")
            print(f"      ↳ {deployment.get('patch') or '(nada)'}")

    print("=" * 60)


def ask_about_stale(stale, now):
    """Los que si corrieron hoy pero ya llevan rato: se pregunta uno por uno."""
    chosen = []
    if not stale:
        return chosen

    print(f"\n🕗 Estos si corrieron hoy, pero hace {STALE_HOURS:g}h o mas.")
    print("   Suele querer decir que ya hay patches nuevos que instalar.")

    for deployment in stale:
        label = td.extract_env_label(deployment["url"])
        print(f"\n   {describe(deployment, now)}")
        print(f"   SD/snapshot: {deployment.get('patch')}")
        if ask_yes(f"   ¿Triggerear {label}? (y/n): "):
            chosen.append(deployment)

    return chosen


def confirm_plan(to_trigger):
    """Ultimo vistazo antes de tocar Jenkins. True si el usuario dijo que si."""
    print("\n" + "=" * 60)
    print(f"🚀 Listo para triggerear {len(to_trigger)} env(s):")
    for deployment in to_trigger:
        label = td.extract_env_label(deployment["url"])
        print(f"   {label:<9} → {deployment.get('patch')}")
    print("=" * 60)

    return ask_yes("\n¿Proceder? (y/n): ")
