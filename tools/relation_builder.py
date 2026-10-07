"""
STEP 2 LOGIC: staging tables -> detect PKs / FKs -> validate every rule -> build final DB.

Design principles
  1. Detection is a heuristic, so only relations backed by BOTH data (100% of non-null child
     values exist in the parent) AND naming evidence are 'high'. Everything else needs a human.
  2. Every rule is checked in validate_relations() BEFORE the database is touched.
  3. After building, the database is read back and compared with what was requested.

Supported: single + composite PKs, single + composite FKs, UNIQUE parent keys, junction
(many-to-many) tables, self-references, 1:1 via UNIQUE, NULL / NOT NULL, referential actions
(NO ACTION, RESTRICT, CASCADE, SET NULL, SET DEFAULT), FK indexes, matching types/lengths.
"""
from dataclasses import asdict, dataclass, replace
from itertools import combinations

import pandas as pd
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Engine, Float,
                        ForeignKeyConstraint, Index, Integer, MetaData,
                        PrimaryKeyConstraint, String, Table, Text,
                        UniqueConstraint, create_engine, event, inspect, text)

from tools.excel_ingest import REGISTRY

ALLOWED_ACTIONS = {None, "NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
PARENT_HINTS = ("basic", "master", "main", "core", "profile")
SELF_HINTS = ("manager", "parent", "supervisor", "reports_to", "boss", "mentor")
SEQ_HINTS = ("line", "seq", "position", "step", "item_no", "_no", "_num", "index")


class RelationError(ValueError):
    """A requested key / relationship breaks a database rule."""


@dataclass(frozen=True)
class Relation:
    child_table: str
    child_columns: tuple
    parent_table: str
    parent_columns: tuple
    name_score: float = 0.0
    containment: float = 0.0
    confidence: str = "manual"      # high | suggested | low | manual
    note: str = ""
    on_delete: str | None = None    # None = database default (NO ACTION)
    on_update: str | None = None
    unique: bool = False            # True => UNIQUE on child columns (enforced 1:1)
    required: bool = False          # True => child columns NOT NULL (mandatory parent)
    child_default: object = None    # needed for SET DEFAULT
    cardinality: str = ""           # "1:1" | "1:N", reported from current data

    def as_dict(self):
        d = asdict(self)
        d["child_columns"], d["parent_columns"] = list(self.child_columns), list(self.parent_columns)
        return d


# ------------------------------------------------------------------ helpers
def _kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s): return "bool"
    if pd.api.types.is_integer_dtype(s): return "int"
    if pd.api.types.is_float_dtype(s): return "float"
    if pd.api.types.is_datetime64_any_dtype(s): return "datetime"
    return "str"


def _singular(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 3 else name


def _rows(df: pd.DataFrame, cols) -> set:
    """Set of key tuples. Rows with ANY null component are skipped (SQL MATCH SIMPLE)."""
    sub = df[list(cols)].dropna()
    return set(sub.itertuples(index=False, name=None))


def _py(v):
    return v.item() if hasattr(v, "item") else v


def cardinality(tables, r: Relation) -> str:
    sub = tables[r.child_table][list(r.child_columns)].dropna()
    return "1:1" if not sub.duplicated().any() else "1:N"


def validate_primary_keys(tables: dict[str, pd.DataFrame], pks: dict[str, list[str]]) -> None:
    for t, cols in pks.items():
        if not cols:
            continue
        if t not in tables:
            raise RelationError(f"Primary key given for unknown table '{t}'")
        for c in cols:
            if c not in tables[t].columns:
                raise RelationError(f"Primary key {t}.{c}: no such column")
        if len(set(cols)) != len(cols):
            raise RelationError(f"Primary key of {t} repeats a column")
        sub = tables[t][list(cols)]
        if sub.isna().any().any():
            raise RelationError(f"Primary key {t}({', '.join(cols)}) contains empty values")
        if sub.duplicated().any():
            raise RelationError(f"Primary key {t}({', '.join(cols)}) contains duplicate values")


# ------------------------------------------------------------------ detection
class RelationDetector:
    def __init__(self, staging_engine: Engine):
        self.engine = staging_engine
        names = [t for t in inspect(staging_engine).get_table_names() if t != REGISTRY]
        self.tables: dict[str, pd.DataFrame] = {
            n: pd.read_sql_table(n, staging_engine).convert_dtypes() for n in names}

    # ---- primary keys (single column) ----
    def detect_primary_keys(self) -> dict[str, list[str]]:
        out = {}
        for t, df in self.tables.items():
            pk = self._pick_pk(t, df)
            out[t] = [pk] if pk else []
        return out

    def _pick_pk(self, table: str, df: pd.DataFrame) -> str | None:
        cands = [c for c in df.columns if _kind(df[c]) in ("int", "str")
                 and df[c].notna().all() and df[c].is_unique and len(df) > 0]
        if not cands:
            return None
        base = _singular(table)
        for p in ("id", f"{table}_id", f"{base}_id", f"{table}id", f"{base}id"):
            if p in cands:
                return p
        id_like = [c for c in cands if c.endswith("_id") or (c.endswith("id") and len(c) > 2)]
        if id_like:
            return id_like[0]
        first = df.columns[0]
        return first if first in cands and _kind(df[first]) == "int" else None

    # ---- everything together ----
    def analyze(self, pk_overrides: dict | None = None):
        """Returns (primary_keys, relations, inferred_composite_pks)."""
        pks = self.detect_primary_keys()
        locked = set()
        for t, v in (pk_overrides or {}).items():
            pks[t] = [] if v is None else ([v] if isinstance(v, str) else list(v))
            locked.add(t)
        validate_primary_keys(self.tables, pks)

        rels = self._single_column_relations(pks)
        inferred = self._infer_composite_pks(pks, locked, rels)
        rels += self._composite_parent_relations(pks)
        rels += self._self_references(pks)
        rels = [replace(r, cardinality=cardinality(self.tables, r)) for r in rels]
        order = {"high": 0, "suggested": 1, "low": 2}
        rels.sort(key=lambda r: (order.get(r.confidence, 3), r.child_table, r.child_columns))
        return pks, rels, inferred

    # ---- single-column foreign keys ----
    def _single_column_relations(self, pks) -> list[Relation]:
        cands: dict[tuple[str, str], list[Relation]] = {}
        for child, cdf in self.tables.items():
            for ccol in cdf.columns:
                is_own_pk = pks.get(child) == [ccol]
                uniq = {r[0] for r in _rows(cdf, [ccol])}
                if not uniq:
                    continue
                for parent, pk in pks.items():
                    if len(pk) != 1 or parent == child:
                        continue
                    pcol = pk[0]
                    pser = self.tables[parent][pcol]
                    if _kind(cdf[ccol]) != _kind(pser) or _kind(pser) not in ("int", "str"):
                        continue
                    cover = len(uniq & set(pser)) / len(uniq)
                    if cover < 0.9:
                        continue
                    ns = self._name_score(ccol, parent, pcol)
                    if is_own_pk and ns < 0.7:
                        continue            # don't link unrelated id columns (order_id vs customer_id)
                    conf = "high" if (cover == 1.0 and ns >= 0.7) else "low"
                    cands.setdefault((child, ccol), []).append(Relation(
                        child, (ccol,), parent, (pcol,), round(ns, 2), round(cover, 3), conf))

        out = []
        for (child, ccol), lst in cands.items():
            best = max((r.name_score, r.containment) for r in lst)
            top = [r for r in lst if (r.name_score, r.containment) == best]
            n_child = len(self.tables[child])
            own = pks.get(child) == [ccol]
            # A shared key with the SAME row count is a peer, not a parent (A->B and B->A look identical).
            if len(top) == 1 and not (own and len(self.tables[top[0].parent_table]) == n_child):
                out.append(top[0]); continue
            # several equally good parents (e.g. every HR table keyed by employee_id)
            bigger = [r for r in top if len(self.tables[r.parent_table]) > n_child]
            if bigger:
                mx = max(len(self.tables[r.parent_table]) for r in bigger)
                winners = [r for r in bigger if len(self.tables[r.parent_table]) == mx]
                if len(winners) == 1:
                    out.append(winners[0]); continue
            hinted = [r for r in top if any(h in r.parent_table for h in PARENT_HINTS)
                      and not any(h in child for h in PARENT_HINTS)]
            if len(hinted) == 1:
                out.append(replace(hinted[0], confidence="suggested",
                    note="Same key and row count in all tables; parent chosen only because its "
                         "table name looks like the master table. Verify before applying."))
                continue
            note = ("Several tables share this key with the same row count, so the parent "
                    "cannot be determined from data. Pick the parent yourself.")
            out += [replace(r, confidence="low", note=note) for r in top]

        resolved = {(r.child_table, r.child_columns) for r in out if r.confidence == "suggested"}
        parents = {(r.parent_table, r.parent_columns) for r in out if r.confidence == "suggested"}
        return [r for r in out if not (r.confidence == "low" and (
            (r.child_table, r.child_columns) in resolved or (r.child_table, r.child_columns) in parents))]

    @staticmethod
    def _name_score(ccol: str, parent: str, pcol: str) -> float:
        base = _singular(parent)
        if ccol == pcol and pcol != "id":
            return 1.0
        if ccol in (f"{parent}_{pcol}", f"{base}_{pcol}", f"{parent}_id", f"{base}_id"):
            return 1.0
        if (ccol.endswith(f"_{pcol}") and pcol != "id") or (base in ccol and ccol.endswith("id")):
            return 0.7
        return 0.0

    # ---- composite primary keys for tables with no single-column key (junction tables) ----
    def _infer_composite_pks(self, pks, locked, rels) -> dict[str, list[str]]:
        inferred = {}
        for t, df in self.tables.items():
            if pks.get(t) or t in locked or len(df) == 0:
                continue
            fk_cols = []
            for r in rels:
                if r.child_table == t and r.confidence == "high" and r.child_columns[0] not in fk_cols:
                    fk_cols.append(r.child_columns[0])
            seq = [c for c in df.columns if c not in fk_cols and _kind(df[c]) == "int"
                   and any(h in c for h in SEQ_HINTS)]
            pool = fk_cols + seq
            for size in (2, 3):
                hit = None
                for combo in combinations(pool, size):
                    if not any(c in fk_cols for c in combo):
                        continue
                    sub = df[list(combo)]
                    if sub.notna().all().all() and not sub.duplicated().any():
                        hit = list(combo); break
                if hit:
                    pks[t] = inferred[t] = hit
                    break
        return inferred

    # ---- foreign keys that reference a composite primary key ----
    def _composite_parent_relations(self, pks) -> list[Relation]:
        out = []
        for parent, pk in pks.items():
            if len(pk) < 2:
                continue
            prows = _rows(self.tables[parent], pk)
            for child, cdf in self.tables.items():
                if child == parent or not all(c in cdf.columns for c in pk):
                    continue
                if any(_kind(cdf[c]) != _kind(self.tables[parent][c]) for c in pk):
                    continue
                crows = _rows(cdf, pk)
                if not crows:
                    continue
                cover = len(crows & prows) / len(crows)
                if cover == 1.0:
                    out.append(Relation(child, tuple(pk), parent, tuple(pk), 1.0, 1.0, "high",
                                        note="composite key (same column names, all values exist in parent)"))
        return out

    # ---- self references (employee.manager_id -> employee.employee_id) ----
    def _self_references(self, pks) -> list[Relation]:
        out = []
        for t, pk in pks.items():
            if len(pk) != 1:
                continue
            df, pcol = self.tables[t], pk[0]
            for c in df.columns:
                if c == pcol or _kind(df[c]) != _kind(df[pcol]) or not any(h in c for h in SELF_HINTS):
                    continue
                vals = {r[0] for r in _rows(df, [c])}
                if vals and vals <= set(df[pcol]):
                    out.append(Relation(t, (c,), t, (pcol,), 0.7, 1.0, "high", note="self-reference"))
        return out


# ------------------------------------------------------------------ validation
def validate_relations(tables, pks, rels):
    """Check every FK rule BEFORE touching the database.
    Returns (relations, unique_targets) - unique_targets are non-PK parent keys to declare UNIQUE."""
    unique_targets, out, seen = set(), [], set()
    for r in rels:
        cc, pc = tuple(r.child_columns), tuple(r.parent_columns)
        name = f"{r.child_table}({', '.join(cc)}) -> {r.parent_table}({', '.join(pc)})"
        err = lambda m: RelationError(f"{name}: {m}")

        if not cc or len(cc) != len(pc):
            raise err(f"column count must match (child {len(cc)} vs parent {len(pc)})")
        if len(set(cc)) != len(cc) or len(set(pc)) != len(pc):
            raise err("a column is listed twice")
        for t, cols in ((r.child_table, cc), (r.parent_table, pc)):
            if t not in tables:
                raise err(f"unknown table '{t}'")
            for c in cols:
                if c not in tables[t].columns:
                    raise err(f"unknown column {t}.{c}")
        if r.child_table == r.parent_table and set(cc) & set(pc):
            raise err("a column cannot reference itself")
        key = (r.child_table, cc, r.parent_table, pc)
        if key in seen:
            raise err("defined twice")
        seen.add(key)
        cdf, pdf = tables[r.child_table], tables[r.parent_table]

        # Rule: parent must be a PRIMARY KEY or a UNIQUE, non-null key
        if set(pc) != set(pks.get(r.parent_table) or []):
            sub = pdf[list(pc)]
            if sub.isna().any().any() or sub.duplicated().any():
                raise err("parent columns must be the primary key or a unique, non-null key "
                          "(they contain duplicates or empty values)")
            unique_targets.add((r.parent_table, pc))
        # Rule: matching data types
        for a, b in zip(cc, pc):
            if _kind(cdf[a]) != _kind(pdf[b]):
                raise err(f"data type mismatch on {a}/{b} ({_kind(cdf[a])} vs {_kind(pdf[b])})")
        # Rule: referential actions
        for act in (r.on_delete, r.on_update):
            if act not in ALLOWED_ACTIONS:
                raise err(f"unsupported action '{act}'")
        acts = {r.on_delete, r.on_update}
        if "SET NULL" in acts:
            if set(cc) & set(pks.get(r.child_table) or []):
                raise err("SET NULL is impossible: a child column is part of the primary key (NOT NULL)")
            if r.required:
                raise err("SET NULL conflicts with required=true (NOT NULL)")
        child_default = r.child_default
        if "SET DEFAULT" in acts:
            if len(cc) != 1:
                raise err("SET DEFAULT is supported for single-column foreign keys only")
            if child_default is None:
                raise err("SET DEFAULT needs 'child_default' (a value that exists in the parent)")
            try:
                child_default = {"int": int, "str": str}[_kind(cdf[cc[0]])](child_default)
            except (KeyError, ValueError):
                raise err(f"child_default {r.child_default!r} is not valid for this column type")
            if (child_default,) not in _rows(pdf, pc):
                raise err(f"child_default {child_default!r} does not exist in the parent, "
                          f"SET DEFAULT would always fail")
        # Rule: nullability
        if r.required and cdf[list(cc)].isna().any().any():
            raise err("required=true but the child columns contain empty values")
        # Rule: 1:1 needs UNIQUE
        if r.unique and cdf[list(cc)].dropna().duplicated().any():
            raise err("unique=true (1:1) but a parent is referenced by several child rows")
        # Rule: every non-null child value must exist in the parent
        missing = _rows(cdf, cc) - _rows(pdf, pc)
        if missing:
            ex = sorted(missing, key=str)[:5]
            ex = [tuple(_py(v) for v in e) if len(cc) > 1 else _py(e[0]) for e in ex]
            raise err(f"{len(missing)} child value(s) have no parent row, e.g. {ex}. "
                      f"Fix the data or remove this relation.")
        out.append(replace(r, child_columns=cc, parent_columns=pc, child_default=child_default,
                           cardinality=cardinality(tables, r)))
    return out, unique_targets


# ------------------------------------------------------------------ building
def _bucket(n: int):
    for b in (16, 32, 64, 128, 255, 512, 1000, 4000):
        if n <= b:
            return b
    return None   # -> TEXT


class DatabaseBuilder:
    def __init__(self, staging_engine: Engine, final_url: str):
        self.staging = staging_engine
        self.final_url = final_url

    def build(self, tables, pks, relations) -> dict:
        validate_primary_keys(tables, pks)
        relations, unique_targets = validate_relations(tables, pks, relations)   # nothing touched yet
        relations, skipped = self._drop_cycles(relations)
        specs = self._type_specs(tables, relations)
        engine = self._fresh_engine()
        md = MetaData()

        required = {(r.child_table, c) for r in relations if r.required for c in r.child_columns}
        defaults = {(r.child_table, r.child_columns[0]): r.child_default
                    for r in relations if "SET DEFAULT" in (r.on_delete, r.on_update)}
        uniques: dict[str, set] = {}
        for t, cols in unique_targets:
            uniques.setdefault(t, set()).add(tuple(cols))
        for r in relations:
            if r.unique:
                uniques.setdefault(r.child_table, set()).add(r.child_columns)

        for t, df in tables.items():
            pk = pks.get(t) or []
            cols = [Column(c, self._sa_type(specs[(t, c)]),
                           nullable=not (c in pk or (t, c) in required),
                           server_default=(str(defaults[(t, c)]) if (t, c) in defaults else None))
                    for c in df.columns]
            args = list(cols)
            if pk:
                args.append(PrimaryKeyConstraint(*pk))
            for u in sorted(uniques.get(t, ())):
                if set(u) != set(pk):
                    args.append(UniqueConstraint(*u, name=f"uq_{t}_{'_'.join(u)}"))
            indexed = set()
            for r in (x for x in relations if x.child_table == t):
                args.append(ForeignKeyConstraint(
                    list(r.child_columns), [f"{r.parent_table}.{p}" for p in r.parent_columns],
                    name=f"fk_{t}_{'_'.join(r.child_columns)}_{r.parent_table}",
                    ondelete=r.on_delete, onupdate=r.on_update,
                    **({"deferrable": True, "initially": "DEFERRED"} if r.child_table == r.parent_table else {})))
                covered = (list(r.child_columns) == pk[:len(r.child_columns)]
                           or r.child_columns in uniques.get(t, ()) or r.child_columns in indexed)
                if not covered:      # index FK columns so parent checks / joins stay fast
                    args.append(Index(f"ix_{t}_{'_'.join(r.child_columns)}", *r.child_columns))
                    indexed.add(r.child_columns)
            Table(t, md, *args)

        loaded = {}
        try:
            md.create_all(engine)
            with engine.begin() as conn:
                for t in md.sorted_tables:                      # parents before children
                    tables[t.name].to_sql(t.name, conn, if_exists="append", index=False)
                    loaded[t.name] = len(tables[t.name])
            self._verify(engine, pks, relations)
        except Exception:
            md.drop_all(engine)                                 # never leave a half-built DB behind
            raise
        finally:
            engine.dispose()
        return {"tables": loaded,
                "primary_keys": {k: v for k, v in pks.items() if v},
                "foreign_keys_applied": [r.as_dict() for r in relations],
                "skipped_due_to_cycle": [r.as_dict() for r in skipped],
                "verified": True}

    # ---- helpers ----
    def _type_specs(self, tables, relations):
        """One type per column; linked FK/PK columns are forced to the SAME type and length."""
        spec = {}
        for t, df in tables.items():
            for c in df.columns:
                s, k = df[c], _kind(df[c])
                if k == "str":
                    n = int(s.dropna().astype(str).str.len().max()) if s.notna().any() else 1
                    spec[(t, c)] = ["str", _bucket(max(n, 1))]
                elif k == "int":
                    spec[(t, c)] = ["int", bool(s.notna().any() and s.abs().max() > 2**31 - 1)]
                else:
                    spec[(t, c)] = [k, None]
        changed = True
        while changed:
            changed = False
            for r in relations:
                for a, b in zip(r.child_columns, r.parent_columns):
                    x, y = spec[(r.child_table, a)], spec[(r.parent_table, b)]
                    if x[0] == "str":
                        m = None if (x[1] is None or y[1] is None) else max(x[1], y[1])
                    else:
                        m = x[1] or y[1]
                    if x[1] != m or y[1] != m:
                        x[1] = y[1] = m; changed = True
        return spec

    @staticmethod
    def _sa_type(spec):
        k, extra = spec
        if k == "str": return String(extra) if extra else Text()
        if k == "int": return BigInteger() if extra else Integer()
        return {"bool": Boolean, "float": Float, "datetime": DateTime}[k]()

    def _fresh_engine(self) -> Engine:
        engine = create_engine(self.final_url)
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine, "connect")
            def _fk_on(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # Drop old tables instead of deleting the file (deleting fails on Windows if it is open).
        md = MetaData()
        md.reflect(engine)
        md.drop_all(engine)
        return engine

    @staticmethod
    def _verify(engine: Engine, pks, relations) -> None:
        """Read the finished database back and prove it contains what was requested."""
        if engine.dialect.name == "sqlite":
            with engine.connect() as c:
                bad = c.execute(text("PRAGMA foreign_key_check")).fetchall()
            if bad:
                raise RelationError(f"foreign_key_check found {len(bad)} violating row(s)")
        insp = inspect(engine)
        for t, pk in pks.items():
            got = insp.get_pk_constraint(t)["constrained_columns"]
            if set(got) != set(pk):
                raise RuntimeError(f"verification failed: primary key of {t} is {got}, expected {pk}")
        for r in relations:
            ok = False
            for fk in insp.get_foreign_keys(r.child_table):
                opts = fk.get("options") or {}
                if (fk["referred_table"] == r.parent_table
                        and fk["constrained_columns"] == list(r.child_columns)
                        and fk["referred_columns"] == list(r.parent_columns)
                        and (r.on_delete is None or opts.get("ondelete") == r.on_delete)
                        and (r.on_update is None or opts.get("onupdate") == r.on_update)):
                    ok = True
            if not ok:
                raise RuntimeError(f"verification failed: FK {r.child_table}{r.child_columns} "
                                   f"-> {r.parent_table}{r.parent_columns} missing in database")

    @staticmethod
    def _drop_cycles(rels):
        """Circular FKs between different tables (A->B->A) cannot be created in order; skip later ones.
        Self-references are fine (deferred constraint)."""
        kept, skipped, edges = [], [], []

        def reaches(src, dst):
            stack, seen = [src], set()
            while stack:
                n = stack.pop()
                if n == dst: return True
                if n in seen: continue
                seen.add(n)
                stack += [p for c, p in edges if c == n]
            return False

        for r in rels:
            if r.child_table != r.parent_table and reaches(r.parent_table, r.child_table):
                skipped.append(r)
            else:
                kept.append(r)
                if r.child_table != r.parent_table:
                    edges.append((r.child_table, r.parent_table))
        return kept, skipped