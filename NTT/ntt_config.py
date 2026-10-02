"""
Rutas y servidores que comparten los scripts de NTT.

Antes cada script traía su propia copia de "C:\\Users\\...\\Daily_Envs.txt" y
de los links de Jenkins; ahora salen de aquí. Si algún día cambia la carpeta
de notas, se cambia en un solo lugar, o sin tocar código con la variable de
entorno NTT_NOTES_DIR (así corren también las pruebas y cualquier otra máquina).
"""

import os
from pathlib import Path

# -------------------------
# ARCHIVOS
# -------------------------
NOTES_DIR = Path(os.environ.get("NTT_NOTES_DIR") or Path.home() / "Documents" / "NTT" / "Notes")

# El archivo del que depende todo: los bloques de deployments del día.
DAILY_ENVS_PATH = NOTES_DIR / "Daily_Envs.txt"

# Track_Deployments -> E2E cuando corre sin terminal (schtasks no pasa argumentos).
PENDING_SANITY_ENVS_FILE = NOTES_DIR / "pending_sanity_envs.txt"

# Bitácora de las corridas automáticas de Track_Deployments.
TRACK_DEPLOYMENTS_LOG = NOTES_DIR / "Track_Deployments_scheduled.log"

# Reporte de patches que MailQAs arma antes de llenar el Excel.
PATCH_STATUS_LOG = NOTES_DIR / "patch_status_log.txt"

# Excel del Quality Gate que MailQAs y MailCOMs llenan para el correo.
QG_SCOPE_ANALYSIS_PATH = Path.home() / "Documents" / "NTT" / "QG Scope Analysis.xlsx"

# OJO: SBA.py escribe en OTRO libro con el mismo nombre, uno nivel arriba
# (pestaña "SBA"). Así estaba desde antes; se deja igual a propósito.
SBA_QG_SCOPE_ANALYSIS_PATH = Path.home() / "Documents" / "QG Scope Analysis.xlsx"

# Carpetas con los Excel partidos que Divided_SBA.py manda uno por pestaña.
SBA_ROOT = Path.home() / "Documents" / "NTT" / "SBA"

# -------------------------
# SERVIDORES
# -------------------------
SDCI = "https://sdci.example.com"   # Deploy-Parallel (SD files)
SNAPCI = "https://snapci.example.com"   # Restore / Create Eatlon Snapshot
JENKINS_FOLDER = "/job/SD.PROJECT/"

JIRA_BASE = "https://jira.example.com"

# Usuario que se sugiere la primera vez que se piden las credenciales de Jenkins.
JENKINS_DEFAULT_USER = "your_user"
