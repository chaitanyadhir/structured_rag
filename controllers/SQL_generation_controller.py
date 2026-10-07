"""Endpoint for generating SQL from a question and schema metadata."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from pipelines.SQL_generation import SQLGenerationPipeline

router = APIRouter(prefix="/sql", tags=["sql"])


class SQLGenerationRequest(BaseModel):
	model_config = ConfigDict(extra="forbid")

	question: str


@router.post("/generate")
async def generate_sql(request: SQLGenerationRequest):
	"""Generate SQL for the supplied natural-language question."""
	try:
		pipeline = SQLGenerationPipeline(request.question)
		return {"sql": await pipeline.run()}
	except ValueError as exc:
		raise HTTPException(status_code=422, detail=str(exc)) from exc
	except Exception as exc:
		raise HTTPException(status_code=502, detail=f"SQL generation failed: {exc}") from exc
