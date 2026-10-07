"""STEP 2 ENDPOINTS: preview detected PK/FK, then build the final DB."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

import config
from relation_builder import DatabaseBuilder, Relation, RelationDetector
from upload_controller import ingestor

router = APIRouter(prefix="/relations", tags=["relations"])


class RelationIn(BaseModel):
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str


class BuildRequest(BaseModel):
    # Override auto-detected primary keys: {"orders": "order_id", "logs": null}
    primary_keys: dict[str, str | None] | None = None
    # If given, EXACTLY these relations are applied. If omitted, only 'high' ones.
    relations: list[RelationIn] | None = None


def _detector() -> RelationDetector:
    d = RelationDetector(ingestor.engine)
    if not d.tables:
        raise HTTPException(409, "No staging tables. Upload Excel files first.")
    return d


@router.get("/preview")
def preview():
    """Review step: what PKs / FKs would be created. Nothing is built."""
    d = _detector()
    pks = d.detect_primary_keys()
    return {"primary_keys": pks,
            "relations": [r.as_dict() for r in d.detect_relations(pks)],
            "note": "Only 'high' relations are auto-applied. Send 'relations' to /relations/build to override."}


@router.post("/build")
def build(req: BuildRequest | None = None):
    req = req or BuildRequest()
    d = _detector()
    pks = {**d.detect_primary_keys(), **(req.primary_keys or {})}

    for t, c in pks.items():
        if t not in d.tables or (c and c not in d.tables[t].columns):
            raise HTTPException(422, f"Bad primary key override: {t}.{c}")

    if req.relations is None:
        rels = [r for r in d.detect_relations(pks) if r.confidence == "high"]
    else:
        rels = []
        for r in req.relations:
            if pks.get(r.parent_table) != r.parent_column:
                raise HTTPException(422, f"{r.parent_table}.{r.parent_column} is not the primary key of {r.parent_table}")
            if r.child_table not in d.tables or r.child_column not in d.tables[r.child_table].columns:
                raise HTTPException(422, f"Unknown child column {r.child_table}.{r.child_column}")
            rels.append(Relation(**r.model_dump()))

    try:
        return DatabaseBuilder(ingestor.engine, config.FINAL_URL).build(d.tables, pks, rels)
    except IntegrityError as e:
        raise HTTPException(422, f"Data violates a constraint (orphan FK values or duplicate PK): {e.orig}")