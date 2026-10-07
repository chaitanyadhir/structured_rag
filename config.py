import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Staging DB: one raw table per Excel sheet, no constraints yet.
STAGING_URL = os.getenv("STAGING_URL", f"sqlite:///{DATA_DIR / 'staging.db'}")
# Final DB: same tables, rebuilt with PKs + FKs.
FINAL_URL = os.getenv("FINAL_URL", f"sqlite:///{DATA_DIR / 'final.db'}")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
ALLOWED_EXTENSIONS = {".xlsx", ".xlsm"}