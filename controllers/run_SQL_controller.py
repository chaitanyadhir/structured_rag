import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config

router = APIRouter(prefix="/sql_run", tags=["sql"])
DB_PATH = Path(config.FINAL_URL.replace("sqlite:///", "", 1))   # was a hardcoded C:\ path
MAX_ROWS = int(os.getenv("MAX_RESULT_ROWS", "5000"))


class SQLRequest(BaseModel):
    sql: str


@router.post("/run-sql")
def run_sql(request: SQLRequest):
    """Run a read-only SQL query and return column names and result rows."""
    statement = request.sql.strip()
    # Remove any metadata JSON appended after the SQL query.
    statement = statement.split("\n{", 1)[0].strip()
    if not statement:
        raise HTTPException(status_code=400, detail="SQL statement cannot be empty")

    try:
        connection = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(statement)
            if cursor.description is None:
                raise HTTPException(status_code=400, detail="A query statement is required")
            return {
                "columns": [column[0] for column in cursor.description],
                "rows": [dict(row) for row in cursor.fetchmany(MAX_ROWS)],
            }
        finally:
            connection.close()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc