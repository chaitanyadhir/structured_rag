"""
STEP 2 LOGIC: staging tables -> detect PKs / FKs -> final DB with constraints.

IMPORTANT: FK detection from raw Excel data is a heuristic. We only auto-apply a
relation when BOTH hold:
  1. every non-null child value exists in the parent key (100% containment), and
  2. the column names look related (e.g. customer_id -> customers.id/customer_id).
Value overlap alone is NOT enough: any two small integer id columns overlap.
Everything else is returned as 'low' confidence and needs human approval.
"""
from dataclasses import dataclass, asdict, replace
from pathlib import Path

import pandas as pd
from sqlalchemy import (Boolean, Column, DateTime, Engine, Float, ForeignKey,
                        Integer, MetaData, String, Table, create_engine, event,
                        inspect, text)

from tools.excel_ingest import REGISTRY


@dataclass(frozen=True)
class Relation:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    name_score: float = 0.0
    containment: float = 0.0
    confidence: str = "manual"  # high | low | manual
    note: str = ""

    def as_dict(self):
        return asdict(self)


def _kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s): return "bool"
    if pd.api.types.is_integer_dtype(s): return "int"
    if pd.api.types.is_float_dtype(s): return "float"
    if pd.api.types.is_datetime64_any_dtype(s): return "datetime"
    return "str"


def _singular(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 3 else name


class RelationDetector:
    def __init__(self, staging_engine: Engine):
        self.engine = staging_engine
        names = [t for t in inspect(staging_engine).get_table_names() if t != REGISTRY]
        self.tables: dict[str, pd.DataFrame] = {
            n: pd.read_sql_table(n, staging_engine).convert_dtypes() for n in names}

    # ---------- primary keys ----------
    def detect_primary_keys(self) -> dict[str, str | None]:
        pks = {}
        for t, df in self.tables.items():
            pks[t] = self._pick_pk(t, df)
        return pks

    def _pick_pk(self, table: str, df: pd.DataFrame) -> str | None:
        cands = [c for c in df.columns
                 if _kind(df[c]) in ("int", "str") and df[c].notna().all()
                 and df[c].is_unique and len(df) > 0]
        if not cands:
            return None
        base = _singular(table)
        preferred = ["id", f"{table}_id", f"{base}_id", f"{table}id", f"{base}id"]
        for p in preferred:
            if p in cands:
                return p
        id_like = [c for c in cands if c.endswith("_id") or c.endswith("id") and len(c) > 2]
        if id_like:
            return id_like[0]
        # fall back to first column only if it is unique & non-null AND integer
        first = df.columns[0]
        return first if first in cands and _kind(df[first]) == "int" else None

    # ---------- foreign keys ----------
    def detect_relations(self, pks: dict[str, str | None] | None = None) -> list[Relation]:
        pks = pks or self.detect_primary_keys()
        cands: dict[tuple[str, str], list[Relation]] = {}
        for child, cdf in self.tables.items():
            for ccol in cdf.columns:
                is_own_pk = ccol == pks.get(child)
                cvals = cdf[ccol].dropna()
                if cvals.empty:
                    continue
                uniq = set(cvals.unique())
                for parent, pcol in pks.items():
                    if pcol is None or parent == child:
                        continue
                    pser = self.tables[parent][pcol]
                    if _kind(cdf[ccol]) != _kind(pser) or _kind(pser) not in ("int", "str"):
                        continue
                    cover = len(uniq & set(pser)) / len(uniq)
                    if cover < 0.9:   # ignore clearly unrelated columns
                        continue
                    ns = self._name_score(child, ccol, parent, pcol)
                    if is_own_pk and ns < 0.7:
                        continue   # avoid linking unrelated id columns (order_id vs customer_id)
                    conf = "high" if (cover == 1.0 and ns >= 0.7) else "low"
                    cands.setdefault((child, ccol), []).append(
                        Relation(child, ccol, parent, pcol, round(ns, 2), round(cover, 3), conf))

        out = []
        for (child, ccol), lst in cands.items():
            best = max((r.name_score, r.containment) for r in lst)
            top = [r for r in lst if (r.name_score, r.containment) == best]
            if len(top) == 1:
                out.append(top[0]); continue
            # Several equally good parents (typical for shared-key 1:1 tables, e.g. every
            # HR table keyed by employee_id). The parent must have MORE rows than the child.
            n_child = len(self.tables[child])
            bigger = [r for r in top if len(self.tables[r.parent_table]) > n_child]
            if bigger:
                biggest = max(len(self.tables[r.parent_table]) for r in bigger)
                winners = [r for r in bigger if len(self.tables[r.parent_table]) == biggest]
                if len(winners) == 1:
                    out.append(winners[0]); continue
            note = ("Several tables share this key with the same row count, so the parent "
                    "cannot be determined from data. Pick the parent yourself.")
            out += [replace(r, confidence="low", note=note) for r in top]
        return sorted(out, key=lambda r: (r.confidence != "high", r.child_table, r.child_column))

    @staticmethod
    def _name_score(child: str, ccol: str, parent: str, pcol: str) -> float:
        base = _singular(parent)
        if ccol == pcol and pcol != "id":
            return 1.0
        if ccol in (f"{parent}_{pcol}", f"{base}_{pcol}", f"{parent}_id", f"{base}_id"):
            return 1.0
        if ccol.endswith(f"_{pcol}") and pcol != "id" or (base in ccol and ccol.endswith("id")):
            return 0.7
        return 0.0


class DatabaseBuilder:
    def __init__(self, staging_engine: Engine, final_url: str):
        self.staging = staging_engine
        self.final_url = final_url

    def build(self, tables: dict[str, pd.DataFrame], pks: dict[str, str | None],
              relations: list[Relation]) -> dict:
        relations, skipped = self._drop_cycles(relations)
        engine = self._fresh_engine()
        md = MetaData()
        fk_by_child: dict[tuple[str, str], Relation] = {(r.child_table, r.child_column): r for r in relations}

        for tname, df in tables.items():
            cols = []
            for c in df.columns:
                kwargs = {"primary_key": pks.get(tname) == c}
                args = [self._sa_type(df[c])]
                rel = fk_by_child.get((tname, c))
                if rel:
                    args.append(ForeignKey(f"{rel.parent_table}.{rel.parent_column}"))
                cols.append(Column(c, *args, **kwargs))
            Table(tname, md, *cols)

        loaded = {}
        try:
            md.create_all(engine)
            with engine.begin() as conn:
                for t in md.sorted_tables:       # parents before children
                    tables[t.name].to_sql(t.name, conn, if_exists="append", index=False)
                    loaded[t.name] = len(tables[t.name])
        finally:
            engine.dispose()                      # release final.db file handle
        return {"tables": loaded,
                "primary_keys": {k: v for k, v in pks.items() if v},
                "foreign_keys_applied": [r.as_dict() for r in relations],
                "skipped_due_to_cycle": [r.as_dict() for r in skipped]}

    # ---------- helpers ----------
    def _fresh_engine(self) -> Engine:
        engine = create_engine(self.final_url)
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine, "connect")
            def _fk_on(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # Drop old tables instead of deleting the file: deleting fails on Windows
        # whenever any process (or an old engine) still has final.db open.
        md = MetaData()
        md.reflect(engine)
        md.drop_all(engine)
        return engine

    @staticmethod
    def _sa_type(s: pd.Series):
        return {"bool": Boolean, "int": Integer, "float": Float,
                "datetime": DateTime}.get(_kind(s), String)()

    @staticmethod
    def _drop_cycles(rels: list[Relation]):
        """Circular FKs (A->B->A) can't be created in order; keep earlier/higher ones."""
        kept, skipped = [], []
        def reaches(src, dst, edges):
            stack, seen = [src], set()
            while stack:
                n = stack.pop()
                if n == dst: return True
                if n in seen: continue
                seen.add(n)
                stack += [p for c, p in edges if c == n]
            return False
        edges: list[tuple[str, str]] = []
        for r in rels:
            if reaches(r.parent_table, r.child_table, edges):
                skipped.append(r)
            else:
                kept.append(r); edges.append((r.child_table, r.parent_table))
        return kept, skipped