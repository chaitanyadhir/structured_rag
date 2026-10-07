"""ENDPOINTS: generate and read LLM metadata for the tables of the final database."""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine

import config
from tools.LLM_provider import LLMConfigError, LLMError
from tools.metadata_generator import MetadataGenerator, save_metadata

router = APIRouter(prefix="/metadata", tags=["metadata"])


@router.post("/generate")
def generate_metadata():
    """Generate metadata for every table in the final DB (1 LLM call per table) and save it."""
    engine = create_engine(config.FINAL_URL)
    try:
        if engine.dialect.name == "sqlite" and not Path(engine.url.database).exists():
            raise HTTPException(409, "Final database not built yet. Call POST /relations/build first.")
        gen = MetadataGenerator(engine)
        if not gen.list_tables():
            raise HTTPException(409, "Final database has no tables.")
        try:
            result = gen.generate_all()
        except LLMConfigError as e:
            raise HTTPException(503, f"LLM is not configured: {e}")
        except LLMError as e:
            raise HTTPException(502, str(e))
    finally:
        engine.dispose()
    result["saved_to"] = str(save_metadata(result))
    return result


@router.get("")
def get_saved_metadata():
    """Return the metadata saved by the last POST /metadata/generate."""
    if not config.METADATA_PATH.exists():
        raise HTTPException(404, "No metadata yet. Call POST /metadata/generate first.")
    return json.loads(config.METADATA_PATH.read_text(encoding="utf-8"))