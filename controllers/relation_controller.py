"""STEP 2 ENDPOINTS: preview detected PK/FK, then build the final DB."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.exc import IntegrityError

import config
from controllers.upload_controller import ingestor
from tools.relation_builder import DatabaseBuilder, Relation, RelationDetector, RelationError

router = APIRouter(prefix="/relations", tags=["relations"])

Action = Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]


class RelationIn(BaseModel):
    """Single column: child_column/parent_column. Composite: child_columns/parent_columns."""
    model_config = ConfigDict(extra="forbid")      # typos are errors, never silently ignored
    child_table: str
    parent_table: str
    child_column: str | None = None
    parent_column: str | None = None
    child_columns: list[str] | None = None
    parent_columns: list[str] | None = None
    on_delete: Action | None = None
    on_update: Action | None = None
    unique: bool = False          # enforce 1:1 with a UNIQUE constraint on the child columns
    required: bool = False        # child columns NOT NULL (mandatory parent)
    child_default: str | int | None = None   # needed for SET DEFAULT

    @model_validator(mode="after")
    def _normalize(self):
        single = self.child_column is not None or self.parent_column is not None
        multi = self.child_columns is not None or self.parent_columns is not None
        if single == multi:
            raise ValueError("give either child_column+parent_column or child_columns+parent_columns")
        if single:
            if self.child_column is None or self.parent_column is None:
                raise ValueError("child_column and parent_column are both required")
            self.child_columns, self.parent_columns = [self.child_column], [self.parent_column]
        elif self.child_columns is None or self.parent_columns is None:
            raise ValueError("child_columns and parent_columns are both required")
        return self

    def to_relation(self) -> Relation:
        return Relation(self.child_table, tuple(self.child_columns), self.parent_table,
                        tuple(self.parent_columns), on_delete=self.on_delete, on_update=self.on_update,
                        unique=self.unique, required=self.required, child_default=self.child_default)


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Override primary keys: {"orders": "order_id", "order_items": ["order_id","line_no"], "logs": null}
    primary_keys: dict[str, str | list[str] | None] | None = None
    # If given, EXACTLY these relations are applied. If omitted, auto mode is used.
    relations: list[RelationIn] | None = None
    # Auto mode only: also apply 'suggested' relations (parent picked from table-name hints).
    include_suggested: bool = False


def _detector() -> RelationDetector:
    d = RelationDetector(ingestor.engine)
    if not d.tables:
        raise HTTPException(409, "No staging tables. Upload Excel files first.")
    return d


@router.get("/preview")
def preview():
    """Review step: what PKs / FKs would be created. Nothing is built."""
    d = _detector()
    try:
        pks, rels, inferred = d.analyze()
    except RelationError as e:
        raise HTTPException(422, str(e))
    return {"primary_keys": pks,
            "inferred_composite_primary_keys": inferred,
            "relations": [r.as_dict() for r in rels],
            "note": "'high' is auto-applied. 'suggested' needs {\"include_suggested\": true}. "
                    "'low' is never auto-applied. Or send explicit 'relations' to /relations/build."}


@router.post("/build")
def build(req: BuildRequest | None = None):
    req = req or BuildRequest()
    d = _detector()
    try:
        pks, detected, _ = d.analyze(req.primary_keys)
        if req.relations is None:
            ok = {"high", "suggested"} if req.include_suggested else {"high"}
            rels = [r for r in detected if r.confidence in ok]
        else:
            rels = [r.to_relation() for r in req.relations]
        return DatabaseBuilder(ingestor.engine, config.FINAL_URL).build(d.tables, pks, rels)
    except RelationError as e:
        raise HTTPException(422, str(e))
    except IntegrityError as e:
        raise HTTPException(422, f"Data violates a constraint: {e.orig}")