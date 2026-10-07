"""
STEP 1 LOGIC: Excel -> one staging table per sheet.

No relationships are guessed here. This module only cleans and stores.
"""
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from sqlalchemy import Engine, create_engine, inspect, text

REGISTRY = "_upload_registry"


def sanitize_identifier(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower()
    if not s:
        s = "col"
    if s[0].isdigit() or s[0] == "_":
        s = "t" + s if s[0] == "_" else "_" + s
    return s


def dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


@dataclass
class IngestResult:
    table_name: str
    source_file: str
    sheet: str
    rows: int
    columns: list[str]
    replaced_existing: bool


class ExcelIngestor:
    def __init__(self, staging_url: str):
        self.engine: Engine = create_engine(staging_url)
        with self.engine.begin() as c:
            c.execute(text(
                f"CREATE TABLE IF NOT EXISTS {REGISTRY} ("
                "table_name TEXT PRIMARY KEY, source_file TEXT, sheet TEXT, "
                "rows INTEGER, uploaded_at TEXT)"
            ))

    # ---------- public API ----------
    def ingest(self, filename: str, fileobj: BinaryIO) -> list[IngestResult]:
        stem = sanitize_identifier(Path(filename).stem)
        try:
            sheets = pd.read_excel(fileobj, sheet_name=None, engine="openpyxl")
        except Exception as e:
            raise ValueError(f"Could not read '{filename}' as Excel: {e}") from e

        usable = {n: self._clean(df) for n, df in sheets.items()}
        usable = {n: df for n, df in usable.items() if not df.empty}
        if not usable:
            raise ValueError(f"'{filename}' has no non-empty sheets.")

        results = []
        for sheet, df in usable.items():
            name = stem if len(usable) == 1 else f"{stem}_{sanitize_identifier(sheet)}"
            existed = inspect(self.engine).has_table(name)
            df.to_sql(name, self.engine, if_exists="replace", index=False)
            self._register(name, filename, sheet, len(df))
            results.append(IngestResult(name, filename, sheet, len(df),
                                        list(df.columns), existed))
        return results

    def list_tables(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(text(f"SELECT * FROM {REGISTRY} ORDER BY table_name")).mappings().all()
        return [dict(r) for r in rows]

    def drop_table(self, name: str) -> bool:
        if not inspect(self.engine).has_table(name) or name == REGISTRY:
            return False
        with self.engine.begin() as c:
            c.execute(text(f'DROP TABLE "{name}"'))
            c.execute(text(f"DELETE FROM {REGISTRY} WHERE table_name = :n"), {"n": name})
        return True

    # ---------- internals ----------
    @staticmethod
    def _clean(df: pd.DataFrame) -> pd.DataFrame:
        df = df.dropna(how="all").dropna(axis=1, how="all")
        df = df.loc[:, [not str(c).startswith("Unnamed:") or df[c].notna().any() for c in df.columns]]
        df.columns = dedupe([sanitize_identifier(c) for c in df.columns])
        for c in df.select_dtypes(include="object").columns:
            df[c] = df[c].map(lambda v: v.strip() if isinstance(v, str) else v)
            df[c] = df[c].replace("", pd.NA)
        return df.convert_dtypes().reset_index(drop=True)

    def _register(self, name: str, filename: str, sheet: str, rows: int) -> None:
        with self.engine.begin() as c:
            c.execute(text(f"DELETE FROM {REGISTRY} WHERE table_name = :n"), {"n": name})
            c.execute(text(
                f"INSERT INTO {REGISTRY} VALUES (:n, :f, :s, :r, :t)"),
                {"n": name, "f": filename, "s": sheet, "r": rows,
                 "t": datetime.now(timezone.utc).isoformat()})