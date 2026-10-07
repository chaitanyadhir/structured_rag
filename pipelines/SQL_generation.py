"""Generate SQL from a user question and selected schema metadata."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import regex as re 
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
        # Avoid str.format(): literal JSON braces in the prompt are treated as
        # replacement fields (for example {"customer_name"}).
        prompt = template.replace("{query_type}", "SQL")
        prompt = prompt.replace("{query}", self.query)
        prompt = prompt.replace("{table_columns_context}", table_columns_context)
        response = llm_call(prompt)
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
            metadata = self._filter_table_metadata(metadata, table, columns)
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

    @staticmethod
    async def _table_metadata(selector: Any, table: str) -> Any:
        """Retrieve a table description without requesting metadata for all columns."""
        for name in (
            "get_table_metadata",
            "fetch_table_metadata",
            "table_metadata",
            "get_table_description",
            "fetch_table_description",
        ):
            method = getattr(selector, name, None)
            if callable(method):
                try:
                    result = method(table)
                except TypeError:
                    try:
                        result = method()
                    except TypeError:
                        continue
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, dict):
                    for key in ("description", "table_description", "metadata"):
                        value = result.get(key)
                        if isinstance(value, dict):
                            desc = value.get("description")
                            if desc is not None:
                                return desc
                            return value
                        if value is not None:
                            return value
                return result

        for attr in ("metadata", "catalog", "table_metadata", "tables"):
            catalog = getattr(selector, attr, None)
            if not isinstance(catalog, dict):
                continue
            if table not in catalog:
                continue
            value = catalog[table]
            if isinstance(value, dict):
                for key in ("description", "table_description"):
                    if key in value and value[key] is not None:
                        return value[key]
                for nested_key in ("metadata", "details"):
                    nested = value.get(nested_key)
                    if isinstance(nested, dict):
                        for key in ("description", "table_description"):
                            if key in nested and nested[key] is not None:
                                return nested[key]
                return value
            return value

        return {"table": table, "description": "No metadata available for this table."}