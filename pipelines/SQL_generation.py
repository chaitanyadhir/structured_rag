"""Generate SQL from a user question and selected schema metadata."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from tools.LLM_provider import llm_call


class SQLGenerationPipeline:
    """Select relevant schema context and generate a SQL response."""

    def __init__(self, query: str) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        self.query = query.strip()

    async def run(self) -> str:
        selection = await self._select_table_columns()
        table_columns_context = await self._build_context(selection)
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "generate_SQL.txt"
        template = prompt_path.read_text(encoding="utf-8")
        prompt = template.format(
            query_type="SQL",
            query=self.query,
            table_columns_context=table_columns_context,
        )
        return llm_call(prompt)

    async def _select_table_columns(self) -> dict[str, list[str]]:
        base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base_url}/metadata/select", json={"question": self.query}
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or any(
            not isinstance(table, str)
            or not isinstance(columns, list)
            or any(not isinstance(column, str) for column in columns)
            for table, columns in result.items()
        ):
            raise ValueError("Metadata selection must map table names to column-name lists")
        return result

    async def _build_context(self, selection: dict[str, list[str]]) -> str:
        """Include table-level descriptions and only the selected columns."""
        from tools.table_column_selection import TableColumnSelector

        selector = TableColumnSelector()
        context: list[dict[str, Any]] = []
        for table, columns in selection.items():
            metadata = await self._table_metadata(selector, table)
            context.append(
                {"table": table, "table_metadata": metadata, "columns": columns}
            )
        return json.dumps(context, ensure_ascii=False, default=str, indent=2)

    @staticmethod
    async def _table_metadata(selector: Any, table: str) -> Any:
        """Retrieve a table description without requesting metadata for all columns."""
        for name in ("get_table_metadata", "fetch_table_metadata", "table_metadata"):
            method = getattr(selector, name, None)
            if callable(method):
                result = method(table)
                if hasattr(result, "__await__"):
                    result = await result
                return result
        catalog = getattr(selector, "metadata", getattr(selector, "catalog", None))
        if isinstance(catalog, dict) and table in catalog:
            value = catalog[table]
            return value.get("description", value) if isinstance(value, dict) else value
        raise RuntimeError(
            "TableColumnSelector must expose a table-level metadata lookup method or catalog"
        )