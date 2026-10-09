"""Generate SQL from a user question and selected schema metadata."""

from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.LLM_provider import llm_call
from tools.table_column_selection import get_selector

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "generate_SQL.txt"


@lru_cache(maxsize=1)
def _load_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class SQLGenerationPipeline:
    """Select relevant schema context and generate a SQL response."""

    def __init__(self, query: str) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        self.query = query.strip()

    async def run(self) -> str:
        selection = await self._select_table_columns()
        table_columns_context = await self._build_context(selection)
        template = _load_template()
        # Avoid str.format(): literal JSON braces in the prompt are treated as
        # replacement fields (for example {"customer_name"}).
        prompt = template.replace("{query_type}", "SQL")
        prompt = prompt.replace("{query}", self.query)
        prompt = prompt.replace("{table_columns_context}", table_columns_context)
        # llm_call is blocking; keep it off the event loop.
        response = await asyncio.to_thread(llm_call, prompt)
        if not isinstance(response, str):
            raise ValueError("SQL generation response must be a string")

        # Return the final query and discard the optional JSON mapping section.
        sql = response.strip()
        # Some providers wrap the generated query in a JSON object.
        try:
            parsed = json.loads(sql)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("sql"), str):
            sql = parsed["sql"].strip()
        else:
            match = re.search(r'"sql"\s*:\s*"((?:\\.|[^"\\])*)"', sql, re.DOTALL)
            if match:
                try:
                    sql = json.loads('"' + match.group(1) + '"').strip()
                except json.JSONDecodeError:
                    pass
        if "<JSON mapping>" in sql:
            sql = sql.split("<JSON mapping>", 1)[0].strip()
        if sql.startswith("<final query>"):
            sql = sql[len("<final query>"):].strip()
        return sql

    async def _select_table_columns(self) -> dict[str, list[str]]:
        # Direct in-process call (was: HTTP POST to our own /metadata/select).
        return await get_selector().select(self.query)

    async def _build_context(self, selection: dict[str, list[str]]) -> str:
        """Include table-level descriptions and only the selected columns."""
        selector = get_selector()
        context: list[dict[str, Any]] = []
        for table, columns in selection.items():
            metadata = self._filter_table_metadata(selector.get_table(table), table, columns)
            context.append(
                {"table": table, "table_metadata": metadata, "columns": columns}
            )
        return json.dumps(context, ensure_ascii=False, default=str, indent=2)

    @staticmethod
    def _filter_table_metadata(
        metadata: Any, table: str, selected_columns: list[str]
    ) -> Any:
        """Extract the requested table and retain only its selected columns."""
        if not isinstance(metadata, dict):
            return metadata
        tables = metadata.get("tables")
        if isinstance(tables, dict):
            metadata = tables.get(table, {})
        if not isinstance(metadata, dict):
            return metadata
        filtered = dict(metadata)
        columns = filtered.get("columns")
        if isinstance(columns, list):
            wanted = set(selected_columns)
            filtered["columns"] = [
                column for column in columns
                if isinstance(column, dict) and column.get("name") in wanted
            ]
        return filtered
