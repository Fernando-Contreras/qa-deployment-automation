"""
MailCOMs: busca en tus mensajes de Webex del chat "QA Build installation" el
mas reciente con envs COM en PASSED (el mismo tipo de mensaje que manda
Webex_Update, ej. "CI_23 - Build & CI_07 - Build"), escribe esa lista en la
pestaña "COM envs" del Excel del Quality Gate, y arma el correo (mensaje
inicial + tabla + firma) en el mail que tengas abierto en Outlook.

Reutiliza el login/token de Webex de Webex_Update.py (misma sala, mismo
Personal Access Token / OAuth ya configurado). Al final, igual que
MailQAs.py, borra de Daily_Envs.txt el bloque cuyos envs ya se mandaron.

Por ahora no toca Deployment Status / Sanity Status / Comments (columnas
E, F, G de la tabla): esas se llenan a mano en Excel. Ya después se
perfecciona.
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import win32com.client as win32

import ntt_config
import Webex_Update as webex

EXCEL_PATH = str(ntt_config.QG_SCOPE_ANALYSIS_PATH)
SHEET_NAME = "COM envs"

HEADER_ROW = 5
FIRST_DATA_ROW = 6
ENV_COL = 4  # D

wdStory = 6

# Cuantas horas hacia atras se buscan mensajes en Webex (mas de un dia por
# si se corre muy temprano y el mensaje de ayer todavia aplica).
LOOKBACK_HOURS = 36

COM_ENV_RE = re.compile(r"(COM\d+QA)", re.IGNORECASE)
CI_TOKEN_RE = re.compile(r"(CI_\d+(?:\s+SS)?)", re.IGNORECASE)
SECTION_RE = re.compile(r"\*\*\s*([A-Z ]+?)\s*\*\*\s*:", re.IGNORECASE)

# Mismos patrones que MailQAs.py usa para partir Daily_Envs.txt en bloques y
# sacar los envs de cada uno (aqui van sueltos para no tener que importar
# MailQAs.py, que al importarse pide credenciales de Jenkins que este script
# no necesita).
BLOCK_HEADER_RE = re.compile(
    r"^#{0,6}\s*\*\*(?:T[\d.]+|CI_\d+|\d+CI\b).*?\*\*\s*$", re.MULTILINE
)
ENV_LABEL_RE = re.compile(r"\[(?:\*\*)?([A-Za-z]*\d+QA)(?:\*\*)?\]", re.IGNORECASE)


def normalize_env_label(raw):
    match = re.match(r"([A-Za-z]*)(\d+)QA$", raw.strip(), re.IGNORECASE)
    if not match:
        return raw.strip().upper()
    prefix, num = match.group(1).upper(), int(match.group(2))
    return f"{prefix}{num}QA"


def normalize_ci_label(raw):
    return re.sub(r"\s+", " ", raw.strip()).upper()


def extract_passed_com_envs(text):
    """Saca 'COM3QA (CI_07)' de la seccion PASSED de un mensaje de Webex.
    Ignora todo lo demas (otros status, links sueltos, etc.) y no repite envs."""
    envs = []
    seen = set()
    in_passed = False

    for line in (text or "").splitlines():
        section_match = SECTION_RE.search(line)
        if section_match:
            in_passed = section_match.group(1).strip().upper() == "PASSED"
            continue

        if not in_passed:
            continue

        env_match = COM_ENV_RE.search(line)
        ci_match = CI_TOKEN_RE.search(line)
        if not env_match or not ci_match:
            continue

        label = f"{normalize_env_label(env_match.group(1))} ({normalize_ci_label(ci_match.group(1))})"
        if label not in seen:
            seen.add(label)
            envs.append(label)

    return envs


def remove_com_block_from_daily_envs(path, envs):
    """Borra de Daily_Envs.txt el/los bloque(s) que traigan alguno de los
    envs que ya se mandaron por correo (normalmente uno solo, el combinado
    tipo 'CI_23 - Build & CI_07 - Build')."""
    path = Path(path)
    if not path.exists():
        print(f"No encontre {path}; Daily_Envs.txt se deja igual.")
        return

    raw_text = path.read_text(encoding="utf-8")
    matches = list(BLOCK_HEADER_RE.finditer(raw_text))
    if not matches:
        print("No encontre bloques en Daily_Envs.txt; se deja igual.")
        return

    wanted = {normalize_env_label(env.split(" ")[0]) for env in envs}

    new_text = raw_text
    removed = 0
    # De atras hacia adelante: asi los offsets de los bloques que todavia no
    # se tocan (calculados sobre raw_text) se mantienen validos.
    for index in range(len(matches) - 1, -1, -1):
        start = matches[index].start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_text)
        block_envs = {
            normalize_env_label(m.group(1))
            for m in ENV_LABEL_RE.finditer(raw_text[start:end])
        }
        if block_envs & wanted:
            new_text = new_text[:start] + new_text[end:]
            removed += 1

    if not removed:
        print("Ningun bloque de Daily_Envs.txt coincidio con los envs mandados; se deja igual.")
        return

    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    path.write_text(new_text, encoding="utf-8")
    print(f"{removed} bloque(s) borrado(s) de Daily_Envs.txt.")


def find_latest_com_envs(room=None, lookback_hours=LOOKBACK_HOURS):
    """Token/sala/mensajes via Webex_Update; regresa (envs, mensaje) del mas
    reciente que traiga envs COM en PASSED, o ([], None) si no hay ninguno."""
    room = room or webex.DEFAULT_ROOM
    since_utc = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    token, source = webex.get_token()
    print(f"[info] Token de Webex: {source}")

    my_id = webex.get_my_id(token)
    room_id = webex.find_room_id(token, room)
    messages = webex.my_messages_since(token, room_id, my_id, since_utc)
    print(f"[info] {len(messages)} mensaje(s) tuyo(s) en las ultimas {lookback_hours}h en {room!r}")

    for message in reversed(messages):  # el mas nuevo primero
        text = message.get("markdown") or message.get("text") or ""
        envs = extract_passed_com_envs(text)
        if envs:
            return envs, message

    return [], None


def get_excel_app():
    try:
        return win32.GetActiveObject("Excel.Application")
    except Exception:
        return win32.Dispatch("Excel.Application")


def find_workbook(excel, excel_path):
    target_path = str(Path(excel_path)).lower()

    for book in excel.Workbooks:
        if book.FullName.lower() == target_path:
            return book, False

    return excel.Workbooks.Open(excel_path, ReadOnly=False), True


def clear_cell(cell):
    try:
        cell.Hyperlinks.Delete()
    except Exception:
        pass
    cell.Value = ""


def write_envs_to_excel(ws, envs):
    """Solo toca la columna Environment (D): escribe la lista nueva desde
    FIRST_DATA_ROW y, si la lista anterior era mas larga, limpia el sobrante
    de D. Deployment Status / Sanity Status / Comments (E, F, G) no se tocan."""
    row = FIRST_DATA_ROW
    for env in envs:
        ws.Cells(row, ENV_COL).Value = env
        row += 1
    last_row = row - 1

    while ws.Cells(row, ENV_COL).Value:
        clear_cell(ws.Cells(row, ENV_COL))
        row += 1

    return last_row


def insert_signature(selection):
    selection.TypeText("Thank you & Best regards,")
    selection.TypeParagraph()
    selection.TypeText("-----------------------------")
    selection.TypeParagraph()
    selection.TypeText("Alex Rivera, QA Engineer")
    selection.TypeParagraph()
    selection.Font.Bold = True
    selection.TypeText("Example Corp")
    selection.Font.Bold = False
    selection.TypeParagraph()
    selection.TypeParagraph()
    selection.TypeText("+00 0000000000 | Mobile")
    selection.TypeParagraph()


def get_open_mail_for_reply(outlook):
    warned = False

    while True:
        inspector = outlook.ActiveInspector()
        current_item = inspector.CurrentItem if inspector is not None else None

        if current_item is not None:
            return current_item

        if not warned:
            print("Abre el mail correcto en Outlook... esperando.")
            warned = True

        time.sleep(2)


def create_reply(target_mail):
    try:
        return target_mail.ReplyAll()
    except Exception as e:
        print("No se pudo crear ReplyAll. Intentando Reply normal...")
        print(e)

    try:
        return target_mail.Reply()
    except Exception as e:
        print("No se pudo crear Reply en Outlook.")
        print(e)
        sys.exit(1)


def main():
    """Webex (QA Build installation) -> columna Environment en 'COM envs' ->
    correo con esa tabla. Sin CAs ni patches: solo mensaje inicial, tabla y firma."""
    envs, message = find_latest_com_envs()

    if not envs:
        print(
            f"No encontre ningun mensaje con envs COM en PASSED en las ultimas "
            f"{LOOKBACK_HOURS}h de {webex.DEFAULT_ROOM!r}."
        )
        sys.exit(1)

    header = (message.get("markdown") or message.get("text") or "").splitlines()[0]
    print(f"Mensaje: {header.strip()}")
    print(f"{len(envs)} env(s) encontrados:")
    for env in envs:
        print(f"  - {env}")

    excel = get_excel_app()
    excel.Visible = True
    wb, _ = find_workbook(excel, EXCEL_PATH)
    ws = wb.Worksheets(SHEET_NAME)

    last_row = write_envs_to_excel(ws, envs)
    wb.Save()
    print(f"Excel actualizado ({SHEET_NAME}!D{FIRST_DATA_ROW}:D{last_row}).")

    outlook = win32.Dispatch("Outlook.Application")
    target_mail = get_open_mail_for_reply(outlook)

    reply = create_reply(target_mail)
    reply.Display()

    inspector = reply.GetInspector
    doc = inspector.WordEditor
    selection = doc.Application.Selection

    selection.HomeKey(wdStory)
    selection.TypeText("Hi team, please find the current status on today's COM environments:")
    selection.TypeParagraph()
    selection.TypeParagraph()

    ws.Range(f"D{HEADER_ROW}:G{last_row}").Copy()
    selection.Paste()
    selection.TypeParagraph()

    insert_signature(selection)

    remove_com_block_from_daily_envs(ntt_config.DAILY_ENVS_PATH, envs)

    print("Mail listo con la tabla de COM envs y la firma.")


if __name__ == "__main__":
    main()
