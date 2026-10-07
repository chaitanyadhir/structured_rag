"""STEP 1 ENDPOINTS: upload Excel files, list / drop staging tables."""
from fastapi import APIRouter, File, HTTPException, UploadFile
from pathlib import Path

import config
from tools.excel_ingest import ExcelIngestor

router = APIRouter(prefix="/uploads", tags=["uploads"])
ingestor = ExcelIngestor(config.STAGING_URL)


@router.post("")
async def upload_excels(files: list[UploadFile] = File(...)):
    """Upload one or many Excel files. Each sheet becomes one staging table.
    Re-uploading a file with the same name replaces its tables."""
    ok, failed = [], []
    for f in files:
        if Path(f.filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
            failed.append({"file": f.filename, "error": "Only .xlsx / .xlsm supported"})
            continue
        data = await f.read()
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            failed.append({"file": f.filename, "error": f"Larger than {config.MAX_UPLOAD_MB} MB"})
            continue
        try:
            import io
            ok.extend(r.__dict__ for r in ingestor.ingest(f.filename, io.BytesIO(data)))
        except ValueError as e:
            failed.append({"file": f.filename, "error": str(e)})
    if not ok and failed:
        raise HTTPException(400, detail=failed)
    return {"tables_created": ok, "failed": failed}


@router.get("/tables")
def list_tables():
    return ingestor.list_tables()


@router.delete("/tables/{table_name}")
def drop_table(table_name: str):
    if not ingestor.drop_table(table_name):
        raise HTTPException(404, f"No such staging table: {table_name}")
    return {"dropped": table_name}