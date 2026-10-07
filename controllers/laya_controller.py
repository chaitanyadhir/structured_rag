"""Endpoints for selecting relevant metadata tables and columns."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tools.table_column_selection import TableColumnSelector

router = APIRouter(prefix="/metadata", tags=["metadata"])
selector = TableColumnSelector()


class TableColumnSelectionRequest(BaseModel):
	question: str


@router.post("/select")
async def select_table_columns(request: TableColumnSelectionRequest):
	"""Select relevant tables and columns from the metadata catalog."""
	try:
		return await selector.select(request.question)
	except ValueError as error:
		raise HTTPException(status_code=400, detail=str(error)) from error
