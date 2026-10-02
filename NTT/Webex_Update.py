"""
Webex_Update.py

Compara tus bloques de Daily_Envs.txt con los mensajes que TU mandaste HOY
al chat de Webex "QA Build installation":
  - Si el bloque ya tiene mensaje de hoy y cambio -> EDITA ese mensaje
    en su lugar (queda "(edited)", conserva hora y posicion).
  - Si el bloque NO tiene mensaje de hoy todavia   -> lo MANDA como
    mensaje nuevo (en el mismo orden en que aparecen en el archivo).

Correr el script = aplicar los cambios. No pregunta nada.
Si solo quieres ver que cambiaria sin tocar Webex:  py Webex_Update.py --dry-run

--------------------------------------------------------------------
TOKEN DE WEBEX (necesario)
--------------------------------------------------------------------
Opcion recomendada, UNA SOLA VEZ (el token se renueva solo despues):
    py Webex_Update.py --setup-oauth
Te pide crear una Integration en developer.webex.com (2 min), abre el
navegador para que inicies sesion, y ya. No lo vuelves a hacer.

Opcion rapida, sin registrar nada (dura 12 h, hay que repetirla):
  El script busca un Personal Access Token en este orden:
  1. --token PEGA_AQUI
  2. archivo  token_webex.txt  (junto a este script)
  3. variable de entorno  WEBEX_ACCESS_TOKEN
  4. credencial cifrada "webex" (py Setup_Credentials.py webex)

--------------------------------------------------------------------
USO
--------------------------------------------------------------------
    py Webex_Update.py --setup-oauth   (una sola vez)
    py Webex_Update.py                 (compara y edita lo que cambio)
    py Webex_Update.py --dry-run       (solo ensena diffs, no edita)
    py Webex_Update.py --room "..."    (otra sala)
    py Webex_Update.py --file "C:\\ruta\\Daily_Envs.txt"
"""

import argparse
import difflib
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

try:
    from nc_credentials import get_credentials, save_credentials
except Exception:
    get_credentials = None
    save_credentials = None

import ntt_config

API = "https://webexapis.com/v1"
DEFAULT_ROOM = "QA Build installation"
DEFAULT_DAILY_ENVS = str(ntt_config.DAILY_ENVS_PATH)
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token_webex.txt")

# --- OAuth: para que el token se renueve solo (no dura solo 12 h) ---
OAUTH_PORT = 8733
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_PORT}/callback"
OAUTH_SCOPES = "spark:messages_read spark:messages_write spark:rooms_read spark:people_read"
OAUTH_AUTHORIZE_URL = "https://webexapis.com/v1/authorize"
OAUTH_TOKEN_URL = "https://webexapis.com/v1/access_token"


# ======================================================================
# Token
# ======================================================================
def _clean(raw: str) -> str:
    """Quita espacios, saltos de linea, comillas y el prefijo 'Bearer '."""
    t = (raw or "").strip().strip('"').strip("'").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return "".join(t.split())


def _oauth_app_creds():
    """(client_id, client_secret) de la Integration registrada en developer.webex.com."""
    cid = os.environ.get("WEBEX_CLIENT_ID", "").strip()
    csec = os.environ.get("WEBEX_CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec
    if get_credentials is not None:
        try:
            cid, csec = get_credentials("webex_app", prompt=False)
            if cid and csec:
                return cid, csec
        except Exception:
            pass
    return None, None


def _oauth_token_request(client_id, client_secret, **extra) -> dict:
    body = {"client_id": client_id, "client_secret": client_secret, **extra}
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"[warn] OAuth: Webex rechazo el intercambio de token: {detail}")
        return {"error": detail}
    except urllib.error.URLError as exc:
        print(f"[warn] OAuth: no se pudo conectar a Webex para renovar el token: {exc}")
        return {"error": str(exc)}


def _save_oauth_tokens(tokens: dict):
    expires_at = time.time() + int(tokens.get("expires_in", 0)) - 60
    payload = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": expires_at,
    }
    save_credentials("webex_oauth", "oauth", json.dumps(payload))


def setup_oauth():
    """Flujo de una sola vez: registra tu Integration y queda renovando el token solo."""
    if get_credentials is None or save_credentials is None:
        print("[error] Necesito nc_credentials.py (esta junto a este script) para guardar tokens.")
        sys.exit(1)

    print(
        "\n--- Configuracion unica de OAuth (para que el token se renueve solo) ---\n"
        "1. Ve a https://developer.webex.com/my-apps/new/integration\n"
        "2. Ponle un nombre (ej. 'Webex_Update NTT') y estos datos:\n"
        f"     Redirect URI:  {OAUTH_REDIRECT_URI}\n"
        f"     Scopes:        {OAUTH_SCOPES}\n"
        "3. Al crearla, copia el 'Client ID' y el 'Client Secret'.\n"
    )
    client_id, client_secret = _oauth_app_creds()
    if not (client_id and client_secret):
        client_id, client_secret = get_credentials("webex_app", prompt=True)
    else:
        save_credentials("webex_app", client_id, client_secret)

    params = {
        "client_id": client_id, "response_type": "code", "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES, "state": "webex_update",
    }
    auth_url = f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            result.update(urllib.parse.parse_qs(qs))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body>Listo, ya puedes cerrar esta pestana.</body></html>".encode("utf-8"))

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", OAUTH_PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"Abriendo el navegador para que inicies sesion en Webex...\n{auth_url}\n")
    webbrowser.open(auth_url)

    waited = 0
    while not result and waited < 120:
        time.sleep(1)
        waited += 1
    server.server_close()

    if "code" not in result:
        print("[error] No llego el 'code' de Webex (¿cerraste la pestana o tardaste mas de 2 min?). Intenta de nuevo.")
        sys.exit(1)

    tokens = _oauth_token_request(
        client_id, client_secret,
        grant_type="authorization_code", code=result["code"][0], redirect_uri=OAUTH_REDIRECT_URI,
    )
    if "access_token" not in tokens:
        print(f"[error] Webex no regreso un access_token: {tokens}")
        sys.exit(1)

    _save_oauth_tokens(tokens)
    print("\nListo: el token de Webex ahora se renueva solo. Ya puedes correr el script normal.")


def _read_oauth_credential():
    """Lee la credencial 'webex_oauth' con reintentos: el archivo cifrado a veces
    se topa con un lock momentaneo (antivirus, OneDrive, etc.)."""
    last_exc = None
    for attempt in range(3):
        try:
            _u, raw = get_credentials("webex_oauth", prompt=False)
            return raw
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.4)
    raise last_exc


def _get_oauth_token():
    """Si ya se corrio --setup-oauth, regresa un access_token vigente (renovandolo si hace falta)."""
    if get_credentials is None:
        return None
    try:
        raw = _read_oauth_credential()
    except Exception as exc:
        print(f"[warn] OAuth: no se pudo leer la credencial guardada tras 3 intentos ({exc}). Corre --setup-oauth de nuevo.")
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        print(f"[warn] OAuth: la credencial guardada esta corrupta ({exc}). Corre --setup-oauth de nuevo.")
        return None

    if time.time() < data.get("expires_at", 0):
        return data["access_token"]

    client_id, client_secret = _oauth_app_creds()
    if not (client_id and client_secret):
        print("[warn] OAuth: el token de acceso expiro y no encuentro el Client ID/Secret para renovarlo.")
        return None
    if not data.get("refresh_token"):
        print("[warn] OAuth: el token de acceso expiro y no hay refresh_token guardado. Corre --setup-oauth de nuevo.")
        return None

    tokens = _oauth_token_request(
        client_id, client_secret,
        grant_type="refresh_token", refresh_token=data["refresh_token"],
    )
    if "access_token" not in tokens:
        print(f"[warn] OAuth: Webex no me dio un access_token nuevo al renovar: {tokens}")
        return None
    tokens.setdefault("refresh_token", data["refresh_token"])
    _save_oauth_tokens(tokens)
    return tokens["access_token"]


def get_token(cli_token=None):
    """Devuelve (token, de_donde_salio)."""
    if cli_token:
        return _clean(cli_token), "--token"

    oauth_token = _get_oauth_token()
    if oauth_token:
        return oauth_token, "OAuth (auto-renovado)"

    if os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            t = _clean(fh.read())
        if t:
            return t, f"archivo {os.path.basename(TOKEN_FILE)}"

    env = _clean(os.environ.get("WEBEX_ACCESS_TOKEN", ""))
    if env:
        return env, "variable WEBEX_ACCESS_TOKEN"

    if get_credentials is not None:
        try:
            user, pwd = get_credentials("webex", default_username="token", prompt=False)
            for cand, where in ((pwd, "password"), (user, "usuario")):
                t = _clean(cand)
                if len(t) > 40:
                    return t, f"credencial 'webex' ({where})"
        except Exception as exc:
            print(f"[warn] No se pudo leer la credencial 'webex': {exc}")

    print(
        "\nNo hay token de Webex.\n"
        "  Opcion recomendada (una sola vez, no vuelve a fallar):\n"
        "      py Webex_Update.py --setup-oauth\n"
        "  Opcion rapida (dura 12 h, hay que repetirla):\n"
        f"      pega tu Personal Access Token en {TOKEN_FILE}\n"
        "      (lo sacas de https://developer.webex.com/docs/getting-started)\n"
        "      o pasalo con:  py Webex_Update.py --token EL_TOKEN\n"
    )
    sys.exit(1)


# ======================================================================
# HTTP a la API de Webex
# ======================================================================
def api_request(method: str, path: str, token: str, params=None, body=None) -> dict:
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            print("[error] Webex rechazo el token (401).")
            print(f"        token: largo={len(token)}, empieza='{token[:8]}...', termina='...{token[-6:]}'")
            print(f"        respuesta: {detail}")
            print("        -> casi siempre es que ya expiro (duran 12 h). Saca uno nuevo.")
        else:
            print(f"[error] HTTP {exc.code} en {method} {path}: {detail}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"[error] No se pudo conectar a Webex: {exc}")
        sys.exit(1)


def api_get(path, token, params=None):
    return api_request("GET", path, token, params=params)


def get_my_id(token: str) -> str:
    return api_get("people/me", token)["id"]


def find_room_id(token: str, room_name: str) -> str:
    resp = api_get("rooms", token, {"type": "group", "sortBy": "lastactivity", "max": 1000})
    rooms = resp.get("items", [])

    want = room_name.strip().lower()
    exact = [r for r in rooms if r.get("title", "").strip().lower() == want]
    partial = [r for r in rooms if want in r.get("title", "").strip().lower()]
    match = exact or partial
    if not match:
        print(f"[error] No encontre ninguna sala '{room_name}'. Salas disponibles:")
        for r in rooms[:40]:
            print(f"          - {r.get('title')}")
        sys.exit(1)
    if len(match) > 1:
        print(f"[warn] Varias salas coinciden con '{room_name}'; uso la primera: {match[0].get('title')!r}")

    chosen = match[0]
    print(f"[ok] Sala: {chosen.get('title')!r}  (id ...{chosen['id'][-10:]})")
    return chosen["id"]


def my_messages_since(token, room_id, my_id, since_utc):
    """Mensajes propios de la sala con created >= since_utc, del mas viejo al mas nuevo."""
    params = {"roomId": room_id, "max": 100}
    mine = []
    while True:
        items = api_get("messages", token, params).get("items", [])
        if not items:
            break
        stop = False
        for m in items:
            created = datetime.fromisoformat(m["created"].replace("Z", "+00:00"))
            if created < since_utc:
                stop = True
                break
            if m.get("personId") == my_id:
                mine.append(m)
        if stop or len(items) < params["max"]:
            break
        params["beforeMessage"] = items[-1]["id"]
    mine.sort(key=lambda m: m["created"])
    return mine


# ======================================================================
# Bloques de Daily_Envs.txt
# ======================================================================
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
ENV_RE = re.compile(r"\b(?:NTTCOM|NTTCW|COM)?\s*0*(\d+)\s*QA\b", re.I)


def strip_md(line: str) -> str:
    line = LINK_RE.sub(r"\1", line)              # [texto](url) -> texto
    line = line.replace("**", "").replace("__", "")
    line = re.sub(r"^\s*#{1,6}\s*", "", line)    # quita '## '
    return line.strip()


def looks_like_header(line: str) -> bool:
    s = line.strip()
    return s.startswith("#") and "**" in s


def parse_blocks(path: str):
    """Cada bloque va de un encabezado '## **...**' al siguiente."""
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    starts = [i for i, ln in enumerate(lines) if looks_like_header(ln)]
    blocks = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        block = build_block(lines[start:end])
        if block["envs"]:          # ignora bloques-plantilla sin envs
            blocks.append(block)
    return blocks


def build_block(raw_lines):
    """Recorta la cola 'Environment lists:' + file://, y arma las vistas del bloque.

    webex_md = lo que se manda a Webex:
        ## **Titulo**
        ---
        <cuerpo: PASSED / IN PROGRESS / ...>
    (el '---' bajo el titulo se agrega siempre, para que el titulo se vea grande)
    """
    body = []
    for ln in raw_lines:
        if ln.strip().lower().startswith("environment lists:"):
            break
        body.append(ln)

    header_line = body[0].rstrip() if body else ""
    if not header_line.lstrip().startswith("#"):
        header_line = "## " + header_line.strip()

    rest = []
    for ln in body[1:]:
        if ln.strip() == "---":
            continue
        rest.append(ln.rstrip())
    while rest and not rest[0].strip():
        rest.pop(0)
    while rest and not rest[-1].strip():
        rest.pop()

    webex_md = collapse_blanks(header_line + "\n---\n" + "\n".join(rest))

    norm = normalize(webex_md)
    header = norm[0] if norm else ""
    envs = env_set("\n".join(body))
    return {"header": header, "raw_lines": raw_lines, "webex_md": webex_md,
            "norm": norm, "envs": envs}


def collapse_blanks(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize(text: str):
    """Lista de lineas en texto plano, sin markdown, sin blancos, sin '---'.
    Es la forma que se compara (asi los links y el '---' no cuentan como diferencia)."""
    out = []
    for ln in (text or "").splitlines():
        if ln.strip() == "---":
            continue
        s = strip_md(ln)
        if s:
            out.append(s)
    return out


def env_set(text: str):
    return frozenset(int(n) for n in ENV_RE.findall(text or ""))


# ======================================================================
# Emparejado bloque <-> mensaje
# ======================================================================
def match_blocks_to_messages(blocks, messages):
    """Devuelve (pares, bloques_sueltos, mensajes_sueltos).
    pares = lista de (block, msg, como)."""
    msg_info = []
    for m in messages:
        txt = m.get("markdown") or m.get("text") or ""
        n = normalize(txt)
        msg_info.append({"msg": m, "norm": n,
                         "header": (n or [""])[0].lower(),
                         "envs": env_set(txt)})

    pares = []
    used_msg = set()
    used_blk = set()

    # Pase 1: por conjunto de envs (exacto y unico)
    for bi, b in enumerate(blocks):
        cands = [mi for mi, minfo in enumerate(msg_info)
                 if mi not in used_msg and minfo["envs"] and minfo["envs"] == b["envs"]]
        if len(cands) > 1:
            cands = [mi for mi in cands if msg_info[mi]["header"] == b["header"].lower()] or cands
        if len(cands) == 1:
            mi = cands[0]
            pares.append((b, msg_info[mi]["msg"], "envs"))
            used_msg.add(mi)
            used_blk.add(bi)

    # Pase 2: por encabezado igual (entre los que quedan)
    for bi, b in enumerate(blocks):
        if bi in used_blk:
            continue
        cands = [mi for mi, minfo in enumerate(msg_info)
                 if mi not in used_msg and minfo["header"] and minfo["header"] == b["header"].lower()]
        if len(cands) == 1:
            mi = cands[0]
            pares.append((b, msg_info[mi]["msg"], "encabezado"))
            used_msg.add(mi)
            used_blk.add(bi)

    # Pase 3: por orden, solo si quedan igual cantidad de cada lado
    rest_blk = [bi for bi in range(len(blocks)) if bi not in used_blk]
    rest_msg = [mi for mi in range(len(msg_info)) if mi not in used_msg]
    if rest_blk and len(rest_blk) == len(rest_msg):
        for bi, mi in zip(rest_blk, rest_msg):
            pares.append((blocks[bi], msg_info[mi]["msg"], "orden (menos seguro)"))
            used_msg.add(mi)
            used_blk.add(bi)

    sueltos_blk = [blocks[bi] for bi in range(len(blocks)) if bi not in used_blk]
    sueltos_msg = [msg_info[mi]["msg"] for mi in range(len(msg_info)) if mi not in used_msg]
    return pares, sueltos_blk, sueltos_msg


# ======================================================================
# Main
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="Compara Daily_Envs.txt con tus mensajes de hoy en Webex y edita los que cambiaron.")
    ap.add_argument("--room", default=DEFAULT_ROOM)
    ap.add_argument("--file", default=DEFAULT_DAILY_ENVS, help="Ruta a Daily_Envs.txt")
    ap.add_argument("--days", type=int, default=1, help="Dias hacia atras (1 = solo hoy)")
    ap.add_argument("--token", default=None)
    ap.add_argument("--dry-run", action="store_true", help="Solo ensena diffs, no edita nada")
    ap.add_argument("--setup-oauth", action="store_true",
                    help="Configura una sola vez para que el token se renueve solo (no expire cada 12h)")
    args = ap.parse_args()

    if args.setup_oauth:
        setup_oauth()
        return

    if not os.path.isfile(args.file):
        print(f"[error] No existe el archivo: {args.file}")
        sys.exit(1)

    token, source = get_token(args.token)
    print(f"[info] Token de: {source}")

    blocks = parse_blocks(args.file)
    print(f"[info] {len(blocks)} bloque(s) con envs en {os.path.basename(args.file)}")

    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if args.days > 1:
        start_local -= timedelta(days=args.days - 1)
    since_utc = start_local.astimezone(timezone.utc)
    print(f"[info] Mensajes tuyos desde {start_local:%Y-%m-%d %H:%M %Z}")

    my_id = get_my_id(token)
    room_id = find_room_id(token, args.room)
    messages = my_messages_since(token, room_id, my_id, since_utc)
    print(f"[info] {len(messages)} mensaje(s) tuyo(s) hoy en la sala")

    pares, sueltos_blk, sueltos_msg = match_blocks_to_messages(blocks, messages)

    cambios = []   # (block, msg)
    print("\n" + "=" * 72)
    for b, m, como in pares:
        created = datetime.fromisoformat(m["created"].replace("Z", "+00:00")).astimezone()
        etiqueta = b["header"] or "(sin encabezado)"
        msg_norm = normalize(m.get("markdown") or m.get("text") or "")
        if b["norm"] == msg_norm:
            print(f"[= igual]   {etiqueta}   (match por {como}, msg {created:%H:%M})")
        else:
            print(f"[~ CAMBIO]  {etiqueta}   (match por {como}, msg {created:%H:%M})")
            for d in difflib.unified_diff(msg_norm, b["norm"],
                                          fromfile="webex (ahora)", tofile="daily_envs (nuevo)",
                                          lineterm=""):
                print("      " + d)
            cambios.append((b, m))
    print("=" * 72)

    for b in sueltos_blk:
        print(f"[+ NUEVO]   {b['header']}   -> no hay mensaje de hoy, se manda como mensaje nuevo")
    for m in sueltos_msg:
        first = (normalize(m.get('markdown') or m.get('text') or '') or ['(vacio)'])[0]
        print(f"[i sin match] mensaje de hoy sin bloque: {first!r}")

    if not cambios and not sueltos_blk:
        print("\nNada que actualizar. Todo coincide.")
        return

    if args.dry_run:
        print(f"\n{len(cambios)} mensaje(s) cambiarian y {len(sueltos_blk)} se mandarian nuevos. (--dry-run: no se toco Webex)")
        return

    if cambios:
        print(f"\nEditando {len(cambios)} mensaje(s) en {args.room!r}...")
        for b, m in cambios:
            api_request("PUT", f"messages/{m['id']}", token,
                        body={"roomId": room_id, "markdown": b["webex_md"]})
            print(f"[ok] editado: {b['header']}")

    if sueltos_blk:
        print(f"\nMandando {len(sueltos_blk)} mensaje(s) nuevo(s) en {args.room!r} (en orden del archivo)...")
        for b in sueltos_blk:
            api_request("POST", "messages", token,
                        body={"roomId": room_id, "markdown": b["webex_md"]})
            print(f"[ok] mandado: {b['header']}")

    print("\nListo.")


if __name__ == "__main__":
    main()
