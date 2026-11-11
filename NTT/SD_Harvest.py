"""
SD_Harvest: lee los correos "Solution Descriptor from Jenkins" que tengas
ABIERTOS en Outlook y pone la ruta del SD en su bloque de Daily_Envs.txt.

Como se decide a que bloque va cada correo:
  1. Por el CI del asunto ("nttcw99_ci" -> bloque "T2.3.54 - 99CI",
     "nttcom23_ci" -> bloque "CI_23").
  2. Si el asunto no ayuda, por la version del yaml (...-T2.3.54.yaml)
     contra el nombre del bloque.
Si un correo no calza con ningun bloque, o calza con varios, se avisa y no se toca.

La linea del SD va al final de la seccion "Environment lists:" del bloque.
Si el bloque ya traia una linea file://, se reemplaza (si era distinta).

Uso:
    python SD_Harvest.py            # escribe Daily_Envs.txt
    python SD_Harvest.py --dry-run  # solo muestra que haria
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import ntt_config

SUBJECT_RE = re.compile(r"Solution Descriptor", re.IGNORECASE)
SD_LINK_RE = re.compile(r"file:[/\\]{2,}[^\s<>\"']+?\.ya?ml", re.IGNORECASE)
SD_ATTACH_RE = re.compile(r"\.ya?ml$", re.IGNORECASE)
SUBJECT_CI_RE = re.compile(r"ntt(?:cw|com)?(\d+)_ci", re.IGNORECASE)
SD_VERSION_RE = re.compile(r"-((?:T[\d.]+|CI_\d+)[^-]*)\.ya?ml$", re.IGNORECASE)

BLOCK_HEADER_RE = re.compile(
    r"^#{0,6}\s*\*\*(?:T[\d.]+|CI_\d+|\d+CI\b).*?\*\*\s*$", re.MULTILINE
)
ENV_LISTS_RE = re.compile(r"^\s*Environment lists:", re.IGNORECASE)
HEADER_TOKEN_RE = re.compile(r"\*\*\s*(T[\d.]+|CI_\d+|\d+CI)\b", re.IGNORECASE)


# -------------------------
# Parseo de un correo
# -------------------------
def normalize_sd_path(raw):
    """file:\\\\host\\x -> file://host/x (como ya va en el Daily_Envs)."""
    path = raw.strip().replace("\\", "/")
    return "file://" + re.sub(r"^file:/+", "", path, flags=re.IGNORECASE)


def parse_sd_mail(subject, body, attachment_names=()):
    """-> dict(ci, version, sd_path, subject) o None si no es un correo de SD."""
    if not SUBJECT_RE.search(subject or ""):
        return None

    link = SD_LINK_RE.search(body or "")
    sd_path = normalize_sd_path(link.group(0)) if link else None
    if not sd_path:
        return None

    filename = sd_path.rsplit("/", 1)[-1]
    version = SD_VERSION_RE.search(filename)
    ci = SUBJECT_CI_RE.search(subject)
    return {
        "subject": subject.strip(),
        "ci": ci.group(1) if ci else None,
        # "T2.3.54.0" en el nombre se trata como "T2.3.54": el contenido y los
        # bloques usan la version sin los .0 del final.
        "version": re.sub(r"(\.0)+$", "", version.group(1)) if version else None,
        "sd_path": sd_path,
    }


# -------------------------
# Verificacion: el nombre del SD debe coincidir con su contenido
# -------------------------
MIN_VERSION_HITS = 3  # un SD bueno tiene MAS de 3 menciones de su version


def verify_sd_content(version, content):
    """-> (ok, detalle). Un SD es bueno si la version de su nombre aparece
    mas de 3 veces dentro del yaml Y al menos una linea de rbm la lleva
    (un SD malo solo tiene las 3 menciones de siempre y su rbm es de otra version)."""
    if not version:
        return False, "no pude sacar la version del nombre del SD"
    if not content:
        return False, "no pude leer el contenido del SD"

    # Tambien cuenta "T2.3.54.0" como T2.3.54, pero no "T2.3.541" ni "T2.3.54.1".
    pattern = re.compile(rf"(?<![\d.]){re.escape(version)}(?:\.0)*(?!\.?\d)", re.IGNORECASE)
    hits = len(pattern.findall(content))
    rbm_ok = any(
        "rbm" in line.lower() and pattern.search(line) for line in content.splitlines()
    )
    detail = f"{hits} coincidencias con {version}, rbm con {version}: {'si' if rbm_ok else 'no'}"
    return (hits > MIN_VERSION_HITS and rbm_ok), detail


def unc_path(sd_path):
    """file://host/a/b.yaml -> \\\\host\\a\\b.yaml"""
    return "\\\\" + re.sub(r"^file:/+", "", sd_path).replace("/", "\\")


def read_sd_content(sd_path, attachments):
    """Contenido del yaml: del adjunto del correo; si no, de la ruta de red."""
    import tempfile

    for att in attachments:
        if SD_ATTACH_RE.search(att.FileName):
            try:
                tmp = Path(tempfile.mkdtemp(prefix="sd_harvest_")) / att.FileName
                att.SaveAsFile(str(tmp))
                return tmp.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"  (no pude leer el adjunto {att.FileName}: {exc})")
    try:
        return Path(unc_path(sd_path)).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"  (no pude leer {sd_path}: {exc})")
        return None


# -------------------------
# Bloques de Daily_Envs.txt
# -------------------------
def block_spans(text):
    """[(start, end)] de cada bloque (header incluido)."""
    matches = list(BLOCK_HEADER_RE.finditer(text))
    return [
        (m.start(), matches[i + 1].start() if i + 1 < len(matches) else len(text))
        for i, m in enumerate(matches)
    ]


def block_matches(block_text, mail):
    """Puntaje de que tan bien calza el correo con el bloque (0 = no calza)."""
    header = block_text.splitlines()[0]
    token = HEADER_TOKEN_RE.search(header)
    token = token.group(1).upper() if token else ""
    score = 0

    if mail["ci"]:
        ci = mail["ci"]
        if re.search(rf"\*\*\s*{ci}CI\b|\b{ci}CI\b|CI_{ci}\b", header, re.IGNORECASE):
            score += 2
    if mail["version"] and token and mail["version"].upper() == token:
        score += 1
    return score


def find_target_block(text, mail):
    """Indice (0-based) del bloque que le toca al correo, o (None, motivo)."""
    spans = block_spans(text)
    scored = [(block_matches(text[s:e], mail), i) for i, (s, e) in enumerate(spans)]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None, "ningun bloque calza"
    best = max(score for score, _ in scored)
    winners = [i for score, i in scored if score == best]
    if len(winners) > 1:
        return None, f"calza con {len(winners)} bloques (ambiguo)"
    return winners[0], None


def put_sd_in_block(block_text, sd_path):
    """-> (nuevo_texto_del_bloque, accion) con accion en
    'insertado' | 'reemplazado' | 'igual' | None (no hay 'Environment lists:')."""
    nl = "\r\n" if "\r\n" in block_text else "\n"
    lines = block_text.split(nl)

    start = next((i for i, l in enumerate(lines) if ENV_LISTS_RE.match(l)), None)
    if start is None:
        return block_text, None

    # La seccion llega hasta la primera linea vacia o '---'.
    end = start + 1
    while end < len(lines) and lines[end].strip() and lines[end].strip() != "---":
        end += 1

    for i in range(start + 1, end):
        if lines[i].strip().lower().startswith("file:"):
            if lines[i].strip() == sd_path:
                return block_text, "igual"
            lines[i] = sd_path
            return nl.join(lines), "reemplazado"

    lines.insert(end, sd_path)
    return nl.join(lines), "insertado"


def apply_mails(text, mails):
    """-> (texto_nuevo, [resultados]) sin tocar disco."""
    results = []
    for mail in mails:
        index, why = find_target_block(text, mail)
        label = mail["subject"]
        if index is None:
            results.append((label, f"SIN CAMBIO: {why}"))
            continue
        start, end = block_spans(text)[index]
        header = text[start:end].splitlines()[0].strip()
        new_block, action = put_sd_in_block(text[start:end], mail["sd_path"])
        if action is None:
            results.append((label, f"SIN CAMBIO: '{header}' no tiene 'Environment lists:'"))
            continue
        text = text[:start] + new_block + text[end:]
        results.append((label, f"{action} en '{header}'"))
    return text, results


# -------------------------
# Outlook
# -------------------------
def read_open_mails():
    """Correos de SD de las ventanas de Outlook abiertas."""
    import win32com.client as win32

    outlook = win32.Dispatch("Outlook.Application")
    mails = []
    for i in range(1, outlook.Inspectors.Count + 1):
        item = outlook.Inspectors.Item(i).CurrentItem
        subject = getattr(item, "Subject", "") or ""
        body = getattr(item, "Body", "") or ""
        attachments = list(item.Attachments) if hasattr(item, "Attachments") else []
        mail = parse_sd_mail(subject, body)
        if mail:
            content = read_sd_content(mail["sd_path"], attachments)
            mail["valid"], mail["detail"] = verify_sd_content(mail["version"], content)
            mails.append(mail)
        else:
            print(f"(se ignora, no es un SD): {subject.strip() or '(sin asunto)'}")
    return mails


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="no escribe el archivo")
    args = parser.parse_args(argv)

    path = Path(ntt_config.DAILY_ENVS_PATH)
    raw = path.read_bytes().decode("utf-8")

    mails = read_open_mails()
    if not mails:
        print("No hay correos de SD abiertos en Outlook.")
        return 1

    good = [m for m in mails if m["valid"]]
    bad = [m for m in mails if not m["valid"]]
    for mail in bad:
        print(f"❌ SD MALO, no se pega: {mail['subject']}\n    {mail['sd_path']}\n    {mail['detail']}")
    for mail in good:
        print(f"✔ SD verificado: {mail['subject']} ({mail['detail']})")

    new_text, results = apply_mails(raw, good)
    for label, outcome in results:
        print(f"- {label}\n    {outcome}")

    if new_text != raw and not args.dry_run:
        path.write_bytes(new_text.encode("utf-8"))
        print(f"\nActualizado {path}")
    elif args.dry_run:
        print("\n(dry-run: no se escribio nada)")

    # Siempre al final, haya habido SD malo o no.
    if args.dry_run:
        print("\n(dry-run: aqui se lanzaria QA_Envs.py --f)")
        return 0
    script = Path(__file__).with_name("QA_Envs.py")
    print("\nLanzando QA_Envs.py --f ...")
    return subprocess.run([sys.executable, str(script), "--f"], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
