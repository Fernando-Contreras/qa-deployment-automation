import re
import shutil
import subprocess
import sys
from pathlib import Path

import ntt_config

FILE_PATH = ntt_config.DAILY_ENVS_PATH
BACKUP_PATH = FILE_PATH.with_suffix(".txt.bak")
WEBEX_UPDATE_PATH = Path(__file__).with_name("Webex_Update.py")
QA_ENVS_PATH = Path(__file__).with_name("QA_Envs.py")

# Versiones conocidas: T2.3.48, CI_123, 46CI, UNKNOWN
VERSION_TOKEN = r"(?:T[\d.]+|CI_\d+|\d+CI|UNKNOWN)"
# La UNICA linea de la que depende todo: el link de un env. Es lo que genera
# List_Envs.py y lo unico que no ha cambiado de formato. Los bloques se deducen
# a partir de estas lineas, no de como este escrito el titulo.
ENV_LABEL_RE = re.compile(r"\[(?:\*\*)?((?:COM)?(\d+)QA)(?:\*\*)?\]")
VERSION_RE = re.compile(r"\((T(\d+\.\d+\.\d+(?:\.\d+)?))\)")
CI_VERSION_RE = re.compile(r"\(CI_(\d+)(?:\s|[)])")
NUM_CI_VERSION_RE = re.compile(r"\((\d+)CI(?:\s|[)])")
FAILED_SUFFIX_RE = re.compile(
    rf"\({VERSION_TOKEN}[^)]*\)\s+-\s+\S.*$"
)

STATUS_NAMES = "PASSED|IN PROGRESS|ON HOLD|BLOCKED|FAILED"
# Dos formas validas, para no depender de las negritas ni del icono:
#   1) con **negritas**, los dos puntos opcionales -> "🔒 **ON HOLD** - nota:"
#   2) sin negritas, pero entonces los dos puntos son obligatorios -> "ON HOLD:"
# La segunda exige el ":" a proposito, si no una linea como "Deploy failed"
# se confundiria con un encabezado.
STATUS_HEADER_RE = re.compile(
    rf"^\s*(?:#{{1,6}}\s*)?(?:\S+\s+)?"
    rf"(?:\*\*\s*({STATUS_NAMES})\s*\*\*([^\n]*?):?"
    rf"|({STATUS_NAMES})([^\n]*?):)"
    rf"\s*$",
    re.IGNORECASE,
)
# Lineas de "cola" de un bloque: nunca son titulo.
TAIL_LINE_RE = re.compile(
    r"^\s*(?:file://|environment\s+lists?\s*:|[-*+]\s|\d+[.)]\s)",
    re.IGNORECASE,
)
# Separadores horizontales de markdown.
RULE_LINE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
PASSED_HINT_RE = re.compile(r"\beast\b.*\bwest\b|\bwest\b.*\beast\b", re.IGNORECASE)

STATUS_ORDER = ["PASSED", "IN PROGRESS", "ON HOLD", "FAILED", "BLOCKED"]
# Estados que el usuario pone a mano a proposito: la heuristica EAST/WEST
# no los reescribe.
STICKY_STATUSES = {"ON HOLD", "BLOCKED"}
STATUS_ICONS = {
    "PASSED": "\u2705",
    "IN PROGRESS": "\U0001F504",
    "ON HOLD": "\U0001F512",
    "BLOCKED": "\u26A0\uFE0F",
    "FAILED": "\u274C",
}


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def match_status_header(line: str):
    """Devuelve (STATUS, nota) si la linea es un encabezado de estado, o None."""
    match = STATUS_HEADER_RE.match(line.strip())
    if not match:
        return None
    status = (match.group(1) or match.group(3)).upper()
    note = (match.group(2) if match.group(1) else match.group(4)) or ""
    return status, note.rstrip()


def is_rule_line(line: str) -> bool:
    return bool(RULE_LINE_RE.match(line))


def is_tail_line(line: str) -> bool:
    return bool(TAIL_LINE_RE.match(line))


def is_title_candidate(line: str) -> bool:
    """Cualquier linea con texto que no sea env, estado, separador ni cola."""
    stripped = line.strip()
    if not stripped:
        return False
    if is_env_line(line) or is_rule_line(line) or is_tail_line(line):
        return False
    return match_status_header(line) is None


def get_env_num(line: str) -> int:
    match = ENV_LABEL_RE.search(line)
    return int(match.group(2)) if match else 999


def is_env_line(line: str) -> bool:
    return bool(ENV_LABEL_RE.search(line))


def has_failed_suffix(line: str) -> bool:
    return bool(FAILED_SUFFIX_RE.search(line))


def get_version_tuple(line: str):
    match = VERSION_RE.search(line)
    if match:
        parts = [int(part) for part in match.group(2).split(".")]
        while len(parts) < 4:
            parts.append(0)
        return (0, *parts[:4])

    ci_match = CI_VERSION_RE.search(line) or NUM_CI_VERSION_RE.search(line)
    if ci_match:
        return (1, int(ci_match.group(1)), 0, 0, 0)

    return (99, 0, 0, 0, 0)


def sort_env_lines(lines):
    return sorted(lines, key=lambda line: (get_version_tuple(line), get_env_num(line)))


def find_footer_start(block_text: str):
    lines = block_text.splitlines(keepends=True)
    footer_start = None
    saw_file_line = False
    offset = len(block_text)

    for line in reversed(lines):
        offset -= len(line)
        stripped = line.strip()

        if stripped == "":
            if saw_file_line:
                footer_start = offset
            continue

        if stripped.startswith("file://"):
            saw_file_line = True
            footer_start = offset
            continue

        if saw_file_line and match_status_header(stripped):
            footer_start = offset
            continue

        if saw_file_line:
            break

    return footer_start if saw_file_line else None


def find_header_indexes(lines):
    """Titulos de bloque, deducidos por estructura y no por sintaxis.

    Una linea es titulo si tiene texto propio (no es env, ni estado, ni "---",
    ni cola tipo "Environment lists:" / "- x" / "file://") y si mas adelante,
    saltando separadores y encabezados de estado, aparece una linea de env
    antes que otro titulo. Asi da igual si viene como "**X**", "## **X**",
    "### X" o texto pelon.
    """
    headers = []

    for index, line in enumerate(lines):
        if not is_title_candidate(line):
            continue

        for next_line in lines[index + 1:]:
            if not next_line.strip():
                continue
            if is_env_line(next_line):
                headers.append(index)
                break
            if is_rule_line(next_line) or match_status_header(next_line):
                continue
            # Otro titulo o una linea de cola: este candidato no encabeza envs.
            break

    return headers


def split_blocks(text: str):
    lines = text.splitlines(keepends=True)
    offsets = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line)
    offsets.append(running)

    header_indexes = find_header_indexes([line.rstrip("\n") for line in lines])
    if not header_indexes:
        return [], text, ""

    prefix = text[:offsets[header_indexes[0]]]
    blocks = []

    for position, start_index in enumerate(header_indexes):
        if position + 1 < len(header_indexes):
            end_index = header_indexes[position + 1]
        else:
            end_index = len(lines)
        blocks.append(text[offsets[start_index]:offsets[end_index]])

    last_block = blocks[-1]
    footer_start = find_footer_start(last_block)
    suffix = ""
    if footer_start is not None:
        blocks[-1] = last_block[:footer_start]
        suffix = last_block[footer_start:]
        suffix, recovered = rescue_envs_from_footer(suffix)
        if recovered:
            blocks[-1] = blocks[-1].rstrip("\n") + "\n" + recovered

    return blocks, prefix, suffix


def rescue_envs_from_footer(footer_text: str):
    """Rescata envs que hayan caido en el pie de pagina.

    normalize_footer() solo conserva las lineas file:// y los marcadores de
    estado, asi que cualquier env ahi dentro se perdia para siempre. Pasa
    cuando Track_Deployments inserta un env PASSED en la leyenda del final.
    Aqui se sacan del pie, conservando bajo que estado estaban, para
    reinyectarlos en el ultimo bloque.
    """
    kept = []
    recovered = {}
    current_status = None

    for line in footer_text.splitlines():
        status_match = match_status_header(line)
        if status_match:
            current_status = status_match[0]
            kept.append(line)
            continue

        if is_env_line(line):
            recovered.setdefault(current_status, []).append(line.rstrip())
            continue

        kept.append(line)

    if not recovered:
        return footer_text, ""

    chunk = []
    for status, env_lines in recovered.items():
        if status:
            chunk.append(f"{STATUS_ICONS[status]} **{status}**:")
        chunk.extend(env_lines)

    total = sum(len(env_lines) for env_lines in recovered.values())
    print(f"♻️  {total} env(s) rescatado(s) del pie de pagina y devuelto(s) al bloque.")

    return "\n".join(kept), "\n".join(chunk) + "\n"


def parse_block(block_text: str, promoted_out: list = None):
    """promoted_out: si se pasa una lista, se le agregan (con .extend) las
    lineas de env que este bloque paso de ON HOLD a IN PROGRESS por tener ya
    algo PASSED. Sirve para decidir si hay que triggerear QA_Envs.py."""
    lines = block_text.splitlines()
    if not lines:
        return block_text

    header = lines[0].rstrip()
    idx = 1
    # Saltar los separadores que vengan pegados al titulo (los volvemos a poner).
    while idx < len(lines) and (not lines[idx].strip() or is_rule_line(lines[idx])):
        if is_rule_line(lines[idx]):
            idx += 1
            break
        idx += 1

    statuses = {status: [] for status in STATUS_ORDER}
    notes = {}
    env_tail = []
    current_status = None

    for raw_line in lines[idx:]:
        stripped = raw_line.strip()
        status_match = match_status_header(raw_line)
        if status_match:
            current_status, note = status_match
            if note:
                notes[current_status] = note
            continue

        if is_env_line(raw_line):
            clean_line = raw_line.rstrip()
            if current_status in STICKY_STATUSES:
                # ON HOLD / BLOCKED son candado: tu los pusiste ahi con su
                # propia razon, ni el "- motivo" de FAILED ni nada mas los
                # saca de aqui.
                statuses[current_status].append(clean_line)
            elif has_failed_suffix(clean_line):
                statuses["FAILED"].append(clean_line)
            elif current_status == "PASSED":
                statuses["PASSED"].append(clean_line)
            elif PASSED_HINT_RE.search(clean_line):
                # Ya trae EAST y WEST -> el deploy termino, se promueve a PASSED
                # aunque siga escrito bajo IN PROGRESS.
                statuses["PASSED"].append(clean_line)
            elif current_status in statuses:
                statuses[current_status].append(clean_line)
            else:
                # Sin ningun encabezado de estado todavia (env pegado justo
                # despues del titulo, antes de cualquier "**STATUS**:").
                statuses["ON HOLD"].append(clean_line)
            continue

        if stripped == "":
            continue

        env_tail.append(raw_line.rstrip())

    for status in STATUS_ORDER:
        statuses[status] = sort_env_lines(list(dict.fromkeys(statuses[status])))

    promoted_to_progress = []
    if statuses["PASSED"] and not notes.get("ON HOLD"):
        # Si ya hay algo PASSED en el bloque, el build se destrabo: los ON HOLD
        # sin razon propia vuelven a IN PROGRESS. Se respetan dos tipos de
        # razon, cualquiera de las dos basta para que el env se quede quieto:
        #   - una nota de grupo en el header ("ON HOLD - T3 & T7 copies...")
        #   - un "- motivo" pegado a la linea del env, igual que un FAILED
        still_on_hold = []
        for line in statuses["ON HOLD"]:
            if has_failed_suffix(line):
                still_on_hold.append(line)
            else:
                statuses["IN PROGRESS"].append(line)
                promoted_to_progress.append(line)
        statuses["ON HOLD"] = still_on_hold
        statuses["IN PROGRESS"] = sort_env_lines(list(dict.fromkeys(statuses["IN PROGRESS"])))
        if promoted_out is not None and promoted_to_progress:
            promoted_out.extend(promoted_to_progress)

    rebuilt = [header, "---"]

    for status in STATUS_ORDER:
        lines_for_status = statuses[status]
        if not lines_for_status:
            continue

        rebuilt.append(f"{STATUS_ICONS[status]} **{status}**{notes.get(status, '')}:")
        rebuilt.extend(lines_for_status)
        rebuilt.append("")

    while rebuilt and rebuilt[-1] == "":
        rebuilt.pop()

    # El "---" del final no es contenido del bloque, es el separador que va
    # entre bloques; se quita aqui y main() lo vuelve a poner con su espacio.
    while env_tail and is_rule_line(env_tail[-1]):
        env_tail.pop()

    if env_tail:
        rebuilt.append("")
        rebuilt.extend(env_tail)

    return "\n".join(rebuilt).rstrip() + "\n"


def env_identity(label: str):
    """Clave para comparar envs.

    COM6QA y 06QA son envs DISTINTOS aunque compartan el numero, porque viven
    en granjas distintas (nttcom* vs nttcw*). En cambio 6QA y 06QA son el mismo
    env escrito con y sin cero al frente.
    """
    match = ENV_LABEL_RE.search(label)
    if not match:
        return None
    family = "COM" if match.group(1).upper().startswith("COM") else "CW"
    return (family, int(match.group(2)))


def find_duplicate_envs(rebuilt_blocks):
    """Busca envs que aparezcan mas de una vez en todo el archivo."""
    occurrences = {}

    for block in rebuilt_blocks:
        lines = block.splitlines()
        if not lines:
            continue

        header = lines[0].strip().lstrip("#").strip().strip("*").strip()
        status = "?"

        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue

            status_match = match_status_header(stripped)
            if status_match:
                status = status_match[0]
                continue

            match = ENV_LABEL_RE.search(line)
            if not match:
                continue

            key = env_identity(match.group(0))
            if key is None:
                continue
            occurrences.setdefault(key, []).append((match.group(1), status, header))

    return {key: places for key, places in occurrences.items() if len(places) > 1}


def report_duplicate_envs(rebuilt_blocks):
    duplicates = find_duplicate_envs(rebuilt_blocks)

    if not duplicates:
        print("\n✅ Sin envs repetidos.")
        return

    total = len(duplicates)
    print(f"\n⚠️  {total} env(s) repetido(s):")

    for key in sorted(duplicates, key=lambda item: (item[0], item[1])):
        places = duplicates[key]
        labels = list(dict.fromkeys(label for label, _, _ in places))
        shown = " / ".join(labels)
        print(f"   {shown} — {len(places)} veces:")
        for label, status, header in places:
            suffix = f"  (como {label})" if len(labels) > 1 else ""
            print(f"     - {status:<11} | {header}{suffix}")


def normalize_footer(footer_text: str):
    footer_text = footer_text.strip("\n")
    footer_lines = [line.rstrip() for line in footer_text.splitlines() if line.strip()]

    file_lines = [line for line in footer_lines if line.startswith("file://")]

    marker_names = []
    for line in footer_lines:
        match = match_status_header(line)
        if match and match[0] not in marker_names:
            marker_names.append(match[0])

    if not marker_names:
        marker_names = STATUS_ORDER[:]

    marker_lines = [
        f"{STATUS_ICONS[status]} **{status}**:"
        for status in STATUS_ORDER
        if status in marker_names
    ]

    parts = []
    if file_lines:
        parts.append("\n".join(file_lines))
    if marker_lines:
        parts.append("\n".join(marker_lines))

    return "\n\n".join(parts).rstrip() + "\n"


def count_env_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if is_env_line(line))


def report_orphan_envs(original_text: str, blocks, prefix: str, footer: str):
    """Red de seguridad: ningun env se debe quedar fuera de un bloque.

    Si el formato vuelve a cambiar y la deteccion falla, esto lo grita en vez
    de dejar envs sin clasificar en silencio.
    """
    total = count_env_lines(original_text)
    inside = sum(count_env_lines(block) for block in blocks)
    orphans = total - inside

    if orphans <= 0:
        print(f"Envs detectados: {total} (todos dentro de un bloque)")
        return

    print(f"\n⛔  {orphans} de {total} env(s) quedaron FUERA de todo bloque.")
    print("   No se reorganizaron. Revisa estas lineas:")

    for number, line in enumerate(original_text.splitlines(), start=1):
        if not is_env_line(line):
            continue
        where = "prefix" if line in prefix else ("footer" if line in footer else None)
        if where:
            print(f"     linea {number} ({where}): {line.strip()[:90]}")


def run_webex_update():
    """Sincroniza los mensajes de Webex con el Daily_Envs ya organizado.

    Se centraliza aquí (y no en cada script que llama a OrganizeResults)
    porque varios scripts corren OrganizeResults, y todos deben terminar
    reflejando el archivo final en Webex, no solo el flujo E2E.
    """
    print("\n📣 Ejecutando Webex_Update.py...")
    try:
        subprocess.run([sys.executable, str(WEBEX_UPDATE_PATH)], check=True)
    except Exception as e:
        print("⚠️ No se pudo ejecutar Webex_Update.py")
        print(e)


def run_qa_envs_trigger(promoted_lines):
    """Dispara QA_Envs.py cuando ON HOLD paso a IN PROGRESS solo.

    QA_Envs.py abre su propio navegador (playwright) y ya sabe leer el
    Daily_Envs ya organizado para decidir que triggerear, asi que basta con
    lanzarlo sin argumentos. Va en su propia consola, igual que
    Deployment_Defect, para no bloquear ni mezclar su output con este script.
    """
    labels = ", ".join(
        sorted({m.group(1) for line in promoted_lines if (m := ENV_LABEL_RE.search(line))})
    )
    print(f"\n🚀 {len(promoted_lines)} env(s) pasaron de ON HOLD a IN PROGRESS ({labels}).")
    print("   Triggereando QA_Envs.py en nueva terminal...")
    try:
        subprocess.Popen(
            [sys.executable, str(QA_ENVS_PATH)],
            cwd=str(QA_ENVS_PATH.parent),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as e:
        print("⚠️ No se pudo lanzar QA_Envs.py")
        print(e)
        print(f"   Córrelo a mano: py {QA_ENVS_PATH}")


def main(run_webex: bool = True):
    """
    run_webex=False es para cuando OrganizeResults se llama como paso
    intermedio de otro flujo que todavía va a seguir modificando el
    archivo (ej. Deployment_Defect corriéndolo antes de pegar los links
    de Jira): ahí publicar a Webex sería prematuro. El script suelto
    (py OrganizeResults.py) y los demás llamadores sí quieren el sync.
    """
    original_text = normalize_newlines(FILE_PATH.read_text(encoding="utf-8"))
    shutil.copyfile(FILE_PATH, BACKUP_PATH)

    blocks, prefix, footer = split_blocks(original_text)
    if not blocks:
        found = count_env_lines(original_text)
        raise RuntimeError(
            "No se detecto ningun bloque en Daily_Envs.txt "
            f"(hay {found} linea(s) de env en el archivo)."
        )

    promoted_to_progress = []
    rebuilt_blocks = [parse_block(block, promoted_to_progress) for block in blocks]

    final_parts = []
    if prefix.strip():
        final_parts.append(prefix.strip("\n"))

    # Dos lineas en blanco despues de cada "Environment lists" (ahi se pega a
    # mano el file:// del SD) y luego el separador del siguiente bloque.
    final_parts.append(
        "\n\n\n---\n\n".join(block.rstrip("\n") for block in rebuilt_blocks)
    )

    normalized_footer = normalize_footer(footer)
    if normalized_footer.strip():
        final_parts.append(normalized_footer.strip("\n"))

    final_text = "\n\n".join(part for part in final_parts if part.strip()) + "\n"
    FILE_PATH.write_text(final_text, encoding="utf-8")

    print("Archivo organizado correctamente.")
    print(f"Backup: {BACKUP_PATH}")
    print(f"Blocks processed: {len(rebuilt_blocks)}")

    report_orphan_envs(original_text, blocks, prefix, footer)
    report_duplicate_envs(rebuilt_blocks)

    if promoted_to_progress:
        run_qa_envs_trigger(promoted_to_progress)

    if run_webex:
        run_webex_update()


if __name__ == "__main__":
    main()
