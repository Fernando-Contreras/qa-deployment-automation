"""
Deployment_Defect.py
====================

Reporta en Jira los deployments fallidos de tu Daily_Envs.txt.

Sin argumentos:

  1. Corre OrganizeResults para acomodar el archivo.
  2. Agarra todas las líneas bajo ❌ **FAILED**.
  3. Las que YA traen link de Jira al final, las deja en paz.
  4. Para las que no: resuelve el build fallido, baja el log, recorta el error,
     y crea el Defect con el formato de tus tickets.
  5. Le pega el link del defect a la línea del Daily_Envs:

     [**06QA**](...Deploy-Parallel/) (T2.3.49) - Check & Clean Up SHARED: https://jira.example.com/browse/QAPRJ-134305

  6. Corre Webex_Update para publicar el archivo ya final en la sala.

También sirve para un solo build, pasándole el link a mano.

Uso:
    py Deployment_Defect.py                        # el flujo del Daily_Envs
    py Deployment_Defect.py --dry-run              # enseña todo, no crea ni toca nada
    py Deployment_Defect.py --yes                  # sin preguntar
    py Deployment_Defect.py --show                 # con el browser visible
    py Deployment_Defect.py --priority Critical
    py Deployment_Defect.py --no-organize          # no correr OrganizeResults
    py Deployment_Defect.py --no-webex             # no publicar a Webex al final
    py Deployment_Defect.py https://sdci.../163/console    # un solo build

Credenciales: se reutilizan las de los demás scripts de NTT (nc_credentials, cifradas
con DPAPI). Primero busca unas guardadas como 'jira'; si no hay, usa las de 'jenkins'.
La sesión del browser se guarda en un perfil aparte, así que normalmente solo hay que
loguearse una vez.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.async_api import async_playwright

import ntt_config
import OrganizeResults
from jenkins_web import fetch_json_from_browser
from nc_credentials import LoginError, get_credentials

# -------------------------
# DAILY ENVS
# -------------------------
DAILY_ENVS = ntt_config.DAILY_ENVS_PATH

# [**06QA**](https://.../Deploy-Parallel/) (T2.3.49) - Check & Clean Up SHARED
ENV_LINE_RE = re.compile(r"^\[\*\*(?P<label>[^\]]+?)\*\*\]\((?P<url>[^)\s]+)\)\s*(?P<rest>.*)$")
# (T2.3.49) - Check & Clean Up SHARED
REST_RE = re.compile(r"^\((?P<version>[^)]*)\)\s*-\s*(?P<stage>.+?)\s*$")
JIRA_LINK_RE = re.compile(r"https://jira\.example\.com/browse/([A-Z][A-Z0-9]*-\d+)")

# -------------------------
# CONFIGURACIÓN DE JIRA
# -------------------------
JIRA_BASE = ntt_config.JIRA_BASE

# El filtro que abres normalmente; se usa como página de aterrizaje para validar sesión.
JIRA_FILTER_URL = (
    f"{JIRA_BASE}/issues/?filter=6058841&jql="
    "(%0A%20%20assignee%20%3D%20currentUser()%0A%20%20AND%20status%20NOT%20IN%20"
    "(Closed%2C%20Passed%2C%20Resolved%2C%20%22Passed%20With%20Minor%20Defects%22%2C%20"
    "Cancelled%2C%20Failed)%0A)%0AOR%0A(%0A%20%20status%20%3D%20%22Ready%20for%20Testing%22"
    "%0A%20%20AND%20reporter%20%3D%20currentUser()%0A)%0AORDER%20BY%20created%20DESC"
)

PROJECT_KEY = "QAPRJ"
ISSUE_TYPE = "Defect"

# IDs sacados del propio Jira (editmeta). Si algún día cambian, aquí se ajustan.
SECURITY_LEVEL_PRIVATE = "10400"
FOUND_IN_FIELD = "customfield_10014"   # "Found in"
FOUND_IN_BUILD = "10012"               # opción "Build"

COMPONENT_IDS = {
    "IT": "56823",
    "DevOps": "57230",
}

# Los de IT se dejan en automático (el lead del componente los agarra), pero los
# de DevOps van directo a esta persona. Se busca su usuario en Jira al vuelo,
# para no depender de un username escrito a mano aquí.
COMPONENT_ASSIGNEES = {
    "DevOps": "Jane Doe",
}

LABELS = ["installation_errors", "installation_failures"]

DEFAULT_PRIORITY = "Blocker"
VALID_PRIORITIES = ("Blocker", "Critical", "Major", "Normal", "Low")

# Jira corta la descripción; dejamos margen de sobra.
MAX_BLOCK_LINES = 120
MAX_BLOCK_CHARS = 14000

# Perfil del browser (para no re-loguear en cada corrida).
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NTT" / "defect_profile"


class NeedsHuman(Exception):
    """Algo pidió intervención manual (login raro, MFA, página inesperada)."""


class SessionProblem(NeedsHuman):
    """La sesión del browser no sirve. Afecta a TODOS los envs, no a uno solo,
    así que se relanza el browser visible en vez de marcar cada env como fallido."""


# -------------------------
# CREDENCIALES
# -------------------------
def jenkins_credentials():
    return get_credentials("jenkins", default_username=ntt_config.JENKINS_DEFAULT_USER)


def jira_credentials():
    """Usa unas credenciales 'jira' si existen; si no, las mismas de 'jenkins'."""
    try:
        return get_credentials("jira", prompt=False)
    except RuntimeError:
        return jenkins_credentials()


# -------------------------
# URL DEL BUILD
# -------------------------
def run_organize_results():
    """Acomoda el Daily_Envs igual que cuando corres OrganizeResults a mano.

    run_webex=False: esto corre ANTES de pegar los links de Jira a las
    líneas FAILED, así que el archivo todavía no queda en su estado final.
    Publicar a Webex aquí mostraría el build sin su link de Jira todavía.
    """
    print("🧹 Corriendo OrganizeResults...")
    try:
        OrganizeResults.main(run_webex=False)
    except Exception as exc:
        print(f"⚠ OrganizeResults falló ({exc}); sigo con el archivo como está.")


def parse_failed_entries(text: str):
    """
    Saca las líneas que están bajo ❌ **FAILED**.
    Regresa dicts con: line_no, label, url, version, stage, jira_key, raw.
    """
    entries = []
    current_status = None

    for line_no, raw_line in enumerate(text.splitlines()):
        stripped = raw_line.strip()

        # match_status_header y no el regex a pelo: el regex tiene dos formas
        # ("**FAILED**" y "FAILED:") y no distingue mayúsculas, así que
        # group(1) salía None o "failed" y esas líneas se ignoraban.
        status_match = OrganizeResults.match_status_header(stripped)
        if status_match:
            current_status = status_match[0]
            continue

        if current_status != "FAILED":
            continue

        match = ENV_LINE_RE.match(stripped)
        if not match:
            continue

        rest = match.group("rest").strip()
        jira_match = JIRA_LINK_RE.search(rest)

        # El link de Jira no es parte del stage; se recorta antes de leerlo.
        rest_no_jira = JIRA_LINK_RE.sub("", rest).rstrip().rstrip(":").rstrip()
        detail = REST_RE.match(rest_no_jira)

        entries.append(
            {
                "line_no": line_no,
                "label": match.group("label"),
                "job_url": match.group("url"),
                "version": detail.group("version") if detail else None,
                "stage": detail.group("stage") if detail else None,
                "jira_key": jira_match.group(1) if jira_match else None,
                "raw": raw_line.rstrip(),
            }
        )

    return entries


def append_jira_links(text: str, results):
    """
    Le pega ': <link>' a la línea de cada env que acabamos de reportar.

    OJO: NO usa el 'line_no' que traía cada entry. Ese número se capturó al
    arrancar, y para cuando esto corre ya pasaron varios minutos crawleando
    Jenkins — en ese rato Daily_Envs.txt lo pudo haber tocado otro proceso
    (OrganizeResults, Track_Deployments, Webex_Update...) y las líneas ya no
    están en el mismo lugar. Confiar en el índice viejo es justo lo que
    pegaba el link en la línea equivocada (p.ej. en el separador en blanco
    de arriba de "❌ FAILED:" en vez de en el env que realmente falló).

    En vez de eso, se vuelve a parsear el archivo TAL CUAL ESTÁ AHORA y se
    busca cada env por su firma (label + job url + version + stage), no por
    posición.
    """
    live_entries = parse_failed_entries(text)
    by_signature = {}
    for live in live_entries:
        sig = (live["label"], live["job_url"], live["version"], live["stage"])
        by_signature.setdefault(sig, []).append(live["line_no"])

    lines = text.splitlines()

    for entry, key in results:
        sig = (entry["label"], entry["job_url"], entry.get("version"), entry.get("stage"))
        candidates = by_signature.get(sig)

        if not candidates:
            print(
                f"⚠ Ya no encuentro la línea de {entry['label']} en el Daily_Envs actual "
                f"(¿alguien la movió mientras se creaba el ticket?); pégala a mano: "
                f"{JIRA_BASE}/browse/{key}"
            )
            continue

        if len(candidates) > 1:
            print(f"⚠ {entry['label']} aparece {len(candidates)} veces bajo FAILED; le pego el link a todas.")

        for idx in candidates:
            line = lines[idx].rstrip()
            if JIRA_LINK_RE.search(line):
                continue
            lines[idx] = f"{line.rstrip().rstrip(':')}: {JIRA_BASE}/browse/{key}"

    return "\n".join(lines) + "\n"


def backup_daily():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DAILY_ENVS.with_suffix(f".defects_{stamp}.bak")
    shutil.copyfile(DAILY_ENVS, backup)
    return backup


def strip_console(raw: str) -> str:
    """Quita /console, /consoleFull, etc. y deja la URL base."""
    url = raw.strip().strip('"').strip("'")

    if not url.lower().startswith("http"):
        raise ValueError(f"Eso no parece un link: {raw!r}")

    url = url.rstrip("/")

    for tail in ("/consoleFull", "/consoleText", "/console", "/pipeline-console", "/flowGraphTable"):
        if url.lower().endswith(tail.lower()):
            url = url[: -len(tail)]
            break

    return url.rstrip("/")


def has_build_number(url: str) -> bool:
    """.../Deploy-Parallel/163 sí; .../Deploy-Parallel/ no."""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    return last.isdigit() or last in ("lastBuild", "lastFailedBuild", "lastCompletedBuild")


def to_console_full(console_url: str) -> str:
    """/console -> /consoleFull (literalmente 'agregarle Full al final')."""
    return console_url + "Full" if console_url.endswith("/console") else console_url


def build_base_url(console_url: str) -> str:
    return re.sub(r"/console(Full|Text)?$", "", console_url)


def parse_build_url(console_url: str) -> dict:
    """Saca job, número de build y server del link."""
    parsed = urlparse(console_url)
    parts = [p for p in parsed.path.split("/") if p]

    jobs = [parts[i + 1] for i, p in enumerate(parts) if p == "job" and i + 1 < len(parts)]
    number = next((p for p in reversed(parts) if p.isdigit()), None)

    return {
        "server": parsed.netloc.split(".")[0],          # sdci / snapci
        "env_job": jobs[1] if len(jobs) > 1 else (jobs[0] if jobs else ""),  # nttcom2_qa
        "pipeline": jobs[-1] if jobs else "",           # Deploy-Parallel
        "build": number or "lastBuild",
    }


# -------------------------
# ETIQUETAS DEL SUMMARY
# -------------------------
def env_tag(env_job: str) -> str:
    """nttcom2_qa -> [2QA] | nttcw15_qa -> [15QA] | nttcom7_ci -> [7CI]"""
    match = re.search(r"ntt[a-z]*?(\d+)_([a-z0-9]+)", env_job, re.I)
    if match:
        return f"[{match.group(1)}{match.group(2).upper()}]"
    return f"[{env_job.upper()}]" if env_job else ""


def region_tags(text: str, env_job: str, stage: str = None) -> str:
    """
    [COM] / [EAST] / [WEST].

    Primero manda el stage que falló en Jenkins ("Product NRM East"): ese es el
    lado donde de verdad tronó. La ruta /u02/app/NTT<REGION>/ del log NO es
    confiable para esto, porque el installer log de un fallo East puede vivir
    bajo NTTWEST (y el título salía [WEST] con el fallo en East).
    Solo si el stage no nombra región se usan las rutas del log, y luego el job.
    """
    found = set(r.upper() for r in re.findall(r"\b(EAST|WEST|COM)\b", stage or "", re.I))

    if not found:
        found = set(r.upper() for r in re.findall(r"/app/NTT([A-Z]+)/", text, re.I))

    # Si el log no dice nada, al menos el nombre del job suele delatarlo.
    if not found and re.search(r"nttcom", env_job, re.I):
        found = {"COM"}

    order = ["COM", "EAST", "WEST"]
    ordered = [r for r in order if r in found] + sorted(found - set(order))
    return "".join(f"[{r}]" for r in ordered)


def guess_failed_item(text: str, meta: dict, stage: str = None) -> str:
    """Qué fue lo que tronó, en el lenguaje que usas en los tickets."""
    checks = [
        (r"nrm_custom|RB\.CST|rbm-release", "NRM Custom patches"),
        (r"Get installed version:\s*NRM|\bNRM-20\d\d", "NRM Product patch"),
        (r"CM_UI_CAAF|component '?CM'?|collections", "Collections patches"),
        (r"data-sync-tool", "Data Sync Tool"),
        (r"\bBRM\b|billing", "Billing patches"),
    ]
    for pattern, label in checks:
        if re.search(pattern, text, re.I):
            return label

    patch = meta.get("patch")
    if patch:
        return f"patch {patch}"

    # Si el log no delata un patch, la etapa del Daily_Envs describe bien el fallo
    # ("Start APPs COM", "Check & Clean Up SHARED").
    return stage or "deployment"


# -------------------------
# EXTRACCIÓN DEL ERROR
# -------------------------
FAILED_RECAP_RE = re.compile(r"failed=[1-9]\d*")

# Anclas de "arribita": el paso donde empezó a tronar.
STEP_ANCHOR_RE = re.compile(
    r"(pre|post)?install_step\d+|(\bstep\d+\s*\(\s*\d+\s*/\s*\d+\s*\))", re.I
)
END_ANCHOR_RE = re.compile(r"Log directory:|Total time:", re.I)
FATAL_RE = re.compile(r"fatal:\s*\[[^\]]+\]:\s*FAILED!|^\s*\S*\s*ERROR OCCURRED", re.I)


def _clean(lines):
    """Quita líneas vacías de más al inicio y al final."""
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _trim(lines):
    if len(lines) > MAX_BLOCK_LINES:
        lines = ["... (recortado) ..."] + lines[-MAX_BLOCK_LINES:]

    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = "... (recortado) ...\n" + block[-MAX_BLOCK_CHARS:]
    return block


def extract_error_block(log_text: str) -> tuple:
    """
    Regresa (bloque_de_error, cómo_lo_encontró).
    Va de lo más específico a lo más genérico.
    """
    lines = log_text.splitlines()

    # 1) INSTALLATION FAILED -> el caso típico del autoinstaller.
    hits = [i for i, l in enumerate(lines) if "INSTALLATION FAILED" in l.upper()]
    if hits:
        idx = hits[-1]

        start = max(0, idx - 12)
        for i in range(idx, max(0, idx - 60), -1):
            if STEP_ANCHOR_RE.search(lines[i]):
                start = i
                break

        end = min(len(lines) - 1, idx + 25)
        for i in range(idx, min(len(lines), idx + 60)):
            if END_ANCHOR_RE.search(lines[i]):
                end = i
        return _trim(_clean(lines[start : end + 1])), "INSTALLATION FAILED"

    # 2) Ansible: fatal: [host]: FAILED!
    hits = [i for i, l in enumerate(lines) if re.search(r"fatal:\s*\[[^\]]+\]:\s*FAILED!", l)]
    if hits:
        idx = hits[-1]
        end = min(len(lines) - 1, idx + 40)
        for i in range(idx + 1, min(len(lines), idx + 40)):
            if re.search(r"PLAY RECAP|^\s*\S*\s*TASK \[", lines[i]):
                end = i - 1
                break
        return _trim(_clean(lines[idx : end + 1])), "fatal: FAILED!"

    # 3) Cualquier ERROR marcado.
    hits = [i for i, l in enumerate(lines) if re.search(r"ERROR OCCURRED|\[ERROR\]|ERROR occurred in", l)]
    if hits:
        idx = hits[-1]
        start = max(0, idx - 15)
        end = min(len(lines) - 1, idx + 30)
        return _trim(_clean(lines[start : end + 1])), "ERROR"

    # 4) Último recurso: lo que había justo antes del recap de Ansible.
    hits = [i for i, l in enumerate(lines) if FAILED_RECAP_RE.search(l)]
    if hits:
        idx = hits[-1]
        return _trim(_clean(lines[max(0, idx - 60) : idx + 1])), "failed=N (recap)"

    return _trim(_clean(lines[-60:])), "cola del log (no se halló patrón)"


def extract_meta(block: str) -> dict:
    """Datos sueltos que le dan contexto al ticket."""
    def first(pattern, text=block, group=1):
        match = re.search(pattern, text, re.I | re.M)
        return match.group(group).strip() if match else None

    meta = {
        "host": first(r"fatal:\s*\[([^\]]+)\]"),
        "patch": (
            first(r"Error location:\s*patch\s+(\S+?),")
            or first(r"Installation of patch\s+(\S+?)\s+is failed")
            or first(r"components?\s+do not match.*?'\[?'?([\w.\-]+\.zip)")
        ),
        "step": first(r"Error location:.*?step\s*(\d+)") or first(r"ERROR occurred in (step\d+)"),
        "log_dir": first(r"Log directory:\s*(\S+)"),
    }

    # El "Error message:" viene en las líneas siguientes, ya sin el timestamp.
    match = re.search(r"Error message:\s*\n(.*?)(?:\n\s*\S*\s*(?:Total time:|Log directory:)|\Z)",
                      strip_timestamps(block), re.S | re.I)
    if match:
        message = " ".join(l.strip() for l in match.group(1).splitlines() if l.strip())
        meta["message"] = message[:600]

    return {k: v for k, v in meta.items() if v}


def strip_timestamps(text: str) -> str:
    """Quita el '14:02:14  ' que Jenkins le pega a cada línea."""
    return re.sub(r"^\d{2}:\d{2}:\d{2}\s{0,2}", "", text, flags=re.M)


def is_sql_problem(text: str) -> bool:
    return bool(
        re.search(
            r"Exception in SQL process|oracle_execute_sql|Errors found in SQL logs"
            r"|execute_sql|\bORA-\d{4,5}\b|PL/SQL",
            text,
            re.I,
        )
    )


# -------------------------
# AGRUPAR ENVS CON EL MISMO ERROR
# -------------------------
# Qué tanto se tienen que parecer dos logs para considerarlos el mismo problema
# (solo se usa de ÚLTIMO recurso, cuando no hay patch ni mensaje que comparar).
SIMILARITY_THRESHOLD = 0.92

# Idem, pero comparando ya nada más el mensaje de error (más corto y más limpio
# que el bloque completo, así que puede pedir menos parecido).
MESSAGE_SIMILARITY_THRESHOLD = 0.90


def error_signature(block: str) -> str:
    """
    El mismo error visto en dos envs distintos trae rutas, hosts y fechas distintas.
    Aquí se borra todo eso para poder comparar el error en sí.
    """
    text = strip_timestamps(block)
    text = re.sub(r"/app/NTT[A-Z]+/", "/corp/<ENV>/", text)
    text = re.sub(r"\d{4}\.\d{2}\.\d{2}_\d{2}-\d{2}-\d{2}", "<DATE>", text)
    text = re.sub(r"\[[a-z]{2,6}\d{1,3}\]", "[<HOST>]", text, flags=re.I)
    text = re.sub(r"\bnttc[a-z]*\d+_[a-z0-9]+\b", "<JOB>", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_patch(patch: str) -> str:
    """Para comparar patches sin que una coma o un punto de más los haga ver distintos."""
    return re.sub(r"[.,:;]+$", "", (patch or "").strip()).lower()


def normalize_message(message: str):
    """El 'Error message:' sin lo que cambia de un env a otro (host, env, IPs)."""
    if not message:
        return None
    text = message.lower()
    text = re.sub(r"/corp/ntt[a-z]+/", "/corp/<env>/", text)
    text = re.sub(r"\bnttc[a-z]*\d+_[a-z0-9]+\b", "<job>", text)
    text = re.sub(r"\[[a-z]{2,6}\d{1,3}\]", "[<host>]", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", text)
    return re.sub(r"\s+", " ", text).strip()


def same_problem(a: dict, b: dict) -> bool:
    """
    ¿Estos dos envs tienen el mismo problema, como para meterlos en un solo ticket?

    El release y la etapa PUEDEN diferir y aun así ser el mismo error (esos se van
    a la tabla del ticket). Lo que sí lo descarta es que el ticket tuviera que ir
    a otro equipo, o que sea otro patch (u otro paso de ese patch) el que truena.

    Orden de las pistas, de la más confiable a la menos:
      1. Mismo patch (y mismo paso, si ambos lo traen) -> mismo problema, punto.
         No se compara el resto del log: ese bloque trae dumps de Ansible, hosts
         y rutas propias de cada env que de por sí nunca van a ser 92% iguales
         aunque sea exactamente el mismo patch fallando en los tres.
      2. Sin patch (fallos de infra: disco lleno, servicio caído, etc.) -> se
         compara el 'Error message:' ya limpio de lo que cambia por env.
      3. Sin patch ni mensaje -> como último recurso, el bloque completo.
    """
    from difflib import SequenceMatcher

    if a["component"] != b["component"]:
        return False

    patch_a = normalize_patch(a["meta"].get("patch"))
    patch_b = normalize_patch(b["meta"].get("patch"))
    if patch_a and patch_b:
        if patch_a != patch_b:
            return False
        step_a, step_b = a["meta"].get("step"), b["meta"].get("step")
        if step_a and step_b and step_a != step_b:
            return False   # mismo patch, pero tronando en otro punto distinto
        return True

    msg_a = normalize_message(a["meta"].get("message"))
    msg_b = normalize_message(b["meta"].get("message"))
    if msg_a and msg_b:
        if msg_a == msg_b:
            return True
        return SequenceMatcher(None, msg_a, msg_b).ratio() >= MESSAGE_SIMILARITY_THRESHOLD

    ratio = SequenceMatcher(None, a["signature"], b["signature"]).ratio()
    return ratio >= SIMILARITY_THRESHOLD


def group_by_problem(pending):
    """
    pending: lista de (entry, info, fields_sin_armar).
    Regresa una lista de grupos; cada grupo es una lista de (entry, info).
    """
    groups = []

    for entry, info in pending:
        for group in groups:
            if same_problem(group[0][1], info):
                group.append((entry, info))
                break
        else:
            groups.append([(entry, info)])

    return groups


# -------------------------
# TEXTO DEL TICKET
# -------------------------
def merged_tags(infos) -> str:
    """[27QA][06QA][COM][WEST] — primero los envs, luego las regiones sin repetir."""
    tags = list(dict.fromkeys(i["env_tag"] for i in infos))
    tags.sort(key=lambda t: int(re.sub(r"\D", "", t) or 0))

    regions = []
    for info in infos:
        for region in re.findall(r"\[([A-Z]+)\]", info["region_tags"]):
            if region not in regions:
                regions.append(region)

    order = ["COM", "EAST", "WEST"]
    regions.sort(key=lambda r: order.index(r) if r in order else 99)

    return "".join(tags) + "".join(f"[{r}]" for r in regions)


def build_summary(infos) -> str:
    # Normalmente todos fallaron en lo mismo; si no, se nombran los dos.
    items = list(dict.fromkeys(i["failed_item"] for i in infos))
    return f"{merged_tags(infos)} | Deployment failed on {' / '.join(items)}"


def _shared(infos, getter):
    """El valor si es el mismo en todos los envs; None si cada quien trae el suyo."""
    values = {getter(i) for i in infos}
    return values.pop() if len(values) == 1 else None


def build_env_table(infos):
    """
    Tabla de Jira con un renglón por env. Solo salen las columnas que aportan:
    las que están vacías, o las que traen el mismo valor en todos (esas ya van arriba).
    """
    columns = [
        ("Environment", lambda i: i["env_job"], True),
        ("Release", lambda i: i.get("version"), False),
        ("Build", lambda i: f"#{i['build']}", True),
        ("Failed stage", lambda i: i.get("stage"), False),
        ("Host", lambda i: i["meta"].get("host"), False),
        ("Installer log", lambda i: i["meta"].get("log_dir"), False),
        ("Console", lambda i: f"[console|{i['console_url']}]", True),
    ]

    used = []
    for title, getter, always in columns:
        if always:
            used.append((title, getter))
            continue

        values = [getter(i) for i in infos]
        # Si nadie lo trae, o si todos traen lo mismo (ya está arriba), la columna sobra.
        if any(values) and len(set(values)) > 1:
            used.append((title, getter))

    header = "||" + "||".join(title for title, _ in used) + "||"
    rows = [
        "|" + "|".join(str(getter(info) or "-") for _, getter in used) + "|"
        for info in infos
    ]
    return [header, *rows]


def build_description(infos) -> str:
    main = infos[0]
    meta = main["meta"]
    several = len(infos) > 1

    lines = ["Hi team, could you please check?", ""]

    if several:
        envs = ", ".join(i["env_job"] for i in infos)
        lines += [f"The same failure is happening on {len(infos)} environments: {envs}.", ""]
    else:
        lines += [f"[{main['console_url']}]", ""]

    # Lo que es igual en todos va arriba una sola vez; lo que cambia va en la tabla.
    context = [
        ("Environment", None if several else main["env_job"]),
        ("Release", _shared(infos, lambda i: i.get("version"))),
        ("Job", _shared(infos, lambda i: f"{i['pipeline']} #{i['build']}") if not several
                else _shared(infos, lambda i: i["pipeline"])),
        ("Failed stage", _shared(infos, lambda i: i.get("stage"))),
        ("Host", _shared(infos, lambda i: i["meta"].get("host"))),
        ("Failed patch", _shared(infos, lambda i: i["meta"].get("patch"))),
        ("Failed step", _shared(infos, lambda i: i["meta"].get("step"))),
        ("Installer log", _shared(infos, lambda i: i["meta"].get("log_dir"))),
    ]
    for label, value in context:
        if value:
            lines.append(f"*{label}:* {value}")

    if several:
        lines += ["", *build_env_table(infos)]

    message = _shared(infos, lambda i: i["meta"].get("message")) or meta.get("message")
    if message:
        lines += ["", "*Error message:*", "{quote}", message, "{quote}"]

    label = f"*Console output* ({main['env_job']}):" if several else "*Console output:*"
    lines += ["", label, "{code:java}", main["block"], "{code}"]

    return "\n".join(lines)


# -------------------------
# BROWSER
# -------------------------
async def page_text(page) -> str:
    return await page.evaluate(
        """() => {
            const pre = document.querySelector('pre.console-output')
                     || document.querySelector('#out')
                     || document.querySelector('pre');
            return pre ? pre.innerText : document.body.innerText;
        }"""
    )


async def jenkins_login(page, username, password):
    """Login clásico de Jenkins (j_username / j_password)."""
    if await page.locator('input[name="j_username"]').count() == 0:
        return

    print("🔐 Jenkins pidió login...")
    await page.fill('input[name="j_username"]', username)
    await page.fill('input[name="j_password"]', password)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    if await page.locator('input[name="j_username"]').count() > 0 or "loginError" in page.url:
        raise LoginError("jenkins", "Jenkins no aceptó usuario/contraseña")

    print("✅ Login de Jenkins listo")


async def resolve_failed_build(page, job_url, username, password) -> str:
    """
    El Daily_Envs apunta al job (.../Deploy-Parallel/), no a un build.
    Aquí se decide cuál build es el que falló.
    """
    job_url = strip_console(job_url)

    if has_build_number(job_url):
        return f"{job_url}/console"

    # Hay que estar logueado antes de poder preguntarle a la API.
    await page.goto(job_url, timeout=180000)
    await page.wait_for_load_state("domcontentloaded")
    await jenkins_login(page, username, password)

    api = (
        f"{job_url}/api/json?tree=lastBuild[number,result,building],"
        f"lastFailedBuild[number],lastCompletedBuild[number,result]"
    )

    try:
        data = await fetch_json_from_browser(page, api)
    except Exception as exc:
        print(f"⚠ No pude preguntarle a la API de Jenkins ({exc}); uso lastFailedBuild.")
        return f"{job_url}/lastFailedBuild/console"

    last = data.get("lastBuild") or {}
    result = (last.get("result") or "").upper()

    # Si el último build es el que tronó, ese es el bueno.
    if not last.get("building") and result in ("FAILURE", "ABORTED", "UNSTABLE"):
        number = last.get("number")
        print(f"🎯 Build fallido: #{number} ({result})")
        return f"{job_url}/{number}/console"

    # Si no (ya lo relanzaron, o sigue corriendo), el último que sí falló.
    failed = (data.get("lastFailedBuild") or {}).get("number")
    if failed:
        note = "sigue corriendo" if last.get("building") else f"el último quedó {result or '?'}"
        print(f"🎯 Build fallido: #{failed} (ojo: {note})")
        return f"{job_url}/{failed}/console"

    raise NeedsHuman(f"En {job_url} no encontré ningún build fallido")


def has_error_signal(text: str) -> bool:
    return bool(FAILED_RECAP_RE.search(text)) or "INSTALLATION FAILED" in text.upper()


async def load_log(page, url, username, password, label) -> str:
    print(f"🌐 Abriendo {label}")
    await page.goto(url, timeout=300000)
    await page.wait_for_load_state("domcontentloaded")
    await jenkins_login(page, username, password)

    if await page.locator('input[name="j_username"]').count() > 0:
        raise SessionProblem("Jenkins sigue mostrando la pantalla de login")

    await asyncio.sleep(1)
    text = await page_text(page)
    print(f"📄 {label}: {len(text.splitlines())} líneas")
    return text


async def fetch_console(page, console_url, username, password) -> tuple:
    """
    Baja el log. Primero /console; si no aparece 'failed=N', se va a /consoleFull.
    Si el pipeline no expone el log en esas vistas, cae a los endpoints de texto plano.
    Regresa (texto, url_para_el_ticket).
    """
    base = build_base_url(console_url)

    # Orden: lo que pediste primero, y respaldos por si la vista nueva de pipeline no trae el log.
    candidates = [
        (console_url, "/console"),
        (to_console_full(console_url), "/consoleFull"),
        (f"{base}/timestamps/?time=HH:mm:ss&appendLog", "/timestamps (texto plano)"),
        (f"{base}/consoleText", "/consoleText"),
    ]

    best_text, best_url = "", console_url

    for url, label in candidates:
        try:
            text = await load_log(page, url, username, password, label)
        except NeedsHuman:
            raise
        except Exception as exc:
            print(f"⚠ {label} falló ({exc}); sigo con el siguiente.")
            continue

        if has_error_signal(text):
            # El ticket siempre apunta a una vista que un humano pueda abrir.
            ticket_url = url if label.startswith("/console") else console_url
            return text, ticket_url

        if len(text) > len(best_text):
            best_text, best_url = text, console_url

        print("🔎 Aquí no apareció 'failed=1'; sigo buscando...")

    if not best_text.strip():
        raise NeedsHuman("No pude bajar el log de este build")

    print("⚠ Nunca vi 'failed=1'; uso el log más completo que conseguí.")
    return best_text, best_url


async def jira_login(page, username, password):
    """Login de TMS. Si la página no es la de siempre, pide ayuda humana."""
    if await page.locator('input[name="os_username"], #login-form-username').count() == 0:
        return

    print("🔐 Jira pidió login...")
    user_field = page.locator('input[name="os_username"], #login-form-username').first
    pass_field = page.locator('input[name="os_password"], #login-form-password').first

    if await pass_field.count() == 0:
        raise SessionProblem("La pantalla de login de Jira no tiene el campo de contraseña esperado")

    await user_field.fill(username)
    await pass_field.fill(password)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    if await page.locator('input[name="os_password"], #login-form-password').count() > 0:
        raise SessionProblem("Jira no aceptó el login automático (¿MFA / SSO?)")

    print("✅ Login de Jira listo")


async def jira_request(page, method, path, payload=None):
    """Llama la REST API de Jira usando las cookies del browser."""
    result = await page.evaluate(
        """async ({method, url, body}) => {
            const options = {
                method,
                credentials: 'same-origin',
                headers: {'Accept': 'application/json', 'X-Atlassian-Token': 'no-check'},
            };
            if (body) {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body);
            }
            const response = await fetch(url, options);
            return {status: response.status, text: await response.text()};
        }""",
        {"method": method, "url": path, "body": payload},
    )

    data = None
    if result["text"]:
        try:
            data = json.loads(result["text"])
        except json.JSONDecodeError:
            data = None

    return result["status"], data, result["text"]


async def open_jira_session(page, username, password):
    print("🌐 Abriendo tu filtro de Jira...")
    await page.goto(JIRA_FILTER_URL, timeout=180000)
    await page.wait_for_load_state("domcontentloaded")
    await jira_login(page, username, password)

    status, data, _ = await jira_request(page, "GET", f"{JIRA_BASE}/rest/api/2/myself")
    if status != 200 or not data:
        raise SessionProblem(f"No pude confirmar la sesión de Jira (HTTP {status})")

    print(f"👤 Jira: {data.get('displayName')} ({data.get('name')})")
    return data


async def resolve_assignee(page, display_name):
    """
    Busca en Jira el usuario que se llama así y regresa su 'name' (el de login).
    Regresa None si no aparece, y entonces el ticket se queda en automático.
    """
    wanted = " ".join(display_name.split()).lower()

    # Jira busca por username, nombre y correo. El apellido solo suele bastar,
    # pero se prueban varias por si acaso.
    words = display_name.split()
    queries = [display_name, words[-1] if words else display_name, words[0] if words else ""]

    seen = set()
    for query in queries:
        query = query.strip()
        if not query or query.lower() in seen:
            continue
        seen.add(query.lower())

        url = f"{JIRA_BASE}/rest/api/2/user/search?username={quote(query)}&maxResults=50"
        status, data, _ = await jira_request(page, "GET", url)
        if status != 200 or not isinstance(data, list):
            continue

        for user in data:
            if " ".join((user.get("displayName") or "").split()).lower() == wanted:
                print(f"👤 Assignee: {user['displayName']} ({user['name']})")
                return user["name"]

    print(f"⚠ No encontré a '{display_name}' en Jira; ese ticket se va en automático.")
    return None


async def resolve_component_assignees(page, infos):
    """Resuelve una sola vez el assignee de cada component que lo tenga definido."""
    assignees = {}

    for component in {i["component"] for i in infos}:
        name = COMPONENT_ASSIGNEES.get(component)
        if name:
            assignees[component] = await resolve_assignee(page, name)

    return assignees


# -------------------------
# ¿YA ESTÁ REPORTADO?
# -------------------------
# Cuántos días hacia atrás se buscan tus defects. Uno abierto de hace meses
# probablemente ya es otra cosa, y es mejor preguntar que pegar un link viejo.
REPORTED_LOOKBACK_DAYS = 30

# Mismos estados que excluye tu filtro de Jira: un defect en alguno de estos ya
# no sirve para "colgarle" un fallo nuevo.
DONE_STATUSES = {
    "closed", "resolved", "passed", "passed with minor defects", "cancelled", "failed",
}

ENV_TAG_RE = re.compile(r"(\d+)\s*(QA|CI)", re.I)
SUMMARY_RE = re.compile(r"^\s*((?:\[[^\]]+\])+)\s*\|\s*(.+?)\s*$")
REGION_NAMES = ("COM", "EAST", "WEST")


def normalize_env_tag(tag: str):
    """'[06QA]' / '6qa' -> '6QA' (para que 06QA y 6QA cuenten como el mismo env)."""
    match = ENV_TAG_RE.fullmatch(tag.strip().strip("[]").strip())
    return f"{int(match.group(1))}{match.group(2).upper()}" if match else None


def parse_defect_summary(summary: str):
    """
    '[11QA][EAST][WEST] | Deployment failed on NRM Custom patches' ->
        {envs: {'11QA'}, regions: {'EAST','WEST'}, items: {'nrm custom patches'}}
    Regresa None si el summary no tiene el formato de estos tickets.
    """
    match = SUMMARY_RE.match(summary or "")
    if not match:
        return None

    tags = re.findall(r"\[([^\]]+)\]", match.group(1))
    failed = re.match(r"deployment failed on (.+)$", match.group(2), re.I)
    if not failed:
        return None

    return {
        "envs": {e for e in (normalize_env_tag(t) for t in tags) if e},
        "regions": {t.strip().upper() for t in tags if t.strip().upper() in REGION_NAMES},
        "items": {i.strip().lower() for i in failed.group(1).split(" / ") if i.strip()},
    }


def match_existing_defect(infos, issues):
    """
    ¿Alguno de tus defects abiertos ya cubre este grupo de envs?
    Tiene que cubrir TODOS los envs del grupo y la misma falla; y la región, si
    ambos la traen, tiene que empalmar al menos en una ([EAST] cubre [EAST][WEST]).
    issues viene del más nuevo al más viejo, así que gana el más reciente.
    """
    want_envs = {normalize_env_tag(i["env_tag"]) for i in infos}
    want_items = {i["failed_item"].strip().lower() for i in infos}
    want_regions = set(re.findall(r"\[([A-Z]+)\]", " ".join(i["region_tags"] for i in infos)))

    for issue in issues:
        if issue["status"].strip().lower() in DONE_STATUSES:
            continue

        parsed = parse_defect_summary(issue["summary"])
        if not parsed:
            continue
        if not want_envs <= parsed["envs"]:
            continue
        if not want_items <= parsed["items"]:
            continue
        if want_regions and parsed["regions"] and not (want_regions & parsed["regions"]):
            continue

        return issue

    return None


def related_open_defects(infos, issues):
    """Defects abiertos que tocan alguno de estos envs, para avisar aunque no sean el mismo."""
    want_envs = {normalize_env_tag(i["env_tag"]) for i in infos}
    related = []

    for issue in issues:
        if issue["status"].strip().lower() in DONE_STATUSES:
            continue
        parsed = parse_defect_summary(issue["summary"])
        if parsed and want_envs & parsed["envs"]:
            related.append(issue)

    return related


async def fetch_reported_defects(page):
    """
    Tus defects recientes (lo mismo que el filtro 'Reported by me' de Jira), del
    más nuevo al más viejo. Si la búsqueda falla regresa [] y el script cae al
    comportamiento de siempre: preguntar antes de crear.
    """
    jql = (
        f"project = {PROJECT_KEY} AND reporter = currentUser() "
        f"AND created >= -{REPORTED_LOOKBACK_DAYS}d ORDER BY created DESC"
    )
    url = f"{JIRA_BASE}/rest/api/2/search?jql={quote(jql)}&maxResults=200&fields=summary,status"

    status, data, _ = await jira_request(page, "GET", url)
    if status != 200 or not isinstance(data, dict):
        print(f"⚠ No pude revisar tus defects existentes (HTTP {status}); sigo preguntando como siempre.")
        return []

    return [
        {"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"]}
        for i in data.get("issues", [])
    ]


# Campos que se pueden sacrificar si este Jira no los acepta al crear.
# Si rechaza el assignee, más vale crear el ticket en automático que no crearlo.
OPTIONAL_FIELDS = ("security", "labels", "assignee")


async def _post_issue(page, fields):
    return await jira_request(page, "POST", f"{JIRA_BASE}/rest/api/2/issue", {"fields": fields})


def _rejected_fields(data):
    """Qué campos concretos rechazó Jira, según su propia respuesta."""
    if not isinstance(data, dict):
        return []
    return list((data.get("errors") or {}).keys())


async def create_defect(page, fields):
    fields = dict(fields)
    status, data, raw = await _post_issue(page, fields)

    # Si solo se quejó de un campo opcional, lo quitamos y volvemos a intentar.
    for _ in range(len(OPTIONAL_FIELDS)):
        if status in (200, 201):
            break

        bad = [f for f in _rejected_fields(data) if f in OPTIONAL_FIELDS and f in fields]
        if not bad:
            break

        for field in bad:
            print(f"⚠ Jira no aceptó '{field}' ({data['errors'][field]}); lo quito y reintento.")
            fields.pop(field)

        status, data, raw = await _post_issue(page, fields)

    if status not in (200, 201):
        message = ""
        if isinstance(data, dict):
            parts = list(data.get("errorMessages") or [])
            parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
            message = "\n   ".join(parts)
        raise RuntimeError(f"Jira contestó HTTP {status}:\n   {message or raw[:1200]}")

    return data["key"]


# -------------------------
# PREVIEW
# -------------------------
def print_preview(group, fields, number=None, total=1):
    infos = [info for _, info in group]
    line = "=" * 78
    title = f"DEFECT {number} DE {total}" if number and total > 1 else "DEFECT"

    print(f"\n{line}")
    print(f"🐞 {title} LISTO PARA CREAR"
          + (f"  (cubre {len(infos)} envs)" if len(infos) > 1 else ""))
    print(line)
    print(f"Project    : {PROJECT_KEY}")
    print(f"Type       : {ISSUE_TYPE}")
    print(f"Summary    : {fields['summary']}")
    print(f"Priority   : {fields['priority']['name']}")
    print(f"Component  : {infos[0]['component']}")
    print("Found in   : Build")
    print(f"Labels     : {', '.join(LABELS)}")
    print("Security   : Private")
    assignee = fields.get("assignee")
    print(f"Assignee   : {assignee['name'] if assignee else '(automático)'}")
    print(f"Envs       : {', '.join(entry['label'] for entry, _ in group)}")
    print(f"\n--- Description ---\n{fields['description']}")
    print(line)


def print_finding(info, source):
    line = "-" * 78
    print(f"\n{line}")
    print(f"🔍 ERROR ENCONTRADO ({source})")
    print(line)
    print(info["block"])
    print(line)

    meta = info["meta"]
    if meta.get("message"):
        print(f"\n💬 Mensaje: {meta['message']}")
    if meta.get("patch"):
        print(f"📦 Patch  : {meta['patch']}")
    if meta.get("host"):
        print(f"🖥  Host   : {meta['host']}")


# -------------------------
# ANALIZAR UN ENV
# -------------------------
async def analyze(page, entry, jenkins_user, jenkins_pass, args):
    """Del link del env al defect ya armado (sin crearlo todavía)."""
    label = entry["label"]
    print(f"\n{'─' * 78}")
    print(f"🔧 {label}" + (f" — {entry['stage']}" if entry.get("stage") else ""))
    print("─" * 78)

    console_url = await resolve_failed_build(page, entry["job_url"], jenkins_user, jenkins_pass)
    log_text, used_url = await fetch_console(page, console_url, jenkins_user, jenkins_pass)

    block, source = extract_error_block(log_text)
    if not block.strip():
        raise NeedsHuman(f"{label}: el log se bajó pero venía vacío")

    build_info = parse_build_url(used_url)
    meta = extract_meta(block)

    info = {
        "console_url": used_url,
        "env_job": build_info["env_job"],
        "pipeline": build_info["pipeline"],
        "build": build_info["build"],
        "env_tag": env_tag(build_info["env_job"]),
        "region_tags": region_tags(block or log_text, build_info["env_job"], entry.get("stage")),
        "block": block,
        "meta": meta,
        "stage": entry.get("stage"),
        "version": entry.get("version"),
        "label": label,
    }
    info["failed_item"] = guess_failed_item(block, meta, entry.get("stage"))
    info["component"] = "DevOps" if is_sql_problem(block) else "IT"
    info["signature"] = error_signature(block)

    print_finding(info, source)
    print(f"\n🏷  Component: {info['component']}"
          f"{' (el log apunta a SQL)' if info['component'] == 'DevOps' else ''}")

    return info


def build_fields(infos, args, assignees=None):
    component = infos[0]["component"]

    fields = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"name": ISSUE_TYPE},
        "summary": build_summary(infos),
        "description": build_description(infos),
        "priority": {"name": args.priority},
        "components": [{"id": COMPONENT_IDS[component]}],
        "labels": LABELS,
        "security": {"id": SECURITY_LEVEL_PRIVATE},
        FOUND_IN_FIELD: {"id": FOUND_IN_BUILD},
    }

    # Sin 'assignee' en el payload, Jira aplica su asignación automática.
    assignee = (assignees or {}).get(component)
    if assignee:
        fields["assignee"] = {"name": assignee}

    return fields


# -------------------------
# ELEGIR CUÁLES CREAR
# -------------------------
ANSWERS_ALL = {"", "y", "yes", "s", "si", "sí", "a", "all", "todos", "todo"}
ANSWERS_NONE = {"n", "no", "ninguno", "ninguna", "none", "0", "q"}


def parse_selection(answer: str, total: int):
    """
    Interpreta la respuesta del prompt. Regresa la lista de índices (base 0) a
    crear, o None si la respuesta no se entiende / se sale de rango.

      Enter, y, todos -> todos        n, ninguno -> ninguno ([])
      2               -> solo el 2    1,3 ó "1 3" -> el 1 y el 3
    """
    answer = answer.strip().lower()

    if answer in ANSWERS_ALL:
        return list(range(total))
    if answer in ANSWERS_NONE:
        return []

    parts = [p for p in re.split(r"[,\s]+", answer) if p]
    if not parts or not all(p.isdigit() for p in parts):
        return None

    numbers = [int(p) for p in parts]
    if any(n < 1 or n > total for n in numbers):
        return None

    return sorted(set(n - 1 for n in numbers))


def choose_defects(prepared, ask=input):
    """
    Deja escoger cuáles defects crear. Con un solo defect sigue siendo Y/n; con
    varios se puede elegir uno, algunos, todos o ninguno.
    Los que no se escojan no se tocan: su línea del Daily_Envs queda sin link y
    la siguiente corrida los vuelve a ofrecer.
    """
    total = len(prepared)

    if total == 1:
        prompt = "\n¿Lo creo? [Y/n]: "
    else:
        prompt = (f"\n¿Cuáles creo? [Enter = todos, 1-{total} para escoger "
                  f"(ej. 2 ó 1,3), n = ninguno]: ")

    while True:
        picked = parse_selection(ask(prompt), total)
        if picked is None:
            print(f"   No te entendí. Escribe un número del 1 al {total}, varios separados por coma, "
                  f"Enter para todos o n para ninguno.")
            continue

        chosen = [prepared[i] for i in picked]
        skipped = total - len(chosen)
        if chosen and skipped:
            print(f"\n👉 Creo {len(chosen)} de {total}. Los otros {skipped} quedan pendientes "
                  f"(no se les pega link; la próxima corrida los vuelve a ofrecer).")
        return chosen


# -------------------------
# FLUJO PRINCIPAL
# -------------------------
async def run(entries, args, headless, update_file):
    jenkins_user, jenkins_pass = jenkins_credentials()
    jira_user, jira_pass = jira_credentials()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--start-maximized"] if not headless else [],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # ---- 1. ANALIZAR TODOS ----
            pending, failures = [], []

            for entry in entries:
                try:
                    pending.append((entry, await analyze(page, entry, jenkins_user, jenkins_pass, args)))
                except (LoginError, SessionProblem):
                    # Es la sesión, no este env: que lo maneje quien pueda abrir el browser.
                    raise
                except NeedsHuman as exc:
                    print(f"⚠ {entry['label']}: {exc}")
                    failures.append((entry["label"], str(exc)))
                except Exception as exc:
                    print(f"⚠ {entry['label']}: no lo pude analizar ({exc})")
                    failures.append((entry["label"], str(exc)))

            if not pending:
                print("\n❌ No pude armar ningún ticket.")
                return [], failures

            # ---- 2. JUNTAR LOS QUE TIENEN EL MISMO ERROR ----
            groups = group_by_problem(pending)

            for group in groups:
                if len(group) > 1:
                    labels = ", ".join(entry["label"] for entry, _ in group)
                    print(f"\n🔗 Mismo error en {labels} → van en un solo ticket.")

            # ---- 3. SESIÓN DE JIRA ----
            await open_jira_session(page, jira_user, jira_pass)

            # ---- 3b. ¿YA ESTÁN REPORTADOS? ----
            # Si un defect tuyo abierto ya cubre el grupo, no se pregunta ni se crea
            # nada: solo se le pega su link a la línea del Daily_Envs.
            reported = await fetch_reported_defects(page)
            already, to_create = [], []

            for group in groups:
                infos = [i for _, i in group]
                existing = match_existing_defect(infos, reported)
                if existing:
                    already.append((group, existing))
                else:
                    to_create.append(group)

            linked = []
            for group, existing in already:
                labels = ", ".join(entry["label"] for entry, _ in group)
                print(f"\n🔁 {labels} ya está reportado en {existing['key']} [{existing['status']}]")
                print(f"   {existing['summary']}")
                print(f"   {JIRA_BASE}/browse/{existing['key']}  → no creo nada, solo pongo el link.")
                linked.extend((entry, existing["key"]) for entry, _ in group)

            for group in to_create:
                for issue in related_open_defects([i for _, i in group], reported):
                    labels = ", ".join(entry["label"] for entry, _ in group)
                    print(f"\n⚠ {labels}: hay un defect abierto de ese env que no es igual a este:")
                    print(f"   • {issue['key']} [{issue['status']}] {issue['summary']}  →  {JIRA_BASE}/browse/{issue['key']}")

            # ---- 4. PREVIEW ----
            assignees = await resolve_component_assignees(
                page, [i for group in to_create for _, i in group]
            )
            prepared = [
                (group, build_fields([i for _, i in group], args, assignees))
                for group in to_create
            ]

            for number, (group, fields) in enumerate(prepared, 1):
                print_preview(group, fields, number, len(prepared))

            if prepared:
                print(f"\n📋 Listos para crear: {len(prepared)} defect(s) "
                      f"para {sum(len(g) for g, _ in prepared)} env(s)")
                for number, (group, fields) in enumerate(prepared, 1):
                    print(f"   {number}. {fields['summary']}")

            if args.dry_run:
                print("\n🧪 --dry-run: aquí le paro, no creo nada ni toco el Daily_Envs.")
                return [], failures

            if prepared and not args.yes:
                prepared = choose_defects(prepared)
                if not prepared:
                    print("🚪 No se creó ningún defect nuevo.")

            # ---- 5. CREARLOS ----
            created = list(linked)
            for group, fields in prepared:
                labels = ", ".join(entry["label"] for entry, _ in group)
                print(f"\n🚀 Creando el de {labels}...")
                try:
                    key = await create_defect(page, fields)
                except Exception as exc:
                    print(f"❌ {labels}: {exc}")
                    failures.append((labels, str(exc)))
                    continue

                print(f"✅ {labels} → {key}  ({JIRA_BASE}/browse/{key})")
                # Todos los envs del grupo apuntan al mismo ticket en el Daily_Envs.
                created.extend((entry, key) for entry, _ in group)

            # ---- 5. ACTUALIZAR EL DAILY_ENVS ----
            if created and update_file:
                backup = backup_daily()
                text = OrganizeResults.normalize_newlines(DAILY_ENVS.read_text(encoding="utf-8"))
                DAILY_ENVS.write_text(append_jira_links(text, created), encoding="utf-8")
                print(f"\n📝 Daily_Envs actualizado (backup: {backup.name})")

            return created, failures

        finally:
            await context.close()


def collect_entries(args):
    """Decide sobre qué se va a trabajar: el Daily_Envs o el link que te pasaron."""
    if args.url:
        return [{
            "line_no": None,
            "label": args.url,
            "job_url": args.url,
            "version": None,
            "stage": None,
            "jira_key": None,
            "raw": args.url,
        }], False

    if not DAILY_ENVS.exists():
        raise RuntimeError(f"No encontré {DAILY_ENVS}")

    if not args.no_organize:
        run_organize_results()

    text = OrganizeResults.normalize_newlines(DAILY_ENVS.read_text(encoding="utf-8"))
    entries = parse_failed_entries(text)

    if not entries:
        print("🎉 No hay nada en FAILED. Nada que reportar.")
        return [], True

    already = [e for e in entries if e["jira_key"]]
    todo = [e for e in entries if not e["jira_key"]]

    print(f"\n❌ FAILED en el Daily_Envs: {len(entries)}")
    for entry in already:
        print(f"   ✔ {entry['label']} — ya tiene {entry['jira_key']}")
    for entry in todo:
        stage = f" — {entry['stage']}" if entry["stage"] else ""
        print(f"   • {entry['label']}{stage} — falta reportar")

    if not todo:
        print("\n🎉 Todos los FAILED ya tienen su ticket.")

    return todo, True


def trigger_webex_update(args, update_file):
    """
    Publica a Webex hasta el final, cuando el Daily_Envs ya quedó en su estado
    definitivo (reorganizado Y con los links de Jira pegados).

    Por eso el OrganizeResults del arranque corre con run_webex=False: si
    publicara ahí, el equipo vería los builds fallidos todavía sin su ticket.
    """
    if not update_file:
        return          # modo "un build suelto": no se tocó el Daily_Envs
    if args.dry_run:
        return          # dry-run no manda nada a la sala
    if args.no_webex:
        return

    OrganizeResults.run_webex_update()


async def main_async(args):
    try:
        entries, update_file = collect_entries(args)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1

    if not entries:
        # Aunque no haya nada que reportar, OrganizeResults ya pudo reacomodar
        # el archivo, y ese reacomodo también tiene que llegar a Webex.
        trigger_webex_update(args, update_file)
        return 0

    try:
        created, failures = await run(entries, args, headless=not args.show, update_file=update_file)
    except NeedsHuman as exc:
        if args.show:
            print(f"\n❌ {exc}")
            return 1

        print(f"\n🙋 Necesito que le eches una mano: {exc}")
        print("   Abro el browser; haz el login / lo que falte y yo sigo solo.")
        created, failures = await run(entries, args, headless=False, update_file=update_file)
    except LoginError as exc:
        print(exc)
        return 1

    print(f"\n{'=' * 78}")
    print("🏁 LISTO")
    for entry, key in created:
        print(f"   ✅ {entry['label']} → {JIRA_BASE}/browse/{key}")
    for label, reason in failures:
        print(f"   ❌ {label}: {reason}")
    print("=" * 78)

    trigger_webex_update(args, update_file)

    return 1 if failures and not created else 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reporta en Jira los deployments fallidos del Daily_Envs."
    )
    parser.add_argument("url", nargs="?", help="Link de un build suelto (en vez del Daily_Envs)")
    parser.add_argument("-y", "--yes", action="store_true", help="Crear sin preguntar")
    parser.add_argument("--dry-run", action="store_true", help="Solo enseñar; no crea ni toca el archivo")
    parser.add_argument("--show", action="store_true", help="Browser visible")
    parser.add_argument("--no-organize", action="store_true", help="No correr OrganizeResults antes")
    parser.add_argument("--no-webex", action="store_true", help="No publicar a Webex al final")
    parser.add_argument(
        "--priority",
        default=DEFAULT_PRIORITY,
        choices=VALID_PRIORITIES,
        help=f"Prioridad del defect (default: {DEFAULT_PRIORITY})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())) or 0)
