import asyncio
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import ntt_config
from jenkins_web import fetch_json_from_browser
from nc_credentials import LoginError, get_credentials

FILE_PATH = str(ntt_config.DAILY_ENVS_PATH)

# Las corridas automáticas (Task Scheduler) no tienen consola donde mirar,
# así que dejan rastro aquí.
SCHEDULED_LOG = ntt_config.TRACK_DEPLOYMENTS_LOG
SCHEDULED_LOG_MAX_MB = 5

# Credenciales cifradas (DPAPI). La primera vez las pide y las guarda.
USERNAME, PASSWORD = get_credentials("jenkins", default_username=ntt_config.JENKINS_DEFAULT_USER)

# Los scripts que se disparan desde aquí viven en esta misma carpeta.
HERE = Path(__file__).resolve().parent
SANITY_SCRIPT = str(HERE / "E2E_Flow1_Till_Billing.py")
DEPLOYMENT_DEFECT_SCRIPT = str(HERE / "Deployment_Defect.py")
SANITY_COM_SCRIPT = str(HERE / "Sanity_COM.py")

# Tarea programada (sólo a demanda) que corre Deployment_Defect.py en TU
# sesión de Windows, con ventana. Ver trigger_deployment_defect.
DEPLOYMENT_DEFECT_TASK = "Deployment Defect"

# Igual que arriba, pero para el sanity de cw: E2E siempre abre navegador
# real (lo necesita para SAML), así que también corre en tu sesión.
E2E_SANITY_TASK = "E2E Sanity"
PENDING_SANITY_ENVS_FILE = ntt_config.PENDING_SANITY_ENVS_FILE

# Encabezado real de sección, p.ej. "🔒 **ON HOLD** - T3 copy In Progress:".
# Sólo mira lo que está DENTRO de los **, así el texto extra después (que a
# veces menciona "In Progress" sin ser la sección de IN PROGRESS) no engaña
# al detector de sección.
SECTION_HEADER_RE = re.compile(
    r"^\s*(?:\S+\s+)?\*\*(PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*"
)


def detect_section(line):
    match = SECTION_HEADER_RE.match(line.strip())
    return match.group(1) if match else None


# "[**115QA**](url) (99CI) - Check & Clean Up EAST": ya trae la razón de la
# falla escrita. Esa línea YA quedó registrada como FAILED (aunque siga bajo
# IN PROGRESS hasta que corra OrganizeResults): no se vuelve a revisar ni se
# le agrega nada. Lo único que le puede caer después es el link de Jira.
FAILURE_SUFFIX_RE = re.compile(r"\]\([^)\s]+\)\s*\([^)]*\)\s+-\s+\S")


def has_failure_suffix(line):
    return bool(FAILURE_SUFFIX_RE.search(line))


# -------------------------
# ¿HAY ALGUIEN VIENDO ESTO?
# -------------------------
def is_interactive():
    """
    True sólo si hay una terminal de verdad. En Task Scheduler no la hay, y
    ahí input() NO se queda esperando: regresa vacío. Por eso el rebuild se
    saltaba y todo se marcaba como FAILED sin preguntar nada.
    Se puede forzar el modo automático con NTT_UNATTENDED=1 (para probar).
    """
    if os.environ.get("NTT_UNATTENDED") == "1":
        return False

    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def ask(prompt, default):
    """
    Pregunta si hay terminal. Si no la hay, contesta 'default' solo y lo
    deja escrito, para que en el log se vea qué decidió y por qué.
    """
    if is_interactive():
        return input(prompt).strip().lower()

    print(f"{prompt}{default}   ← automático (sin terminal)")
    return default


# -------------------------
# LIMPIEZA DE PROCESOS ZOMBIE
# -------------------------
# Un proceso que lleva esto sin gastar NADA de CPU ya no está trabajando:
# está colgado esperando un input que nadie va a escribir, o dormido a
# propósito. Se tumba.
IDLE_LIMIT_MINUTES = 15

# MailQAs/MailCOMs se saltan a propósito: mueven Excel y Outlook, y
# matarlos a medio camino puede dejarte el archivo corrupto.
CLEANUP_SKIP_SCRIPTS = (
    "MailQAs", "MailCOMs", "Deployment_Defect",
    "E2E_Flow1_Till_Billing", "Results_Sanity", "Rerun_Sanity",
)


def _run_powershell(script, timeout=60):
    """Regresa (número_final, líneas_extra) de lo que imprimió PowerShell."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    lines = [l.strip() for l in (result.stdout or "").strip().splitlines() if l.strip()]
    count = int(lines[-1]) if lines and lines[-1].lstrip("-").isdigit() else 0
    return count, lines[:-1] if lines else []


def kill_idle_processes():
    """
    Tumba procesos de Python de esta carpeta que llevan más de
    IDLE_LIMIT_MINUTES sin gastar CPU. Para no matar a uno que sí está
    trabajando, se mide su CPU dos veces con 2 segundos de diferencia: si no
    avanzó ni un tick, está colgado de verdad.

    No toca: este mismo proceso, su padre, ni los de CLEANUP_SKIP_SCRIPTS.
    """
    skip = " -and ".join(
        f"$_.CommandLine -notlike '*{name}*'" for name in CLEANUP_SKIP_SCRIPTS
    )

    script = f"""
$mine = @({os.getpid()}, {os.getppid()})
$limit = (Get-Date).AddMinutes(-{IDLE_LIMIT_MINUTES})

$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" |
    Where-Object {{
        $_.CreationDate -lt $limit -and
        $mine -notcontains $_.ProcessId -and
        $(if ($_.CommandLine) {{ ($_.CommandLine -like '*Corp\\NTT*') -and ({skip}) }} else {{ $true }})
    }}

if (@($targets).Count -eq 0) {{ 0; exit }}

# Muestra 1 de CPU, esperar, muestra 2. Si no se movió -> colgado.
# Se consulta TODO de un jalón: preguntar proceso por proceso a WMI tarda
# ~1s cada uno y con 20 procesos se pasaba del timeout.
$before = @{{}}
foreach ($p in $targets) {{ $before[[string]$p.ProcessId] = $p.UserModeTime + $p.KernelModeTime }}
Start-Sleep -Seconds 2

$after = @{{}}
foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'")) {{
    $after[[string]$p.ProcessId] = $p.UserModeTime + $p.KernelModeTime
}}

$condenados = @()
foreach ($p in $targets) {{
    $id = [string]$p.ProcessId
    if (-not $after.ContainsKey($id)) {{ continue }}          # ya murió solo
    if ($after[$id] -eq $before[$id]) {{ $condenados += $p.ProcessId }}
}}

foreach ($id in $condenados) {{ Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }}

# Confirmar cuáles murieron de verdad: matar entre sesiones distintas
# (escritorio vs Task Scheduler) da "Access denied" en silencio.
$vivos = (Get-Process -ErrorAction SilentlyContinue).Id
$stubborn = @($condenados | Where-Object {{ $vivos -contains $_ }}).Count
$killed = @($condenados).Count - $stubborn

if ($stubborn -gt 0) {{ Write-Output "STUBBORN:$stubborn" }}
$killed
"""
    try:
        killed, extra = _run_powershell(script)
        if killed:
            print(f"🧹 Tumbé {killed} proceso(s) colgado(s) (+{IDLE_LIMIT_MINUTES} min sin hacer nada).")

        stubborn = next((int(l.split(":")[1]) for l in extra if l.startswith("STUBBORN:")), 0)
        if stubborn:
            # Pasa cuando los corrió el Task Scheduler (sesión 0) y tú lanzas
            # esto desde tu escritorio (otra sesión): Windows no deja matarlos
            # entre sesiones sin permisos de admin.
            print(f"⚠ {stubborn} proceso(s) colgado(s) NO se dejaron matar (otra sesión de Windows).")
            print("   Desde la tarea programada sí se pueden; a mano, necesitas PowerShell como administrador.")

        return killed
    except Exception as e:
        print(f"⚠ No pude revisar procesos colgados: {e}")
        return 0


def kill_orphan_chromiums():
    """
    Mata los Chromium (y el driver node) de Playwright cuyo proceso padre ya
    murió: son basura pura, nadie los puede controlar ya y se quedan comiendo
    RAM. NO toca tu Chrome normal (otra ruta) ni sesiones vivas (padre vivo).
    """
    script = r"""
$alive = (Get-Process -ErrorAction SilentlyContinue).Id
$procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='node.exe'" |
    Where-Object { $_.ExecutablePath -like '*ms-playwright*' -or $_.ExecutablePath -like '*playwright\driver*' }
$dead = $procs | Where-Object { $alive -notcontains $_.ParentProcessId }
foreach ($p in $dead) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
@($dead).Count
"""
    try:
        killed, _ = _run_powershell(script, timeout=30)
        if killed:
            print(f"🧹 Cerré {killed} navegador(es) huérfano(s) de corridas anteriores.")
        return killed
    except Exception as e:
        print(f"⚠ No pude revisar Chromium huérfanos: {e}")
        return 0


def cleanup_stale_processes():
    """Primero los procesos colgados; luego los navegadores que dejaron sueltos."""
    kill_idle_processes()
    kill_orphan_chromiums()


# Cuántos envs se revisan al mismo tiempo.
CONCURRENCY = 8


# -------------------------
# STATUS ICONS
# -------------------------
STATUS_ICONS = {
    "IN_PROGRESS": "🔄",
    "PASSED": "✅",
    "FAILED": "❌",
    "ABORTED": "🚫",
    "UNKNOWN": "❔",
    "ERROR": "⚠",
}

# Un build abortado/cancelado casi siempre es error humano (se canceló porque ya
# estaba instalado), así que se reporta pero NO se toca: ni rebuild, ni Daily_Envs.
# Sólo lo de HOY entra a las listas finales.
ACTIONABLE_STATUSES = ("PASSED", "FAILED")

# Si un env ya falló esto o más veces HOY, ya no es un tropiezo — es bug real.
# Ese env en particular se salta el rebuild y se trata como "n" automático.
CHRONIC_FAILURE_THRESHOLD = 3

# Sólo cuentan para "es crónico" los builds que dispararon ellos: si alguien
# más lo intentó y falló, eso no confirma que el patch esté roto de verdad.
CHRONIC_FAILURE_DEPLOYERS = {"alex rivera", "sam ortega"}

# Excepción: si lo único que falló es esto, se rebuildea sin límite.
UNLIMITED_REBUILD_STAGES = ("CHECK & CLEAN UP SHARED",)


def is_unlimited_rebuild(deployment):
    patches = [p.strip().upper() for p in deployment.get("failed_patches") or []]
    return bool(patches) and all(p in UNLIMITED_REBUILD_STAGES for p in patches)

# Si el stage actual lleva atorado más de esto, se marca con ⚠ en vez de 🔄.
# Stop/Start APPs suelen ser rápidos, así que a ellos se les exige menos tiempo.
STUCK_STAGE_KEYWORDS = ("STOP APP", "START APP")
STUCK_THRESHOLD_STOP_START_MIN = 30
STUCK_THRESHOLD_OTHER_MIN = 120


def format_status(status, stuck=False):
    icon = "⚠" if stuck else STATUS_ICONS.get(status, "•")
    return f"{icon} {status.replace('_',' ')}"


# -------------------------
# FECHAS (último deployment)
# -------------------------
# Formato que usa Jenkins en la fila del build: "Aug 24, 2026, 2:59 PM"
JENKINS_DATE_FORMATS = ("%b %d, %Y, %I:%M %p", "%b %d, %Y %I:%M %p")


def parse_jenkins_date(text):
    text = " ".join((text or "").split())
    for fmt in JENKINS_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_datetime(timestamp_ms):
    if not timestamp_ms:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000)


def format_datetime(moment):
    """'Aug 24, 2026 2:59 PM' (igual que lo muestra Jenkins)."""
    if not moment:
        return "sin fecha"
    hour = moment.hour % 12 or 12
    return f"{moment:%b} {moment.day}, {moment.year} {hour}:{moment:%M} {moment:%p}"


def format_age(moment, now=None):
    """'hace 2 h 15 min', 'hace 3 días'..."""
    if not moment:
        return "?"

    seconds = ((now or datetime.now()) - moment).total_seconds()

    if seconds < 0:
        return "en el futuro"
    if seconds < 60:
        return "hace segundos"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"hace {minutes} min"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"hace {hours} h {minutes} min" if minutes else f"hace {hours} h"

    days = hours // 24
    return "hace 1 día" if days == 1 else f"hace {days} días"


def format_duration(duration_ms):
    """'1 hr 34 min' (igual que el 'Took ...' de Jenkins)."""
    if not duration_ms:
        return ""

    minutes = int(duration_ms // 60000)
    hours, minutes = divmod(minutes, 60)

    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    if minutes:
        return f"{minutes} min"
    return "<1 min"


def is_today(moment, now=None):
    return bool(moment) and moment.date() == (now or datetime.now()).date()


# -------------------------
# PARSE
# -------------------------
def extract_patch_reference(line):
    stripped = line.strip()
    if not stripped:
        return None

    file_match = re.search(r"(file://\S+)", stripped, re.IGNORECASE)
    if file_match:
        return file_match.group(1).rstrip(").,;")

    # Los links de Jenkins traen "Snapshot" en el nombre del job
    # (.../job/NTT-Create-Eatlon-Snapshot), asi que sin quitarlos el
    # heuristico de abajo confunde el link del env con el snapshot a
    # instalar y el bloque acaba con un monton de rutas fantasma.
    without_urls = re.sub(r"https?://\S+", " ", stripped, flags=re.IGNORECASE)

    snapshot_match = re.search(r"snapshot\s*:\s*(.+)", without_urls, re.IGNORECASE)
    if snapshot_match:
        return snapshot_match.group(1).strip()

    generic_snapshot = re.search(
        r"\b[\w.-]+(?:/[\w.-]+)*snapshot[\w./-]*\b",
        without_urls,
        re.IGNORECASE,
    )
    if generic_snapshot:
        return generic_snapshot.group(0)

    return None


# Lineas de "Environment lists": "- 88CI → 74, 81" / "- CI_23 SS → COM28, COM36".
ENV_GROUP_RE = re.compile(r"^\s*[-*+]\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$")
ENV_TOKEN_RE = re.compile(r"(COM)?0*(\d+)", re.IGNORECASE)


def env_key(prefix, number):
    """Identidad de un env. COM2 y 02 son distintos aunque compartan numero."""
    return ("COM" if prefix else "CW", int(number))


def env_key_from_url(url):
    match = re.search(r"nttc(w|om)(\d+)_qa", url)
    if not match:
        return None
    return env_key("COM" if match.group(1) == "om" else "", match.group(2))


def parse_env_group_payloads(lines):
    """Mapea cada env de 'Environment lists' con SU ruta.

    En el archivo la ruta va en la linea de ABAJO del grupo al que pertenece:

        - CI_23 SS → COM28, COM36, COM37, COM42
        nttcom23_ci_48981/..._snapshot_ci_20260904_T5
        - CI_23 → COM2, COM15, COM29
        file://...G3.1.1.yaml

    Sin esto, un bloque con varias rutas no se puede resolver: gana la ultima y
    los envs de los otros grupos se van con el SD equivocado. Un grupo al que
    todavia no le pegan su ruta simplemente no aparece en el mapa.
    """
    payload_by_env = {}
    pending = []

    for line in lines:
        group = ENV_GROUP_RE.match(line)
        if group:
            pending = [
                env_key(token.group(1), token.group(2))
                for token in ENV_TOKEN_RE.finditer(group.group(2))
            ]
            continue

        payload = extract_patch_reference(line)
        if payload and pending:
            for key in pending:
                payload_by_env.setdefault(key, payload)
            pending = []

    return payload_by_env


def extract_in_progress_deployments(text):
    deployments = []
    seen = set()

    current_block = []
    for line in text.splitlines():
        # Cualquier titulo de bloque corta, no solo los "**T2.3.49**". Antes solo
        # se cortaba en versiones T, asi que un bloque "**88CI — Build**" se
        # quedaba pegado al anterior y sus envs heredaban el SD del bloque de
        # arriba (o del de abajo, porque gana el ultimo file:// del bloque).
        if BLOCK_HEADER_RE.match(line.strip()) and current_block:
            deployments.extend(extract_in_progress_deployments_from_block(current_block, seen))
            current_block = []

        current_block.append(line)

    if current_block:
        deployments.extend(extract_in_progress_deployments_from_block(current_block, seen))

    return deployments


def extract_patch_version(block_header_line):
    match = re.match(r"^#*\s*\*\*(T[\d.]+)", block_header_line.strip())
    return match.group(1) if match else None


def extract_in_progress_deployments_from_block(lines, seen):
    patch = None
    # Todas las rutas del bloque, no solo la ultima: un bloque con SD y snapshot
    # a la vez es ambiguo, y quien vaya a triggerear necesita poder darse cuenta.
    patch_candidates = []
    for line in lines:
        patch_reference = extract_patch_reference(line)
        if patch_reference:
            patch = patch_reference
            if patch_reference not in patch_candidates:
                patch_candidates.append(patch_reference)

    version = extract_patch_version(lines[0]) if lines else None
    payload_by_env = parse_env_group_payloads(lines)

    deployments = []
    current_section = None

    for line in lines:
        section = detect_section(line)
        if section is not None:
            current_section = section
            continue

        # Si ya tiene razón de falla escrita, ya se resolvió: no se re-revisa
        # (antes cada corrida de 10 min la volvía a checar y le re-agregaba texto).
        if current_section == "IN PROGRESS" and not has_failure_suffix(line):
            for url in re.findall(r"https://[^\s\)]+", line):
                if url not in seen:
                    seen.add(url)

                    # Primero la ruta de SU grupo; si el env no sale en ninguna
                    # lista, se cae al viejo "la ultima del bloque" y se marca
                    # como tal para que quien triggeree desconfie.
                    own_patch = payload_by_env.get(env_key_from_url(url))
                    deployments.append({
                        "url": url,
                        "patch": own_patch or patch,
                        "patch_source": "env_list" if own_patch else "block",
                        "patch_candidates": patch_candidates,
                        "version": version,
                    })

    return deployments


def extract_env_number(url):
    match = re.search(r"nttc(w|om)(\d+)_qa", url)
    if not match:
        return None
    number = match.group(2)
    return number if match.group(1) == "om" else number.zfill(2)


def extract_env_prefix(url):
    match = re.search(r"nttc(w|om)(\d+)_qa", url)
    return "COM" if match and match.group(1) == "om" else ""


def is_com_url(url):
    return extract_env_prefix(url) == "COM"


def is_restore_snapshot_job(url):
    return "restore-eatlon-snapshot" in url.lower()


def all_com_envs_passed(text):
    """
    True si en el archivo (siempre hay un solo bloque COM) TODOS los envs
    COM están bajo PASSED — ninguno en IN PROGRESS/FAILED/ABORTED/ON HOLD/
    BLOCKED. Se lee el archivo completo (no sólo lo que Track_Deployments
    revisó hoy), así que también cuenta lo que ya estaba resuelto de antes.
    """
    current_section = None
    any_com_seen = False

    for line in text.splitlines():
        section = detect_section(line)
        if section is not None:
            current_section = section
            continue

        for url in re.findall(r"https://[^\s\)]+", line):
            if not is_com_url(url):
                continue
            any_com_seen = True
            if current_section != "PASSED":
                return False

    return any_com_seen


def extract_env_label(url):
    env = extract_env_number(url)
    prefix = extract_env_prefix(url)
    return f"{prefix}{env}QA" if env else "UNKNOWN"


# -------------------------
# LOGIN (REUSABLE)
# -------------------------
# Evita que varias pestañas intenten loguearse al mismo tiempo y se pisen.
_login_lock = asyncio.Lock()


async def ensure_logged_in(page):
    if await page.locator('input[name="j_username"]').count() == 0:
        return

    async with _login_lock:
        if await page.locator('input[name="j_username"]').count() == 0:
            return

        print("🔐 Logging in...")
        await page.fill('input[name="j_username"]', USERNAME)
        await page.fill('input[name="j_password"]', PASSWORD)
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        # Si el formulario sigue ahí, Jenkins no aceptó las credenciales.
        still_on_form = await page.locator('input[name="j_username"]').count() > 0
        if still_on_form or "loginError" in page.url:
            raise LoginError("jenkins", "Jenkins no aceptó usuario/contraseña")


# -------------------------
# STATUS CHECK
# -------------------------
async def get_status(page, url):
    try:
        await page.goto(url, timeout=60000)
        await page.wait_for_load_state("domcontentloaded")

        # 🔥 auto login if needed
        await ensure_logged_in(page)

        icon = page.locator("tr.build-row a.build-status-link").first

        if await icon.count() == 0:
            return "UNKNOWN"

        title = await icon.get_attribute("title") or ""
        t = title.lower()

        if "in progress" in t or "running" in t:
            return "IN_PROGRESS"
        elif "success" in t:
            return "PASSED"
        elif "abort" in t or "cancel" in t:
            return "ABORTED"
        elif "fail" in t or "error" in t:
            return "FAILED"
        else:
            return "UNKNOWN"

    except LoginError:
        raise
    except Exception:
        return "ERROR"


def failed_patches_from_stages(data):
    failed_statuses = {"FAILED", "FAILURE", "ABORTED"}
    return [
        stage.get("name") or "UNKNOWN"
        for stage in data.get("stages", [])
        if (stage.get("status") or "").upper() in failed_statuses
    ]


def is_stop_start_stage(stage_name):
    name = (stage_name or "").upper()
    return any(keyword in name for keyword in STUCK_STAGE_KEYWORDS)


def stuck_threshold_minutes(stage_name):
    return STUCK_THRESHOLD_STOP_START_MIN if is_stop_start_stage(stage_name) else STUCK_THRESHOLD_OTHER_MIN


def current_stage_info_from_stages(data, now=None):
    """
    El patch que se está instalando AHORITA (el stage 'IN_PROGRESS') y cuánto
    lleva corriendo. Regresa (nombre, minutos) — minutos es None si Jenkins no
    dio el startTimeMillis de ese stage.
    """
    running_statuses = {"IN_PROGRESS", "RUNNING", "PAUSED_PENDING_INPUT"}
    now = now or datetime.now()

    for stage in data.get("stages", []):
        status = (stage.get("status") or "").upper()
        if status not in running_statuses:
            continue

        name = stage.get("name") or "UNKNOWN"
        started = to_datetime(stage.get("startTimeMillis"))
        minutes = (now - started).total_seconds() / 60 if started else None
        return name, minutes

    return None, None


# Casillas del formulario de Jenkins (las mismas que marca QA_Envs).
CLEANUP_FS = "CLEANUP_FS"
STOP_APPS_BEFORE = "STOP_APPS_BEFORE"
START_APPS = "START_APPS"

def stages_from_data(data):
    """[(nombre, status), ...] de todos los stages del build."""
    return [
        (stage.get("name") or "UNKNOWN", (stage.get("status") or "").upper())
        for stage in data.get("stages", [])
    ]


def params_to_uncheck(stages):
    """
    Qué casillas hay que DESmarcar en el rebuild — SÓLO según qué fue lo que
    FALLÓ, no según qué ya corrió. Si Check & Clean Up o Stop APPs ya
    pasaron pero lo que de verdad falló fue otra cosa (un patch como "CM",
    "Product ECA WEST", etc.), esas casillas se QUEDAN marcadas: el rebuild
    las tiene que volver a correr, igual que todo lo demás.

      - lo que falló es Check & Clean Up -> sin CLEANUP_FS
      - lo que falló es Stop APPs        -> sin CLEANUP_FS ni STOP_APPS_BEFORE
      - lo que falló es cualquier otra cosa (incluyendo Start APPs o un
        patch) -> no se desmarca nada; todo se vuelve a intentar.
    """
    skip = set()
    fallidos = {"FAILED", "FAILURE", "ABORTED"}

    for name, status in stages:
        if status not in fallidos:
            continue

        upper = name.upper()
        if "CLEAN UP" in upper:
            skip.add(CLEANUP_FS)
        elif "STOP APPS" in upper:
            skip.update({CLEANUP_FS, STOP_APPS_BEFORE})

    return skip


def failed_signature(stages):
    """Qué stages fallaron, para poder comparar '¿falló en LO MISMO?'."""
    return frozenset(
        name.upper() for name, status in stages if status in {"FAILED", "FAILURE", "ABORTED"}
    )


async def fetch_stage_data(page, deploy_url):
    api_url = deploy_url.rstrip("/") + "/lastBuild/wfapi/describe"
    return await fetch_json_from_browser(page, api_url)


def build_deployer(build):
    """Quién disparó el build, según la causa que reporta Jenkins ('fired by X')."""
    for action in build.get("actions") or []:
        for cause in action.get("causes") or []:
            name = cause.get("userName") or cause.get("userId")
            if name:
                return name
    return None


def is_chronic_deployer(name):
    return (name or "").strip().lower() in CHRONIC_FAILURE_DEPLOYERS


async def count_todays_failures(page, job_url, signature=None):
    """
    Cuántos builds de HOY, disparados por Alex Rivera o Sam Ortega,
    fallaron EN LO MISMO que el actual.

    'Lo mismo' = exactamente los mismos stages fallidos. Si hoy falló 2 veces
    en Check & Clean Up y una en Start APPs, eso NO son 3 del mismo problema,
    así que no cuenta como bug real.

    Un fallo disparado por alguien más (QA probando el env, otro deployer) NO
    cuenta: sólo Alex o Sam pueden confirmar que el patch de verdad está
    roto. Si al ignorar esos ya no se llega al umbral, se reintenta el
    rebuild en vez de mandarlo derecho a Deployment_Defect.

    Sin 'signature' cuenta cualquier falla de hoy de ellos (comportamiento viejo).
    """
    api_url = (
        job_url.rstrip("/")
        + "/api/json?tree=builds[number,timestamp,result,actions[causes[userId,userName]]]{0,25}"
    )
    data = await fetch_json_from_browser(page, api_url)

    broken_results = {"FAILURE", "FAILED", "ABORTED"}
    now = datetime.now()
    base = job_url.rstrip("/")
    count = 0
    revisados = 0

    for build in data.get("builds", []):
        result = (build.get("result") or "").upper()
        if result not in broken_results:
            continue

        started = to_datetime(build.get("timestamp"))
        if not (started and is_today(started, now)):
            continue

        if not is_chronic_deployer(build_deployer(build)):
            continue

        if signature is None:
            count += 1
            continue

        # Tope de consultas: con 8 builds ya se sabe de sobra si son 3+ iguales.
        if revisados >= 8:
            break
        revisados += 1

        try:
            stages = await fetch_json_from_browser(
                page, f"{base}/{build.get('number')}/wfapi/describe"
            )
            if failed_signature(stages_from_data(stages)) == signature:
                count += 1
        except Exception:
            pass  # si no se puede leer ese build, simplemente no cuenta

    return count


async def get_failed_patches_via_api(page, deploy_url):
    """Como get_failed_patches, pero sin navegar: para cuando la pestaña ya
    está en el mismo origin y logueada (la sesión headless del chequeo)."""
    data = await fetch_stage_data(page, deploy_url)
    return failed_patches_from_stages(data)


async def get_failed_patches(page, deploy_url):
    base_url = deploy_url.rstrip("/")
    job_url = f"{base_url}/"

    # Se abre la página del JOB, no la del build. Ir a "lastBuild/" hace que
    # Jenkins redirija a ".../44/", y ahí se ve menos información.
    # Los datos del build se piden por API, sin movernos de esta página.
    await page.goto(job_url, timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await ensure_logged_in(page)

    return await get_failed_patches_via_api(page, deploy_url)


# -------------------------
# STATUS CHECK (RÁPIDO, VÍA API DE JENKINS)
# -------------------------
def status_from_api(data):
    if data.get("building"):
        return "IN_PROGRESS"

    result = (data.get("result") or "").upper()

    if result == "SUCCESS":
        return "PASSED"
    elif "ABORT" in result or "CANCEL" in result:
        return "ABORTED"
    elif "FAIL" in result or "ERROR" in result:
        return "FAILED"
    else:
        return "UNKNOWN"


async def get_build_info_via_api(page, url):
    """
    Status + fecha/hora + duración del último build, en una sola llamada.
    Si falló o se canceló, de una vez saca el patch responsable en esta misma
    pestaña ya logueada (headless) — así no hay que volver a loguear en la
    ventana visible sólo para saber la razón.
    """
    api_url = (
        url.rstrip("/")
        + "/lastBuild/api/json?tree=building,result,timestamp,duration,number"
    )
    data = await fetch_json_from_browser(page, api_url)
    status = status_from_api(data)

    info = {
        "status": status,
        "started": to_datetime(data.get("timestamp")),
        "duration_text": format_duration(data.get("duration")),
        "build": data.get("number"),
    }

    if status in ("FAILED", "ABORTED"):
        try:
            stage_data = await fetch_stage_data(page, url)
            stages = stages_from_data(stage_data)
            info["failed_patches"] = failed_patches_from_stages(stage_data)
            info["stages"] = stages
            # Qué casillas ya no hay que volver a marcar en el rebuild.
            info["skip_params"] = params_to_uncheck(stages)
        except Exception:
            pass  # se reintenta al abrir la pestaña visible

        if status == "FAILED":
            try:
                info["failures_today"] = await count_todays_failures(
                    page, url, signature=failed_signature(info.get("stages") or [])
                )
            except Exception:
                pass  # sin este dato, simplemente no se detecta como crónico
    elif status == "IN_PROGRESS":
        try:
            stage_data = await fetch_stage_data(page, url)
            current_stage, stage_minutes = current_stage_info_from_stages(stage_data)
            info["current_stage"] = current_stage
            info["stage_minutes"] = stage_minutes

            if stage_minutes is not None and current_stage:
                info["stuck"] = stage_minutes > stuck_threshold_minutes(current_stage)
        except Exception:
            pass  # no es crítico: sólo se pierde el detalle del patch actual

    return info


async def get_build_info_via_dom(page, url):
    """Respaldo: lee la fecha de la fila del build, tal cual la muestra Jenkins."""
    status = await get_status(page, url)
    started = None
    duration_text = ""

    try:
        link = page.locator("tr.build-row a.build-link").first
        if await link.count() > 0:
            # El <a> trae "Aug 24, 2026, 2:59 PM" y title="Took 1 hr 34 min".
            started = parse_jenkins_date((await link.inner_text()).splitlines()[0])
            title = await link.get_attribute("title") or ""
            duration_text = title.replace("Took", "").strip()
    except Exception:
        pass

    return {
        "status": status,
        "started": started,
        "duration_text": duration_text,
        "build": None,
    }


async def open_session_page(context, url):
    """Abre UNA pestaña por servidor y hace login ahí; las demás heredan la cookie."""
    page = await context.new_page()
    await page.goto(url, timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await ensure_logged_in(page)
    return page


async def check_status(context, session_page, url):
    if session_page is not None:
        try:
            return await get_build_info_via_api(session_page, url)
        except LoginError:
            raise
        except Exception as e:
            print(f"⚠ API falló en {extract_env_label(url)} ({e}); usando método lento.")

    # Método viejo: cargar la página completa y leer el ícono.
    page = await context.new_page()
    try:
        return await get_build_info_via_dom(page, url)
    finally:
        await page.close()


async def check_all_deployments(context, deployments):
    """Revisa todos los envs en paralelo. Regresa los status en el mismo orden."""
    by_origin = {}
    for deployment in deployments:
        by_origin.setdefault(urlparse(deployment["url"]).netloc, []).append(deployment)

    # Login una sola vez por servidor (sdci, snapci, ...).
    session_pages = {}
    for origin, items in by_origin.items():
        try:
            session_pages[origin] = await open_session_page(context, items[0]["url"])
        except LoginError:
            raise
        except Exception as e:
            print(f"⚠ No se pudo abrir sesión en {origin} ({e}); esos envs van por método lento.")
            session_pages[origin] = None

    semaphore = asyncio.Semaphore(CONCURRENCY)
    print_lock = asyncio.Lock()
    total = len(deployments)
    done = 0

    async def check(deployment):
        nonlocal done
        url = deployment["url"]
        session_page = session_pages[urlparse(url).netloc]

        async with semaphore:
            info = await check_status(context, session_page, url)

        deployment.update(info)

        async with print_lock:
            done += 1
            label = extract_env_label(url)
            stuck = info.get("stuck", False)
            stage = f" ({info['current_stage']})" if info.get("current_stage") else ""
            if stuck:
                stage += f" ⏱ atorado {int(info['stage_minutes'])} min"
            # Sin fecha/hora aquí: la tabla de "ÚLTIMO DEPLOYMENT POR ENV"
            # de más abajo ya trae ese detalle completo.
            print(f"[{done:>2} / {total}] {format_status(info['status'], stuck):16} → {label}{stage}")

        return info["status"]

    return await asyncio.gather(*(check(d) for d in deployments))


def print_deployments_by_date(deployments):
    """Todo lo revisado, del más reciente al más viejo."""
    if not deployments:
        return

    now = datetime.now()

    # Sin fecha se van hasta abajo.
    ordered = sorted(
        deployments,
        key=lambda d: (d.get("started") is not None, d.get("started") or datetime.min),
        reverse=True,
    )

    print("\n" + "=" * 78)
    print("📅 ÚLTIMO DEPLOYMENT POR ENV (más reciente primero)")
    print("=" * 78)

    for deployment in ordered:
        started = deployment.get("started")
        status = deployment.get("status", "UNKNOWN")
        stuck = deployment.get("stuck", False)
        icon = "⚠" if stuck else STATUS_ICONS.get(status, "•")
        label = extract_env_label(deployment["url"])
        version = deployment.get("version") or ""

        if started is None:
            marker = "❔"
        elif is_today(started, now):
            marker = "🟢 HOY"
        else:
            marker = "🕸 VIEJO"

        duration = deployment.get("duration_text") or ""
        if status == "IN_PROGRESS":
            current_stage = deployment.get("current_stage")
            if stuck:
                duration = f"⏱ {current_stage} ({int(deployment['stage_minutes'])} min)"
            elif current_stage:
                duration = f"→ {current_stage}"
            else:
                duration = "corriendo..."

        print(
            f"{icon} {label:<9} {version:<9} {format_datetime(started):<22} "
            f"{format_age(started, now):<16} {duration:<14} {marker}"
        )

    stale = [d for d in ordered if d.get("started") and not is_today(d["started"], now)]
    if stale:
        print(f"\n🕸 {len(stale)} env(s) sin deployment de hoy: "
              f"{', '.join(extract_env_label(d['url']) for d in stale)}")


def print_ignored_group(title, note, ignored, now=None):
    """Envs que se reportan pero no entran a PASSED ni a rebuild."""
    if not ignored:
        return

    now = now or datetime.now()

    print("\n" + "=" * 78)
    print(f"{title} ({len(ignored)})")
    print("=" * 78)

    for deployment in sorted(ignored, key=lambda d: d.get("started") or datetime.min):
        status = deployment.get("status", "UNKNOWN")
        icon = STATUS_ICONS.get(status, "•")
        label = extract_env_label(deployment["url"])
        started = deployment.get("started")

        print(
            f"{icon} {label:<9} {status:<12} {format_datetime(started):<22} "
            f"{format_age(started, now)}"
        )

    print(note)


def print_all_ignored(old, no_date, now=None):
    print_ignored_group(
        "🕸 IGNORADOS — no se deployearon hoy",
        "\nNo entran a PASSED ni se les hace rebuild.",
        old,
        now,
    )
    print_ignored_group(
        "❔ SIN FECHA — Jenkins no dio la fecha del último build",
        "\n⚠ Éstos revísalos a mano: no se puede saber si son de hoy.\n"
        "Por si acaso, no entran a PASSED ni a rebuild.",
        no_date,
        now,
    )


async def inspect_build(context, deployment):
    """
    Abre la pestaña del job (se deja abierta) para que la revises.
    Si el patch que falló ya se sacó antes (headless, durante el chequeo),
    no se vuelve a pedir — sólo se abre la pestaña.
    """
    url = deployment["url"]
    page = await context.new_page()

    if "failed_patches" in deployment:
        try:
            await page.goto(url.rstrip("/") + "/", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            await ensure_logged_in(page)
        except Exception as e:
            print(f"⚠ No se pudo abrir la pestaña de {extract_env_label(url)}: {e}")
        return

    try:
        deployment["failed_patches"] = await get_failed_patches(page, url)
    except Exception as e:
        deployment["failed_patches"] = []
        print(f"⚠ Could not detect failed patch for {extract_env_label(url)}: {e}")


# -------------------------
# REBUILD
# -------------------------
async def uncheck_param(page, label):
    """Desmarca una casilla del formulario de rebuild. Si ya está desmarcada
    la deja igual. Regresa True si quedó desmarcada."""
    container = page.locator(f'div.jenkins-form-item:has-text("{label}")').first

    if await container.count() == 0:
        print(f"   ⚠ No encontré la casilla {label}; se deja como esté.")
        return False

    await container.scroll_into_view_if_needed(timeout=10000)
    checkbox = container.locator('input[type="checkbox"]').first

    if await checkbox.count() == 0:
        return False

    if await checkbox.is_checked():
        try:
            await checkbox.uncheck(force=True, timeout=10000)
        except Exception:
            await container.locator("label").first.click(timeout=10000)

    return not await checkbox.is_checked()


async def trigger_rebuild(page, url, skip_params=()):
    try:
        await page.goto(url + "lastBuild/")
        await ensure_logged_in(page)

        await page.locator('a:has-text("Rebuild")').first.click()
        await page.wait_for_load_state("networkidle")

        # Lo que ya se hizo en el intento anterior no se repite.
        for label in (CLEANUP_FS, STOP_APPS_BEFORE, START_APPS):
            if label in skip_params:
                if await uncheck_param(page, label):
                    print(f"   ⏭ {label} desmarcado (ya se había hecho).")

        await page.get_by_role("button", name="Rebuild").click()

        detalle = f" (sin {', '.join(sorted(skip_params))})" if skip_params else ""
        print(f"🚀 Rebuild → {extract_env_label(url)}{detalle}")

    except Exception as e:
        print(f"⚠ Error rebuilding {url}: {e}")


# -------------------------
# DAILY ENVS UPDATE
# -------------------------
TENANT_ISSUE_REASON = "Posible tenant incorrecto (revisar OS_Tenant_Name) — no se hizo rebuild"


def build_failed_reason(deployment):
    if deployment.get("tenant_issue"):
        return TENANT_ISSUE_REASON

    failed_patches = deployment.get("failed_patches") or []

    if failed_patches:
        return ", ".join(failed_patches)

    # Sin stages fallidos: si el build fue cancelado, eso es la razón.
    return "ABORTED" if deployment.get("status") == "ABORTED" else "UNKNOWN"


def build_console_link(deployment):
    """Link directo al console log del build que falló (sólo para consola —
    Daily_Envs.txt NO lleva esto, para no romper el formato que lee
    Deployment_Defect.py)."""
    base = deployment["url"].rstrip("/")
    build = deployment.get("build")
    build_path = str(build) if build else "lastBuild"
    return f"{base}/{build_path}/console"


def append_failed_envs_to_file(failed_deployments):
    with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    updated = 0
    for deployment in failed_deployments:
        url = deployment["url"]
        reason = build_failed_reason(deployment)

        for i, line in enumerate(lines):
            if url in line:
                # Nunca se escribe dos veces: si ya trae texto, se deja igual.
                if not has_failure_suffix(line):
                    lines[i] = line.rstrip("\n") + f" - {reason}\n"
                    updated += 1
                break

    if updated:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)

    print(f"\n📝 {FILE_PATH} updated with {updated} failed env(s).")
    return updated


def trigger_deployment_defect():
    """
    Lanza Deployment_Defect.py en una nueva terminal, sin bloquear este script.
    Deployment_Defect ya sabe correr OrganizeResults y leer las líneas FAILED
    que acabamos de escribir en Daily_Envs.txt.

    SIEMPRE en terminal visible, porque te pregunta si reportas el Defect.
    Desde Task Scheduler eso tiene truco: la tarea corre en la "sesión 0" de
    Windows, y cualquier ventana que abra ahí es INVISIBLE para ti. Por eso,
    sin terminal, se dispara otra tarea ("Deployment Defect") configurada
    para correr en TU sesión: esa sí abre la ventana en tu escritorio.
    """
    if not is_interactive():
        try:
            result = subprocess.run(
                ["schtasks", "/run", "/tn", DEPLOYMENT_DEFECT_TASK],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                print(f"\n🐞 Deployment_Defect.py lanzado en tu escritorio (tarea '{DEPLOYMENT_DEFECT_TASK}').")
            else:
                print(f"\n⚠ No se pudo lanzar la tarea '{DEPLOYMENT_DEFECT_TASK}': {(result.stderr or result.stdout).strip()}")
                print(f"   Las líneas FAILED ya están escritas. Córrelo a mano: py {DEPLOYMENT_DEFECT_SCRIPT}")
        except Exception as e:
            print(f"\n⚠ No se pudo lanzar la tarea '{DEPLOYMENT_DEFECT_TASK}': {e}")
        return

    try:
        subprocess.Popen(
            ["py", DEPLOYMENT_DEFECT_SCRIPT],
            cwd=os.path.dirname(DEPLOYMENT_DEFECT_SCRIPT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print("\n🐞 Deployment_Defect.py lanzado en nueva terminal.")
    except Exception as e:
        print(f"\n⚠ No se pudo lanzar Deployment_Defect.py: {e}")
        print(f"   Córrelo a mano: py {DEPLOYMENT_DEFECT_SCRIPT}")


def trigger_sanity_com():
    """
    Lanza Sanity_COM.py en su propia terminal, sin bloquear este script.
    Ventana aparte (no comparte consola): si necesita pedir login o avisar
    algo, se mezclaba con los prompts de Track_Deployments (y/n de rebuild,
    sanity, etc.) — cada uno con su propia consola, sin pisarse.
    """
    try:
        subprocess.Popen(
            ["py", SANITY_COM_SCRIPT],
            cwd=os.path.dirname(SANITY_COM_SCRIPT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print("\n🚀 Sanity_COM.py lanzado en nueva terminal.")
    except Exception as e:
        print(f"\n⚠ No se pudo lanzar Sanity_COM.py: {e}")
        print(f"   Córrelo a mano: py {SANITY_COM_SCRIPT}")


def trigger_e2e_sanity(env_string):
    """
    Sin terminal: dispara la tarea 'E2E Sanity' (corre en TU sesión, con
    ventana — E2E siempre abre navegador real, lo necesita para SAML).
    schtasks /run no acepta argumentos dinámicos, así que los envs se le
    pasan por archivo; E2E_Flow1_Till_Billing.py ya sabe leerlo.

    Se ACUMULA en vez de sobrescribir: si la tarea ya está corriendo un
    batch anterior, estos envs no se pierden — se suman, y salen en la
    próxima corrida que sí alcance a lanzarse.
    """
    try:
        PENDING_SANITY_ENVS_FILE.parent.mkdir(parents=True, exist_ok=True)

        existing = ""
        if PENDING_SANITY_ENVS_FILE.exists():
            existing = PENDING_SANITY_ENVS_FILE.read_text(encoding="utf-8").strip()

        nuevos = [e.strip() for e in env_string.split(",") if e.strip()]
        previos = [e.strip() for e in existing.split(",") if e.strip()]
        combinados = sorted(set(previos) | set(nuevos), key=lambda e: int(e) if e.isdigit() else 0)
        merged = ", ".join(combinados)

        PENDING_SANITY_ENVS_FILE.write_text(merged, encoding="utf-8")

        result = subprocess.run(
            ["schtasks", "/run", "/tn", E2E_SANITY_TASK],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"\n🧪 E2E Sanity lanzado en tu escritorio (envs pendientes: {merged}).")
        else:
            msg = (result.stderr or result.stdout).strip()
            print(f"\n⚠ No se pudo lanzar '{E2E_SANITY_TASK}' ({msg}).")
            print(f"   Puede que ya esté corriendo — los envs quedaron en cola: {merged}")
    except Exception as e:
        print(f"\n⚠ No se pudo lanzar '{E2E_SANITY_TASK}': {e}")


# El encabezado de bloque puede venir como "**T2.3.49 — 99CI**" o con prefijo
# de markdown: "## **T2.3.49 — 99CI**".
BLOCK_HEADER_RE = re.compile(r"^#*\s*\*\*.+\*\*\s*$")
PASSED_HEADER = "✅ **PASSED**:"
# Donde empieza la "cola" de un bloque: listas de envs, ruta del SD y la
# leyenda de estados vacia del final del archivo.
TAIL_START_RE = re.compile(r"^\s*(?:environment\s+lists?\s*:|file://)", re.IGNORECASE)
ENV_LINK_RE = re.compile(r"^\s*\[\*{0,2}(?:COM)?\d+QA")
STATUS_LINE_RE = re.compile(
    r"^\s*\S*\s*\*\*(?:PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*[^\n]*:\s*$",
    re.IGNORECASE,
)


def drop_empty_status_sections(body):
    """Quita encabezados de estado que se quedaron sin ningun env debajo.

    Al mover a PASSED el ultimo env de una seccion, su encabezado quedaba
    huerfano. Antes lo limpiaba OrganizeResults, pero ese solo corre cuando
    hay deployments fallidos, asi que en una corrida donde todo pasa el
    encabezado vacio se quedaba en el archivo.

    Solo se aplica al cuerpo del bloque; la leyenda del final del archivo
    vive en la cola y no se toca.
    """
    result = []
    index = 0

    while index < len(body):
        line = body[index]

        if not STATUS_LINE_RE.match(line.strip()):
            result.append(line)
            index += 1
            continue

        has_envs = False
        scan = index + 1
        while scan < len(body):
            candidate = body[scan].strip()
            if STATUS_LINE_RE.match(candidate) or TAIL_START_RE.match(candidate):
                break
            if ENV_LINK_RE.match(body[scan]):
                has_envs = True
                break
            scan += 1

        if has_envs:
            result.append(line)
            index += 1
            continue

        # Encabezado vacio: se salta junto con las lineas en blanco que lo siguen.
        index += 1
        while index < len(body) and not body[index].strip():
            index += 1

    return result


def find_tail_start(body):
    """Indice donde termina el cuerpo del bloque y empieza su cola.

    Sin esto, el "✅ **PASSED**:" que se busca mas abajo puede encontrar el de
    la leyenda vacia al final del archivo (el ultimo bloque llega hasta EOF) e
    insertar ahi los envs, donde OrganizeResults los borraba.
    """
    for index, line in enumerate(body):
        if TAIL_START_RE.match(line.strip()):
            start = index
            while start > 0 and (not body[start - 1].strip() or body[start - 1].strip() == "---"):
                start -= 1
            return start
    return len(body)


def split_into_blocks(lines):
    header_indices = [i for i, l in enumerate(lines) if BLOCK_HEADER_RE.match(l.strip())]
    if not header_indices:
        return None

    prefix = lines[: header_indices[0]]
    blocks = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        blocks.append(lines[start:end])

    return prefix, blocks


def move_passed_lines_in_block(block_lines, passed_urls):
    header = block_lines[0]
    rest = block_lines[1:]

    sep_idx = 0
    if rest and rest[0].strip() == "---":
        sep_idx = 1
    body = rest[sep_idx:]

    # Separar el cuerpo real de la cola (Environment lists / file:// / leyenda).
    tail_start = find_tail_start(body)
    main, tail = body[:tail_start], body[tail_start:]

    moved = []
    remaining = []
    for line in main:
        if any(url in line for url in passed_urls):
            moved.append(line)
        else:
            remaining.append(line)

    # Rescatar envs que corridas anteriores hayan dejado tirados en la cola.
    clean_tail = []
    for line in tail:
        if ENV_LINK_RE.match(line) and any(url in line for url in passed_urls):
            moved.append(line)
        else:
            clean_tail.append(line)

    if moved:
        stripped_remaining = [l.strip() for l in remaining]
        if PASSED_HEADER in stripped_remaining:
            insert_at = stripped_remaining.index(PASSED_HEADER) + 1
            while insert_at < len(remaining) and remaining[insert_at].strip().startswith("[**"):
                insert_at += 1
            remaining[insert_at:insert_at] = moved
        else:
            blank = [""] if remaining and remaining[0].strip() != "" else []
            remaining = [PASSED_HEADER] + moved + blank + remaining

    # Se limpia siempre, no solo cuando hubo movimiento: asi tambien barre
    # encabezados vacios que hayan quedado de corridas anteriores.
    remaining = drop_empty_status_sections(remaining)

    new_block = [header]
    if sep_idx:
        new_block.append("---")
    new_block.extend(remaining)
    new_block.extend(clean_tail)
    return new_block


def mark_passed_envs_in_file(passed_urls):
    if not passed_urls:
        return

    with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    lines = text.splitlines()
    split = split_into_blocks(lines)
    if not split:
        print("⚠ No blocks found in file; skipping PASSED update.")
        return

    prefix, blocks = split
    new_blocks = [move_passed_lines_in_block(b, passed_urls) for b in blocks]

    new_lines = list(prefix)
    for b in new_blocks:
        new_lines.extend(b)

    new_text = "\n".join(new_lines).rstrip("\n") + "\n"
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)

    print(f"\n📝 {FILE_PATH} updated: moved {len(passed_urls)} env(s) to PASSED.")


# -------------------------
# MAIN
# -------------------------
async def main():
    with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    total_urls = re.findall(r"https://[^\s\)]+", text)
    total_unique = len(set(total_urls))

    deployments = extract_in_progress_deployments(text)
    urls = [deployment["url"] for deployment in deployments]

    if not urls:
        print("❌ No IN PROGRESS deployments found.")
        return

    print(f"\n⏳ Checking {len(urls)} / {total_unique} deployments...\n")

    async with async_playwright() as pw:
        check_browser = None
        visible_browser = None
        visible_context = None
        tabs_left_open = False

        try:
            # Revisar status (incluye el login) corre invisible: no hay razón
            # para que veas esa pestaña, y antes se confundía con un env fallido.
            check_browser = await pw.chromium.launch(headless=True)
            check_context = await check_browser.new_context()

            passed = []
            failed = []
            in_progress = []

            try:
                statuses = await check_all_deployments(check_context, deployments)
            except LoginError as e:
                print(e)
                print("\nNo se revisó nada ni se tocó Daily_Envs.txt.")
                return
            finally:
                await check_browser.close()
                check_browser = None

            # Segundo navegador: para revisar fallidos y disparar rebuilds.
            # CON terminal se abre con ventana (para que tú lo veas y se queda
            # abierto). SIN terminal va headless y se cierra al final — si no,
            # cada corrida automática dejaría un Chromium colgado invisible.
            async def get_visible_context():
                nonlocal visible_browser, visible_context
                if visible_context is None:
                    visible_browser = await pw.chromium.launch(
                        headless=not is_interactive()
                    )
                    visible_context = await visible_browser.new_context()
                return visible_context

            now = datetime.now()
            ignored_old = []
            ignored_aborted = []
            ignored_no_date = []
            tenant_issue_failed = []

            for deployment, status in zip(deployments, statuses):
                # Cancelado: se reporta y ya. Ni rebuild ni Daily_Envs.
                if status == "ABORTED":
                    ignored_aborted.append(deployment)
                    continue

                if status in ACTIONABLE_STATUSES:
                    started = deployment.get("started")

                    if started is None:
                        ignored_no_date.append(deployment)
                        continue

                    if not is_today(started, now):
                        ignored_old.append(deployment)
                        continue

                if status == "PASSED":
                    passed.append(deployment["url"])
                elif status == "FAILED":
                    # Restore-Eatlon-SnapShot no se rebuildea: si falla, casi
                    # siempre es porque el tenant (OS_Tenant_Name) está mal, y
                    # reintentar con el mismo tenant falla igual. Se avisa y
                    # se deja para que lo arreglen a mano.
                    if is_restore_snapshot_job(deployment["url"]):
                        deployment["tenant_issue"] = True
                        tenant_issue_failed.append(deployment)
                    else:
                        failed.append(deployment)
                elif status == "IN_PROGRESS":
                    in_progress.append(deployment["url"])

            print_all_ignored(ignored_old, ignored_no_date, now)

            if tenant_issue_failed:
                labels = ", ".join(extract_env_label(d["url"]) for d in tenant_issue_failed)
                print(f"\n🏢 TENANT INCORRECTO — {len(tenant_issue_failed)} env(s) ({labels})")
                print("   Restore-Eatlon-SnapShot falló; revisa OS_Tenant_Name a mano. No se rebuildea solo.")
                append_failed_envs_to_file(tenant_issue_failed)

            # Fallidos y cancelados: se abre su pestaña para revisarlos a mano.
            # Los cancelados sólo se muestran; no van a rebuild ni a Daily_Envs.txt.
            to_inspect = failed + ignored_aborted + tenant_issue_failed

            if to_inspect:
                # El patch que falló ya se sacó en la mayoría de los casos durante
                # el chequeo headless (misma sesión, sin re-login). Eso se imprime
                # de una vez, sin esperar a que abra la pestaña visible.
                def print_reason(deployment):
                    label = extract_env_label(deployment["url"])
                    icon = STATUS_ICONS.get(deployment.get("status"), "❌")
                    print(f"{icon} {label} → {build_failed_reason(deployment)}")
                    print(f"   🔗 {build_console_link(deployment)}")

                ya_se_sabe = [d for d in to_inspect if "failed_patches" in d]
                falta_revisar = [d for d in to_inspect if "failed_patches" not in d]

                for deployment in ya_se_sabe:
                    print_reason(deployment)

                if is_interactive():
                    print(f"\n👀 Abriendo {len(to_inspect)} build(s) para revisar (las pestañas se quedan abiertas)...")
                    visible_context = await get_visible_context()
                    await asyncio.gather(*(inspect_build(visible_context, d) for d in to_inspect))
                    tabs_left_open = True
                elif falta_revisar:
                    # Sin terminal no tiene caso abrir pestañas que nadie va a
                    # ver; sólo se consulta lo que falte y se cierra al final.
                    visible_context = await get_visible_context()
                    await asyncio.gather(*(inspect_build(visible_context, d) for d in falta_revisar))

                for deployment in falta_revisar:
                    print_reason(deployment)

            # -------------------------
            # CANCELADOS
            # -------------------------
            # Se pregunta con las pestañas ya abiertas, para poder revisarlos antes
            # de decidir: normalmente se cancelan porque el patch ya estaba puesto.
            aborted_to_passed = []

            if ignored_aborted:
                print_ignored_group(
                    "🚫 CANCELADOS (aborted)",
                    "\nNo se les hace rebuild.",
                    ignored_aborted,
                    now,
                )

                labels = ", ".join(extract_env_label(d["url"]) for d in ignored_aborted)
                # Sin terminal no se tocan: darlos por buenos es decisión tuya.
                answer = ask(
                    f"\n🚫 ¿Pasar a PASSED los cancelados ({labels})? (y/n): ", default="n"
                )

                if answer == "y":
                    aborted_to_passed = ignored_aborted
                    passed.extend(d["url"] for d in ignored_aborted)
                    print(f"✅ {len(aborted_to_passed)} cancelado(s) se tratarán como PASSED.")
                else:
                    print("🚫 Se quedan como están; no se tocan en Daily_Envs.txt.")

            # -------------------------
            # POR FECHA
            # -------------------------
            print_deployments_by_date(deployments)

            # -------------------------
            # SUMMARY
            # -------------------------
            print("\n" + "=" * 50)
            print("(sólo deployments de HOY)")
            print(
                f"✅ PASSED: {len(passed)}"
                + (f"  (incluye {len(aborted_to_passed)} cancelado(s))" if aborted_to_passed else "")
            )
            print(f"❌ FAILED: {len(failed)}")
            print(f"🔄 IN PROGRESS: {len(in_progress)}")

            stuck_in_progress = [
                d for d in deployments if d.get("status") == "IN_PROGRESS" and d.get("stuck")
            ]
            if stuck_in_progress:
                stuck_labels = ", ".join(extract_env_label(d["url"]) for d in stuck_in_progress)
                print(f"⚠ ATORADOS: {len(stuck_in_progress)} ({stuck_labels})")

            if ignored_old:
                print(f"🕸 IGNORADOS (no son de hoy): {len(ignored_old)}")
            if ignored_aborted and not aborted_to_passed:
                print(f"🚫 IGNORADOS (cancelados): {len(ignored_aborted)}")
            if ignored_no_date:
                print(f"❔ SIN FECHA (revisar a mano): {len(ignored_no_date)}")

            print("=" * 50)

            # -------------------------
            # ENV LIST
            # -------------------------
            # Los COM no van al sanity de E2E (necesitan otro tipo de sanity, aún
            # sin script). Si pasaron, se marcan PASSED directo en Daily_Envs.txt.
            com_passed = [url for url in passed if is_com_url(url)]
            cw_passed = [url for url in passed if not is_com_url(url)]

            if com_passed:
                com_labels = ", ".join(sorted(extract_env_label(u) for u in com_passed))
                print(f"\n📌 COM pasaron ({com_labels}): no van a E2E sanity, se marcan PASSED directo.")
                mark_passed_envs_in_file(com_passed)

                # Siempre hay un solo bloque COM: si con esto ya no queda ninguno
                # sin resolver, dispara su sanity en paralelo.
                with open(FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    updated_text = f.read()

                if all_com_envs_passed(updated_text):
                    print("🟢 Todos los COM están PASSED.")
                    trigger_sanity_com()
                else:
                    print("⏳ Aún hay COM sin resolver — no se dispara Sanity_COM.py todavía.")

            passed_envs = [
                extract_env_number(url)
                for url in cw_passed
                if extract_env_number(url)
            ]

            if passed_envs:
                env_string = ", ".join(sorted(passed_envs, key=int))
                print(f"\n📋 PASSED ENVS (para E2E):\n{env_string}")

            # -------------------------
            # REBUILD
            # -------------------------
            if failed:
                def is_chronic(d):
                    if is_unlimited_rebuild(d):
                        return False
                    return (d.get("failures_today") or 0) >= CHRONIC_FAILURE_THRESHOLD

                chronic_failed = [d for d in failed if is_chronic(d)]
                normal_failed = [d for d in failed if not is_chronic(d)]
                need_deployment_defect = False

                # Estos ya fallaron 3+ veces HOY: no es mala suerte, es bug real.
                # Se saltan el rebuild — solo ellos, no todo el grupo de failed.
                if chronic_failed:
                    for d in chronic_failed:
                        label = extract_env_label(d["url"])
                        veces = d.get("failures_today")
                        print(f"\n🔁 {label} ya falló {veces} veces hoy — no vale la pena rebuild, es bug real.")

                    if append_failed_envs_to_file(chronic_failed):
                        need_deployment_defect = True

                if normal_failed:
                    # Sin terminal: rebuild. Marcarlos como FAILED sin reintentar
                    # sólo debe pasar si ya van 3+ fallos hoy (eso es 'chronic',
                    # y ésos ni siquiera llegan aquí).
                    choice = ask("\n❌ Rebuild ALL failed? (y/n): ", default="y")

                    if choice == "y":
                        visible_context = await get_visible_context()

                        async def rebuild(deployment):
                            page = await visible_context.new_page()
                            await trigger_rebuild(
                                page,
                                deployment["url"],
                                skip_params=deployment.get("skip_params") or set(),
                            )

                        await asyncio.gather(*(rebuild(d) for d in normal_failed))

                        print("\n🔥 Rebuilds triggered.")
                    else:
                        if append_failed_envs_to_file(normal_failed):
                            need_deployment_defect = True

                if need_deployment_defect:
                    trigger_deployment_defect()

        finally:
            # Cierre garantizado, pase lo que pase (incluso si algo truena).
            # Lo único que se deja vivo es el navegador CON ventana que tú vas
            # a revisar; todo lo demás se cierra para no dejar Chromium
            # invisibles comiendo RAM.
            if check_browser:
                await check_browser.close()

            if visible_browser and not (is_interactive() and tabs_left_open):
                await visible_browser.close()
                visible_browser = None

            if visible_browser:
                print("\n👀 El navegador se quedó abierto con las pestañas por revisar.")

    if not passed_envs:
        print("\nNo hay envs cw en PASSED. Se omite el prompt de SANITY (E2E).")
        print("\n✅ DONE.")
        return

    # -------------------------
    # SANITY
    # -------------------------
    # El E2E abre ventana real siempre (la necesita para SAML) y al final se
    # queda dormido a propósito para no cerrar el navegador. Corrido tal
    # cual desde Task Scheduler (sesión 0) esa ventana sería invisible, así
    # que sin terminal no se lanza aquí: se manda a la tarea 'E2E Sanity',
    # que corre en TU sesión — mismo truco que Deployment_Defect.
    if not is_interactive():
        env_string = ", ".join(sorted(passed_envs, key=int)) if passed_envs else ""
        if env_string:
            trigger_e2e_sanity(env_string)
        print("\n🎉 DONE.")
        return

    sanity = ask("\n🧪 Run SANITY script? (y/n): ", default="n")

    if sanity == "y":
        env_string = ", ".join(sorted(passed_envs, key=int)) if passed_envs else ""

        # Los envs se le pasan directo al script, así ya no hay que pegarlos.
        command = ["py", "-3.14", SANITY_SCRIPT]

        if env_string:
            print(f"\n📋 ENVS → E2E: {env_string}")
            command.append(env_string)
        else:
            print("\n⚠ No ENVS to send")

        subprocess.run(command)
    else:
        # com_passed ya se marcó arriba, incondicional; aquí sólo faltan los cw.
        mark = ask(
            "\n✅ ¿Marcar estos envs cw como PASSED en el archivo? (y/n): ", default="n"
        )
        if mark == "y":
            mark_passed_envs_in_file(cw_passed)

    print("\n🎉 DONE.")


class _Tee:
    """Escribe en la consola y en el log al mismo tiempo."""

    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file

    def write(self, data):
        self.stream.write(data)
        self.log_file.write(data)

    def flush(self):
        self.stream.flush()
        self.log_file.flush()


def _rotate_log_if_big():
    try:
        if SCHEDULED_LOG.exists() and SCHEDULED_LOG.stat().st_size > SCHEDULED_LOG_MAX_MB * 1024 * 1024:
            SCHEDULED_LOG.replace(SCHEDULED_LOG.with_suffix(".log.old"))
    except Exception:
        pass


def run_scheduled():
    """Corrida automática: sin preguntas, y todo queda escrito en el log."""
    _rotate_log_if_big()
    SCHEDULED_LOG.parent.mkdir(parents=True, exist_ok=True)

    # El BOM va UNA sola vez, al crear el archivo: sirve para que PowerShell y
    # Notepad no lean los emojis como basura. En cada append sería un BOM
    # nuevo a media página, así que sólo se escribe si el archivo va vacío.
    nuevo = not SCHEDULED_LOG.exists() or SCHEDULED_LOG.stat().st_size == 0

    with open(SCHEDULED_LOG, "a", encoding="utf-8") as log_file:
        if nuevo:
            log_file.write("﻿")
        log_file.write(f"\n{'=' * 70}\n{datetime.now():%Y-%m-%d %H:%M:%S} — corrida automática\n{'=' * 70}\n")
        original_stdout = sys.stdout
        sys.stdout = _Tee(original_stdout, log_file)

        try:
            asyncio.run(main())
        except Exception:
            # Sin esto, un error en la corrida automática se perdía sin rastro.
            traceback.print_exc(file=log_file)
            raise
        finally:
            sys.stdout = original_stdout


if __name__ == "__main__":
    # Cada corrida (cada 10 min) barre lo que quedó colgado de las anteriores.
    cleanup_stale_processes()

    if is_interactive():
        asyncio.run(main())
    else:
        run_scheduled()
