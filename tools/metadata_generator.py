"""
Generate metadata (table definition + per-column metadata) for every table in the final DB.

Split of responsibility (on purpose):
  LLM  -> table definition, column definitions, semantic value_type
  CODE -> sql_type, sample_values, row_count, primary/foreign keys   (taken from the real DB,
          so they can never be hallucinated)

PRIVACY: the prompt contains real rows from your tables and is sent to the LLM provider.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, MetaData, create_engine, func, inspect, select

import config
from tools import LLM_provider as llm_provider
from tools.LLM_provider import LLMConfigError

PROMPT_FILE = "metadata_generation.txt"
PLACEHOLDERS = ("{table content}", "{table_content}")
PROMPT_ROWS = 3          # rows shown to the LLM
SAMPLES_PER_COLUMN = 3   # distinct non-null sample values stored in the metadata


def _json_safe(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def extract_json(text: str) -> dict:
    """Parse the model's answer even if it wrapped the JSON in fences or prose."""
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a == -1 or b <= a:
            raise ValueError("no JSON object found in model answer")
        obj = json.loads(t[a:b + 1])
    if not isinstance(obj, dict):
        raise ValueError("model answer is not a JSON object")
    return obj


class MetadataGenerator:
    def __init__(self, engine: Engine | None = None, prompt_path: Path | None = None):
        self.engine = engine or create_engine(config.FINAL_URL)
        self.prompt_path = Path(prompt_path or config.PROMPTS_DIR / PROMPT_FILE)

    # ---------- public ----------
    def list_tables(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def generate_all(self) -> dict:
        names = self.list_tables()
        workers = max(1, int(os.getenv("LLM_WORKERS", "4")))
        tables, errors = {}, {}

        def work(name):
            try:
                return name, self.generate_for_table(name), None
            except LLMConfigError:
                raise                                   # missing key: every table would fail the same way
            except Exception as e:                      # one bad table must not kill the rest
                return name, None, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(work, names))       # preserves sorted order
        for name, res, err in results:
            if err:
                errors[name] = err
            else:
                tables[name] = res
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "tables": tables, "errors": errors}

    def generate_for_table(self, name: str) -> dict:
        info = self._read_table(name)
        prompt = self._build_prompt(info)
        try:
            raw = extract_json(llm_provider.llm_call(prompt))
        except ValueError:                              # bad JSON -> one retry with a stricter reminder
            raw = extract_json(llm_provider.llm_call(
                prompt + "\n\nYour previous answer was not valid JSON. Reply with ONLY the JSON object."))
        return self._assemble(info, raw)

    # ---------- reading the table ----------
    def _read_table(self, name: str) -> dict:
        insp = inspect(self.engine)
        if name not in insp.get_table_names():
            raise KeyError(f"No such table '{name}'")
        md = MetaData(); md.reflect(self.engine, only=[name])
        t = md.tables[name]
        with self.engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(t)).scalar()
            df = pd.read_sql_query(select(t).limit(200), conn)       # head only, never the whole table
        return {"name": name, "total": total, "df": df,
                "types": {c["name"]: str(c["type"]) for c in insp.get_columns(name)},
                "pk": insp.get_pk_constraint(name)["constrained_columns"],
                "fks": [{"columns": f["constrained_columns"],
                         "references": f"{f['referred_table']}({', '.join(f['referred_columns'])})"}
                        for f in insp.get_foreign_keys(name)]}

    # ---------- prompt ----------
    def _table_content(self, info: dict) -> str:
        head = info["df"].head(PROMPT_ROWS)
        fks = "; ".join("{} -> {}".format(", ".join(f["columns"]), f["references"]) for f in info["fks"])
        lines = [f"Table name: {info['name']}",
                 f"Total rows: {info['total']}",
                 "Primary key: " + (", ".join(info["pk"]) or "none"),
                 "Foreign keys: " + (fks or "none"),
                 "", "Columns (name: SQL type):"]
        lines += [f"- {c}: {info['types'].get(c, '?')}" for c in info["df"].columns]
        lines += ["", f"First {len(head)} rows (CSV):", head.to_csv(index=False).strip()]
        return "\n".join(lines)

    def _build_prompt(self, info: dict) -> str:
        template = self.prompt_path.read_text(encoding="utf-8")
        token = next((p for p in PLACEHOLDERS if p in template), None)
        if token is None:
            raise ValueError(f"{self.prompt_path.name} must contain the placeholder {{table content}}")
        # .replace, NOT .format: the prompt contains JSON braces that would break str.format
        return template.replace(token, self._table_content(info))

    # ---------- result ----------
    def _assemble(self, info: dict, raw: dict) -> dict:
        llm_cols = raw.get("columns", {})
        if isinstance(llm_cols, list):
            llm_cols = {c.get("name"): c for c in llm_cols if isinstance(c, dict)}
        df, warnings, cols = info["df"], [], []
        for c in df.columns:
            m = llm_cols.get(c)
            if not isinstance(m, dict):
                warnings.append(f"LLM gave no metadata for column '{c}'")
                m = {}
            samples = [_json_safe(v) for v in df[c].dropna().drop_duplicates().head(SAMPLES_PER_COLUMN)]
            cols.append({"name": c,
                         "definition": m.get("definition") or m.get("description") or "",
                         "value_type": m.get("value_type") or m.get("type") or "",
                         "sql_type": info["types"].get(c, ""),
                         "sample_values": samples})
        extra = [c for c in llm_cols if c not in set(df.columns)]
        if extra:
            warnings.append(f"Ignored columns the LLM invented: {extra}")
        return {"table_name": info["name"],
                "definition": raw.get("definition") or raw.get("description") or "",
                "row_count": info["total"], "primary_key": info["pk"], "foreign_keys": info["fks"],
                "columns": cols, "warnings": warnings}


def save_metadata(result: dict, path: Path | None = None) -> Path:
    path = Path(path or config.METADATA_PATH)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path