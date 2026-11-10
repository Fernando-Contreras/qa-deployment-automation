import re
import ctypes
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from collections import defaultdict

try:
    import win32com.client as win32
except ImportError:
    win32 = None

import ntt_config

SDCI_URL = f"{ntt_config.SDCI}{ntt_config.JENKINS_FOLDER}job"
SNAPCI_URL = f"{ntt_config.SNAPCI}{ntt_config.JENKINS_FOLDER}job"
DAILY_ENVS_PATH = ntt_config.DAILY_ENVS_PATH
ORGANIZE_RESULTS_PATH = Path(__file__).resolve().parent / "OrganizeResults.py"

def version_key(v):
    try:
        nums = re.findall(r"\d+", v)
        return tuple(int(x) for x in nums)
    except:
        return (0,)

def add_env(envs_by_version, version, env, is_snapshot, seen_envs=None):
    current = envs_by_version[version].get(env)

    if seen_envs is not None and env in seen_envs and not is_snapshot:
        return

    if current is None or is_snapshot:
        envs_by_version[version][env] = is_snapshot

    if seen_envs is not None:
        seen_envs.add(env)

def format_env_label(env: int, is_ci_version: bool) -> str:
    if is_ci_version:
        return f"COM{env}QA"
    return f"{env:02d}QA"

def is_snapshot_line(line: str) -> bool:
    # "snap" a secas tambien cuenta (la gente abrevia), igual que "ss".
    return bool(re.search(r"\bsnap(?:shot)?\b|\bss\b", line, re.I))

def find_ci_tag(raw_text: str):
    # Admite espacio o guion bajo entre el numero y "CI": "88 CI", "88_CI", "88CI".
    m = re.search(r"\b(\d{1,3})[\s_]*ci\b", raw_text, re.I)
    if m:
        return f"{m.group(1)}CI"

    # Ej: "46QA ADM CI" o "46_qa+...+ADM+CI" (bass page)
    m = re.search(r"\b(\d{1,3})[\s_]*qa\b[^\n]{0,40}?\bCI\b", raw_text, re.I)
    if m:
        return f"{m.group(1)}CI"

    # Ej: "Latest 88 ADM CI SD" - el numero y "CI" ni siquiera estan pegados,
    # solo comparten la palabra "Latest" al frente.
    m = re.search(r"\bLatest\s+(\d{1,3})\b[^\n]{0,30}?\bCI\b", raw_text, re.I)
    if m:
        return f"{m.group(1)}CI"

    return None

def line_has_explicit_signal(line: str) -> bool:
    # La línea ya dice explícitamente si es snapshot o SD/yaml normal,
    # así que no debe depender del contexto global del mensaje.
    return bool(re.search(r"\bsnap(?:shot)?\b|\bss\b|\bsd\b|\byaml\b", line, re.I))

def build_webex_box(raw_text: str):
    # version -> { env_number: is_snapshot }
    envs_by_version = defaultdict(dict)
    seen_envs = set()

    # ========== MODO 1: Formato original (versión en paréntesis) ==========
    version_pattern = re.compile(r"\((T\d+(?:\.\d+)+)\)", re.I)
    env_pattern = re.compile(r"Nttcw(\d+)_qa", re.I)

    current_version = None

    for line in raw_text.splitlines():
        v_match = version_pattern.search(line)
        if v_match:
            current_version = v_match.group(1)

        e_match = env_pattern.search(line)
        if e_match and current_version:
            env = int(e_match.group(1))
            is_snapshot = is_snapshot_line(line)
            add_env(envs_by_version, current_version, env, is_snapshot, seen_envs)

    # ========== MODO 2: Formato nuevo (versión en SD File) ==========
    # Buscar versión en SD File Location
    sd_file_pattern = re.compile(r"SD File.*?T(\d+\.\d+\.\d+(?:\.\d+)?)", re.I | re.DOTALL)
    sd_match = sd_file_pattern.search(raw_text)
    
    if sd_match:
        sd_version = f"T{sd_match.group(1)}"
        
        # Buscar envs en formato XX_qa SOLAMENTE (más específico)
        # Debe tener _ antes de qa
        env_list_pattern = re.compile(r"\b(\d+)_qa\b", re.I)
        
        for match in env_list_pattern.finditer(raw_text):
            # Verificar que NO sea parte de nttcw o nttcom
            start_pos = match.start()
            if start_pos >= 5:
                prefix = raw_text[start_pos-5:start_pos].lower()
                if 'nttcw' in prefix or 'nttcom' in prefix:
                    continue  # Skip, ya lo detectó el modo 1
            
            env = int(match.group(1))
            # Solo agregar si no está ya en envs_by_version[sd_version]
            if env not in envs_by_version[sd_version]:
                add_env(envs_by_version, sd_version, env, False, seen_envs)

    # ========== MODO 3: Formato COM (CI builds) ==========
    # Buscar versión CI en el texto
    ci_pattern = re.compile(r"CI[_\s-]?(\d+)", re.I)
    ci_versions_in_text = [f"CI_{int(match.group(1)):02d}" for match in ci_pattern.finditer(raw_text)]
    default_ci_version = ci_versions_in_text[0] if len(set(ci_versions_in_text)) == 1 else None

    if ci_versions_in_text:
        # Buscar envs COM en formato NTTCOMXX_QA
        com_env_pattern = re.compile(r"(?:NTTCOM|COM)\s*(\d+)\s*[_-]?\s*QA", re.I)
        current_ci_version = default_ci_version

        for line in raw_text.splitlines():
            ci_match = ci_pattern.search(line)
            if ci_match:
                current_ci_version = f"CI_{int(ci_match.group(1)):02d}"

            matches = list(com_env_pattern.finditer(line))
            if not matches:
                continue

            if not current_ci_version:
                continue

            is_snapshot = is_snapshot_line(line)
            is_yaml = bool(re.search(r"yaml", line, re.I))

            for match in matches:
                env = int(match.group(1))
                add_env(
                    envs_by_version,
                    current_ci_version,
                    env,
                    is_snapshot and not is_yaml,
                    seen_envs,
                )

    # ========== MODO 4: Versión en texto plano (ej: "with T2.3.31") ==========
    # SOLO si no detectó nada en MODO 1
    if not envs_by_version:
        text_version_pattern = re.compile(r"(?:with|builds?)\s+\(?(T\d+\.\d+\.\d+(?:\.\d+)?)\)?", re.I)
        text_version_match = text_version_pattern.search(raw_text)
        
        if text_version_match:
            text_version = text_version_match.group(1)
            
            # Buscar SOLO envs en formato XX_qa con separador claro
            standalone_env_pattern = re.compile(r"\b(\d+)_?\s*qa\b", re.I)
            
            for match in standalone_env_pattern.finditer(raw_text):
                env = int(match.group(1))
                # Solo agregar si no está ya en otras versiones
                already_exists = any(env in envs for envs in envs_by_version.values())
                if not already_exists:
                    add_env(envs_by_version, text_version, env, False, seen_envs)

    # ========== MODO 5: CATCH-ALL (ULTRA ROBUSTO FIXED) ==========
    # Detecta:
    # 41_qa, 41qa, 41 qa, nttcw41_qa, etc

    catch_all_pattern = re.compile(
        r"(?:nttcw|nttcom|com)?\s*(\d{1,3})\s*[_-]?\s*qa\b",
        re.I
    )

    # Mensaje entero habla de snapshot (ej. tablas "Env / Snapshot of .46")
    # aunque la palabra no esté en la misma línea que cada env.
    # Solo aplica cuando NINGUNA línea con env trae su propia señal: si el mail
    # ya marca env por env (ej. "Nttcom30_qa Snapshot" junto a otros sin nada),
    # ese detalle manda y no se contagia a los demás.
    any_env_line_has_signal = any(
        line_has_explicit_signal(line)
        for line in raw_text.splitlines()
        if catch_all_pattern.search(line)
    )
    global_is_snapshot = is_snapshot_line(raw_text) and not any_env_line_has_signal

    # El número de un tag CI (ej. "88" en "88_CI", "46" en "46QA ADM CI" /
    # "46qa CI") es el nombre del grupo, nunca un env real, sin importar si al
    # final se usa como versión (fallback) o solo como referencia en el texto
    # mientras la versión real es un T-version. Solo cuenta si viene explícito
    # como Nttcw46_qa/Nttcom46_qa.
    ci_tag = find_ci_tag(raw_text)
    ci_tag_env = int(ci_tag[:-2]) if ci_tag else None

    # Detectar versión fallback (una sola vez, no por cada match)
    if envs_by_version:
        fallback_version = list(envs_by_version.keys())[0]
    else:
        v_match = re.search(r"T\d+\.\d+\.\d+(?:\.\d+)?", raw_text)
        if v_match:
            fallback_version = v_match.group(0)
        else:
            fallback_version = ci_tag if ci_tag else "UNKNOWN"

    # El catch-all es solo red de seguridad: los envs que los modos anteriores
    # ya ubicaron (con su propia versión) no se vuelven a agregar, si no se
    # duplicarían bajo la versión fallback.
    already_placed = {env for envs in envs_by_version.values() for env in envs}

    for line in raw_text.splitlines():
        if line_has_explicit_signal(line):
            is_snapshot = is_snapshot_line(line)
        else:
            is_snapshot = global_is_snapshot

        for match in catch_all_pattern.finditer(line):
            env = int(match.group(1))
            has_env_prefix = bool(re.match(r"(?:nttcw|nttcom|com)", match.group(0), re.I))

            if env == ci_tag_env and not has_env_prefix:
                # Es el número del grupo CI (ej. "46QA ADM CI"), no un env real.
                continue

            if env in already_placed:
                continue

            # Inicializar si no existe
            if fallback_version not in envs_by_version:
                envs_by_version[fallback_version] = {}

            add_env(envs_by_version, fallback_version, env, is_snapshot, seen_envs)

    # ========== VALIDACIÓN ==========
    if not envs_by_version:
        return "No environments detected (even after catch-all)."

    versions = sorted(envs_by_version.keys(), key=version_key, reverse=True)

    # ========== EXTRAER FECHA ==========
    date_match = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
        raw_text,
        re.I,
    )
    
    # También buscar fecha en el SD File (formato YYYYMMDD)
    if not date_match:
        sd_date_pattern = re.compile(r"-r(\d{4})(\d{2})(\d{2})\d+-T\d+\.\d+", re.I)
        sd_date_match = sd_date_pattern.search(raw_text)
        if sd_date_match:
            # Convertir YYYYMMDD a formato "DDMmmBuild"
            month_num = int(sd_date_match.group(2))
            day = sd_date_match.group(3)
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            month_str = months[month_num - 1]
            date_str = f"{day}{month_str}Build"
        else:
            date_str = "Build"
    else:
        date_str = f"{date_match.group(1)}{date_match.group(2)}Build"

    # Preferir el tag de CI (88CI, 99CI, etc.) sobre la fecha en el título,
    # salvo que la versión misma ya sea ese tag (ej. fallback "46CI").
    ci_tag = find_ci_tag(raw_text)

    def title_suffix(v):
        if ci_tag and v != ci_tag:
            return ci_tag
        return date_str

    title = " & ".join(f"{v} — {title_suffix(v)}" for v in versions)

    lines = [f"**{title}**", "---"]

    # ---------- MAIN LINKS ----------
    for version in versions:
        snapshots = []
        regular = []

        for env, is_snapshot in envs_by_version[version].items():
            if is_snapshot:
                snapshots.append(env)
            else:
                regular.append(env)

        # Determinar si es COM o regular
        is_ci_version = version.startswith("CI")

        for env in sorted(snapshots):
            if is_ci_version:
                # COM: NO padding con 0
                env_id = str(env)
                env_label = format_env_label(env, is_ci_version)
                url = (
                    f"{SNAPCI_URL}/nttcom{env_id}_qa/"
                    f"job/NTT-Create-Eatlon-Snapshot/"
                )
            else:
                # Regular: padding con 0
                env_id = f"{env:02d}"
                env_label = format_env_label(env, is_ci_version)
                url = (
                    f"{SNAPCI_URL}/nttcw{env_id}_qa/"
                    f"job/Restore-Eatlon-SnapShot/"
                )

            lines.append(f"[**{env_label}**]({url}) ({version} SS)")

        for env in sorted(regular):
            if is_ci_version:
                # COM: NO padding con 0
                env_id = str(env)
                env_label = format_env_label(env, is_ci_version)
                url = (
                    f"{SDCI_URL}/nttcom{env_id}_qa/"
                    f"job/Deploy-Parallel/"
                )
            else:
                # Regular: padding con 0
                env_id = f"{env:02d}"
                env_label = format_env_label(env, is_ci_version)
                url = (
                    f"{SDCI_URL}/nttcw{env_id}_qa/"
                    f"job/Deploy-Parallel/"
                )
            
            lines.append(f"[**{env_label}**]({url}) ({version})")

    # ---------- ENV LISTS ----------
    lines.append("\n---")
    lines.append("Environment lists:")

    for version in versions:
        is_ci_version = version.startswith("CI")
        
        snapshots = sorted(
            env for env, is_ss in envs_by_version[version].items() if is_ss
        )
        regular = sorted(
            env for env, is_ss in envs_by_version[version].items() if not is_ss
        )

        if snapshots:
            if is_ci_version:
                ss_list = ", ".join(f"COM{env}" for env in snapshots)
            else:
                ss_list = ", ".join(f"{env:02d}" for env in snapshots)
            lines.append(f"- {version} SS → {ss_list}")

        if regular:
            if is_ci_version:
                reg_list = ", ".join(f"COM{env}" for env in regular)
            else:
                reg_list = ", ".join(f"{env:02d}" for env in regular)
            lines.append(f"- {version} → {reg_list}")

    return "\n".join(lines)

def copy_to_clipboard(text: str):
    # Use the native Windows clipboard so the text persists after the script
    # continues or exits, and can be pasted from any other window with Ctrl+V.
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    if user32.OpenClipboard(None):
        try:
            user32.EmptyClipboard()

            data = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(data)
            handle = kernel32.GlobalAlloc(0x0002, size)
            if not handle:
                raise OSError("GlobalAlloc failed")

            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                raise OSError("GlobalLock failed")

            try:
                ctypes.memmove(locked, ctypes.addressof(data), size)
            finally:
                kernel32.GlobalUnlock(handle)

            if not user32.SetClipboardData(13, handle):
                kernel32.GlobalFree(handle)
                raise OSError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()
        return

    # Fallback for environments where the native clipboard is temporarily busy.
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    root.destroy()

def update_daily_envs(text: str, mode: str):
    clean_text = text.rstrip() + "\n"

    if mode == "r":
        DAILY_ENVS_PATH.write_text(clean_text, encoding="utf-8")
        return

    existing = ""
    if DAILY_ENVS_PATH.exists():
        existing = DAILY_ENVS_PATH.read_text(encoding="utf-8")

    if existing.strip():
        DAILY_ENVS_PATH.write_text(existing.rstrip() + "\n\n" + clean_text, encoding="utf-8")
    else:
        DAILY_ENVS_PATH.write_text(clean_text, encoding="utf-8")

def build_blocks_with_plan(mail_texts):
    """Un bloque por mail abierto. Cada mail se procesa por separado para que
    sus versiones y envs no se mezclen con los de los otros mails.

    Devuelve (texto, plan). El plan dice que envs arrancan IN PROGRESS cuando
    se termine de mandar a Webex (ver plan_start_envs)."""
    blocks = []
    plan = []

    for source, text in mail_texts:
        output = build_webex_box(text)
        if output.startswith("No environments detected"):
            print(f"   (sin envs detectados, se omite: {source})")
            continue

        plan.extend(plan_start_envs(text, output))

        out_lines = output.splitlines()
        # build_webex_box devuelve "**title**" y luego "---";
        # aqui el separador va entre bloques, no debajo del titulo.
        title = out_lines[0]
        body = out_lines[1:]
        if body and body[0].strip() == "---":
            body = body[1:]

        blocks.append("\n".join([f"## {title}", *body]))

    if not blocks:
        return "No environments detected in any open mail.", []

    return "\n".join(["\n\n---\n\n".join(blocks)]), plan

def build_blocks(mail_texts):
    return build_blocks_with_plan(mail_texts)[0]

# ---------------------------------------------------------------------------
# Quien arranca primero
# ---------------------------------------------------------------------------
# Los mails piden "primero este env y, si sube bien, entonces los de abajo":
#
#   1. Nttcw67_qa ... Latest 99 ADM CI SD
#   Note: If above Env will upgrade successfully, then only upgrade the below Envs
#   2. Nttcw42_qa ... SD
#
# El de arriba arranca (IN PROGRESS) y los de abajo esperan (ON HOLD). Si el
# mail no trae esa nota (los COM, por ejemplo) no hay "primero": todos arrancan.
# Esto se decide aqui porque solo List_Envs ve el mail; Daily_Envs.txt ya no
# guarda cual era el primero.
GATE_RE = re.compile(
    r"\b(?:if|once|when|after)\b[^.\n]{0,40}\babove\b[^.\n]{0,60}"
    r"\b(?:upgrad|updat|install|deploy|success)"
    r"|\bonly\s+(?:upgrade|update|deploy|install|proceed)\b[^.\n]{0,40}\bbelow\b",
    re.I,
)
ENV_MENTION_RE = re.compile(r"(?:nttcw|nttcom|com)?\s*(\d{1,3})\s*[_-]?\s*qa\b", re.I)
URL_RE = re.compile(r"https?://\S+")

# Una linea de env ya armada: [**43QA**](url) (T2.3.55)
ENV_LINE_RE = re.compile(r"\[(?:\*\*)?((?:COM)?\d+QA)(?:\*\*)?\]\([^)]*\)\s*\(([^)]*)\)")
STATUS_HEADER_RE = re.compile(
    r"^\s*(?:\S+\s+)?\*\*(PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*", re.I
)
TITLE_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\*\*(?!(?:PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED)\*\*)(.+?)\*\*\s*$",
    re.I,
)
IN_PROGRESS_HEADER = "\U0001F504 **IN PROGRESS**:"

def block_key(title: str) -> str:
    """'## **T2.3.55 — 46CI**' y '**T2.3.55 — 46CI**' son el mismo bloque."""
    return " ".join(re.sub(r"[#*]+", " ", title).split())

def env_number(label: str) -> int:
    return int(re.search(r"\d+", label).group())

def find_first_envs(raw_text: str, produced):
    """Numeros de env que el mail manda ANTES de la nota de "si el de arriba
    sube bien...". None si no hay nota (o no separa nada): todos arrancan.

    Solo cuentan los envs que build_webex_box si dejo en el bloque (`produced`),
    asi un "46_qa" suelto en el nombre del CI o en una URL no se cuela."""
    first = set()
    gate_seen = False

    for line in raw_text.splitlines():
        if GATE_RE.search(line):
            gate_seen = True
            break

        for match in ENV_MENTION_RE.finditer(URL_RE.sub(" ", line)):
            number = int(match.group(1))
            if number in produced:
                first.add(number)

    if not gate_seen or not first or first >= produced:
        return None

    return first

def plan_start_envs(raw_text: str, output: str):
    """Lista de (bloque, env, version) que deben quedar IN PROGRESS.
    `output` es lo que devolvio build_webex_box para este mismo mail."""
    lines = output.splitlines()
    if not lines:
        return []

    title = block_key(lines[0])
    env_lines = []
    for line in lines[1:]:
        match = ENV_LINE_RE.search(line)
        if match:
            env_lines.append((match.group(1).upper(), match.group(2).strip()))

    produced = {env_number(label) for label, _ in env_lines}
    first = find_first_envs(raw_text, produced)

    return [
        (title, label, version)
        for label, version in env_lines
        if first is None or env_number(label) in first
    ]

def promote_envs_to_in_progress(path, plan) -> int:
    """Mueve de ON HOLD a IN PROGRESS las lineas del plan en Daily_Envs.txt.

    Solo agrega una seccion IN PROGRESS arriba del ON HOLD de cada bloque;
    el orden y el formato final los deja OrganizeResults, que junta las
    secciones repetidas. Devuelve cuantas lineas movio."""
    wanted = set(plan)
    if not wanted:
        return 0

    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    block = -1
    title = ""
    status = None
    hold_header = {}   # bloque -> indice de su primer encabezado ON HOLD
    moved = {}         # bloque -> lineas que se mueven
    dropped = set()

    for index, line in enumerate(lines):
        if TITLE_LINE_RE.match(line):
            block += 1
            title = block_key(line)
            status = None
            continue

        header = STATUS_HEADER_RE.match(line)
        if header:
            status = header.group(1).upper()
            if status == "ON HOLD":
                hold_header.setdefault(block, index)
            continue

        match = ENV_LINE_RE.search(line)
        if match and status == "ON HOLD":
            key = (title, match.group(1).upper(), match.group(2).strip())
            if key in wanted:
                moved.setdefault(block, []).append(line.rstrip())
                dropped.add(index)

    if not dropped:
        return 0

    insert_before = {hold_header[b]: moved[b] for b in moved if b in hold_header}

    out = []
    for index, line in enumerate(lines):
        if index in dropped:
            continue
        if index in insert_before:
            out.extend([IN_PROGRESS_HEADER, *insert_before[index], ""])
        out.append(line)

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(dropped)

def strip_env_lists(text: str) -> str:
    """Version para Webex: mismo output pero sin las 'Environment lists'.
    Esas listas solo sirven para el archivo/los scripts, no para el chat."""
    out_lines = []
    skipping = False
    skip_blanks = False

    for line in text.splitlines():
        stripped = line.strip()

        # Tras el "---" que separa bloques no va linea en blanco en Webex.
        if skip_blanks:
            if not stripped:
                continue
            skip_blanks = False

        if stripped == "Environment lists:":
            skipping = True
            # El "---" que va justo antes tampoco se necesita si el bloque
            # ya termina aqui; se vuelve a agregar entre bloques mas abajo.
            while out_lines and out_lines[-1].strip() in {"", "---"}:
                out_lines.pop()
            continue

        if skipping:
            if stripped.startswith("- "):
                continue
            if not stripped:
                continue
            skipping = False
            # Lo que sigue es el separador/titulo del siguiente bloque.
            out_lines.append("")
            out_lines.append("---")
            if stripped == "---":
                skip_blanks = True
                continue

        out_lines.append(line)

    return "\n".join(out_lines).rstrip() + "\n"

def strip_quoted_reply(body: str) -> str:
    """Se queda solo con el mensaje mas reciente. Cuando alguien contesta sin
    borrar el hilo, los envs de ayer siguen ahi abajo y se colaban al output;
    aqui se corta en el header de la respuesta citada ("From:/Sent:", "-----
    Original Message-----", o la linea de guiones bajos de Outlook)."""
    lines = body.splitlines()
    cut_at = None

    separator_re = re.compile(r"^\s*(?:-{2,}\s*Original Message\s*-{2,}|_{10,})\s*$", re.I)
    from_re = re.compile(r"^\s*(?:From|De)\s*:\s*\S", re.I)
    follow_re = re.compile(r"^\s*(?:Sent|Date|To|Enviado|Para)\s*:\s*\S", re.I)

    for i, line in enumerate(lines):
        if separator_re.match(line):
            cut_at = i
            break

        # "From:" solo no basta (puede ir en texto normal); se confirma con el
        # "Sent:"/"To:" que Outlook siempre pone en las siguientes lineas.
        if from_re.match(line):
            lookahead = [l for l in lines[i + 1:i + 6] if l.strip()]
            if any(follow_re.match(l) for l in lookahead):
                cut_at = i
                break

    if cut_at is None:
        return body

    return "\n".join(lines[:cut_at]).rstrip()

def get_open_mail_texts():
    """Body de cada mail que esté abierto en su propia ventana en Outlook
    (doble click / Reply / etc). Devuelve [] si Outlook no corre o no hay
    ninguna ventana de mail abierta - en ese caso se cae al input manual."""
    if win32 is None:
        return []

    try:
        outlook = win32.GetActiveObject("Outlook.Application")
    except Exception:
        return []

    mails = []
    try:
        inspectors = outlook.Inspectors
    except Exception:
        return []

    for inspector in inspectors:
        try:
            item = inspector.CurrentItem
            body = getattr(item, "Body", None)
            if body and body.strip():
                subject = getattr(item, "Subject", "") or "(sin asunto)"
                latest = strip_quoted_reply(body)
                if latest.strip():
                    mails.append((subject, latest))
        except Exception:
            continue

    return mails

def run_organize_results(webex: bool = True):
    """Se corre siempre despues de escribir Daily_Envs.txt, que es lo que se
    hacia a mano cada vez. webex=False lo deja solo ordenar, sin mandar nada
    a Webex."""
    print("\n▶ Corriendo OrganizeResults...")
    command = [sys.executable, str(ORGANIZE_RESULTS_PATH)]
    if not webex:
        command.append("--no-webex")
    result = subprocess.run(command)

    if result.returncode != 0:
        print(f"⚠ OrganizeResults termino con codigo {result.returncode}")

def start_first_envs(plan):
    """Despues de que OrganizeResults ya mando el estado inicial a Webex (todo
    en ON HOLD): el primero de cada mail pasa a IN PROGRESS y el resto se
    queda ON HOLD (los COM, que no tienen primero, arrancan todos).

    Se vuelve a correr OrganizeResults solo para dejar el archivo ordenado,
    SIN Webex: el mensaje de Webex se queda con el estado inicial (ON HOLD) y
    aqui termina el script."""
    moved = promote_envs_to_in_progress(DAILY_ENVS_PATH, plan)

    if not moved:
        print("\n⚠ No encontre en ON HOLD los envs que tenian que arrancar; no cambie nada.")
        return

    print(f"\n▶ {moved} env(s) pasaron a IN PROGRESS:")
    for title, label, version in plan:
        print(f"   - {label} ({version})  [{title}]")

    run_organize_results(webex=False)

def read_multiline_input() -> str:
    print("Paste text. Finish with triple Enter:\n")

    lines = []
    empty = 0

    while True:
        line = input()
        if line.strip() == "":
            empty += 1
            if empty == 3:
                break
        else:
            empty = 0
        lines.append(line)

    return "\n".join(lines)

if __name__ == "__main__":
    while True:
        open_mails = get_open_mail_texts()

        if open_mails:
            print(f"\n📧 {len(open_mails)} mail(s) abierto(s) en Outlook detectado(s):")
            for subject, _ in open_mails:
                print(f"   - {subject}")
            output, start_plan = build_blocks_with_plan(open_mails)
        else:
            raw_text = strip_quoted_reply(read_multiline_input())
            output = build_webex_box(raw_text)
            start_plan = plan_start_envs(raw_text, output)

        print("\n" + output)
        # Al archivo va el output completo; al clipboard la version de Webex.
        copy_to_clipboard(strip_env_lists(output))
        print("\nOutput copied to clipboard (sin env lists, listo para Webex).")

        while True:
            answer = input("\nDo you want to make another list? (y/n/r/v): ").strip().lower()
            if answer in {"y", "n", "r", "v"}:
                break
            print("Please answer with y, n, r, or v.")

        if answer in {"r", "v"}:
            update_daily_envs(output, answer)
            action = "replaced" if answer == "r" else "updated"
            print(f"\nDaily_Envs.txt {action}: {DAILY_ENVS_PATH}")
            run_organize_results()   # ordena y manda a Webex el estado inicial
            start_first_envs(start_plan)
            break

        if answer == "n":
            break

        print()
