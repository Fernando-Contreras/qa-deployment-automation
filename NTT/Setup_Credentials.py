"""
Guarda / actualiza tus credenciales cifradas (una sola vez).

    py Setup_Credentials.py            -> menú con todas
    py Setup_Credentials.py jenkins    -> solo esa
    py Setup_Credentials.py --list     -> ver cuáles ya están guardadas

Nada se escribe en los .py: se cifra con DPAPI de Windows y solo tu
usuario de Windows en esta máquina puede descifrarlo.
"""

import getpass
import sys

from nc_credentials import (
    STORE_PATH,
    delete_credentials,
    describe_credentials,
    list_credentials,
    save_credentials,
)

# Cuentas que usan los scripts de esta carpeta.
ACCOUNTS = {
    "jenkins": "SDCI / SNAPCI CI (Track_Deployments, MailQAs, MailCOMs)",
    "nttests": "NTTTESTs / Resolve (tests_user)",
    "putty_sit1": "PuTTY sesión SIT1 (TS_Sanity)",
    "putty_atp_sit": "PuTTY sesión ATP_SIT_Step2 / túnel SOCKS (TS_Sanity)",
    "atp_sit_web": "Login del ATP de SIT dentro del navegador (TS_Sanity)",
    "jira": "TMS / Jira (Deployment_Defect; PIM, AM y Excel_Screenshots de GCI)",
    "toms": "TOMS de GCI (PIM_Validation, AM_Validation)",
    "gci_unix": "Servidor Linux de GCI: PuTTY y dzdo (GCIBilling)",
}


def ask_and_save(name: str) -> None:
    print(f"\n🔐 {name} — {ACCOUNTS.get(name, 'cuenta')}")
    username = input("   Usuario: ").strip()
    if not username:
        print("   ⏭ Saltado (usuario vacío)")
        return

    password = getpass.getpass("   Contraseña (no se ve al escribir): ")
    if not password:
        print("   ⏭ Saltado (contraseña vacía)")
        return

    save_credentials(name, username, password)


def main() -> None:
    args = [a for a in sys.argv[1:]]

    if "--list" in args or "-l" in args:
        print(f"Archivo cifrado: {STORE_PATH}\n")
        rows = describe_credentials()

        if not rows:
            print("(ninguna guardada todavía)")
            return

        print(f"{'CUENTA':<16} {'USUARIO':<20} {'CONTRASEÑA':<12} DÓNDE SE USA")
        for name, username, masked in rows:
            print(f"{name:<16} {username:<20} {masked:<12} {ACCOUNTS.get(name, '')}")

        faltan = [n for n in ACCOUNTS if n not in {r[0] for r in rows}]
        if faltan:
            print(f"\nSin guardar: {', '.join(faltan)}")
        return

    if "--delete" in args:
        idx = args.index("--delete")
        if idx + 1 >= len(args):
            print("Uso: py Setup_Credentials.py --delete <nombre>")
            return
        name = args[idx + 1]
        print("🗑 Borrada." if delete_credentials(name) else f"No existe '{name}'.")
        return

    names = [a for a in args if not a.startswith("-")] or list(ACCOUNTS)

    already = set(list_credentials())
    for name in names:
        if name in already and len(names) > 1:
            answer = input(f"\n'{name}' ya está guardada. ¿Cambiarla? (y/n): ").strip().lower()
            if answer != "y":
                continue
        ask_and_save(name)

    print(f"\n✅ Listo. Archivo cifrado: {STORE_PATH}")


if __name__ == "__main__":
    main()
