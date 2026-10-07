"""Select relevant tables and columns from the metadata catalog."""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .laya_provider import LayaProvider


class TableColumnSelector:
    """Classify metadata columns against a user's question."""

    def __init__(self, metadata_path: Optional[str] = None,
                 provider: Optional[LayaProvider] = None) -> None:
        # tools/ is two levels below the project root.
        self.metadata_path = Path(metadata_path) if metadata_path else (
            Path(__file__).resolve().parents[2] / "data" / "metadata.json"
        )
        self.provider = provider or LayaProvider()

    def load_metadata(self) -> Dict[str, Any]:
        """Read metadata.json and verify its expected top-level structure."""
        with self.metadata_path.open("r", encoding="utf-8") as source:
            metadata = json.load(source)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("tables"), dict):
            raise ValueError("Metadata must contain a 'tables' object.")
        return metadata

    async def select(self, question: str) -> Dict[str, List[str]]:
        """Return only relevant columns, grouped under their table names."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")

        selections: Dict[str, List[str]] = {}
        for key, table in self.load_metadata()["tables"].items():
            if not isinstance(table, dict):
                continue
            columns = table.get("columns", [])
            if not isinstance(columns, list):
                continue

            valid_columns = [column for column in columns if isinstance(column, dict)]
            decisions = await asyncio.gather(*(
                self._is_relevant(question, table, column)
                for column in valid_columns
            ))
            names = [
                column["name"] for column, decision in zip(valid_columns, decisions)
                if decision and isinstance(column.get("name"), str)
            ]
            if names:
                selections[table.get("table_name", key)] = names
        return selections

    async def _is_relevant(self, question: str, table: Dict[str, Any],
                           column: Dict[str, Any]) -> bool:
        context = {
            "table_name": table.get("table_name"),
            "table_definition": table.get("definition"),
            "column": column,
        }
        result = await self.provider.evaluate_binary_choice(
            context=context,
            question=question,
            instructions="Does this column contain information needed to answer the question?",
            yes_criteria="The column directly contains or supports information needed to answer the question.",
            no_criteria="The column is unrelated or does not help answer the question.",
        )
        return result == "yes"