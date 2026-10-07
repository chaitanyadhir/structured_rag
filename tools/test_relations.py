"""One test per rule. Behaviour is checked on the REAL generated SQLite DB, not on our own report."""
import io, os, sqlite3, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import config
from main import app

c = TestClient(app)


def xl(df):
    b = io.BytesIO(); df.to_excel(b, index=False, engine="openpyxl"); b.seek(0); return b


@pytest.fixture(autouse=True)
def clean():
    for t in c.get("/uploads/tables").json():
        c.delete("/uploads/tables/" + t["table_name"])
    (config.DATA_DIR / "final.db").unlink(missing_ok=True)
    yield


def up(**dfs):
    r = c.post("/uploads", files=[("files", (k + ".xlsx", xl(v))) for k, v in dfs.items()])
    assert r.status_code == 200, r.text


def R(ct, cc, pt, pc, **kw):
    return {"child_table": ct, "child_column": cc, "parent_table": pt, "parent_column": pc, **kw}


def build(**body):
    return c.post("/relations/build", json=body)


def db():
    con = sqlite3.connect(config.DATA_DIR / "final.db")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def blocked(sql):
    """The statement must be rejected by a database constraint."""
    con = db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql)
    finally:
        con.close()


def one(rels, **body):
    r = build(relations=rels, **body); assert r.status_code == 200, r.text; return r.json()


DEPTS = pd.DataFrame({"dept_id": [1, 2, 3], "name": list("abc")})
STAFF = pd.DataFrame({"staff_id": [10, 11, 12], "dept_id": [1, 1, 2]})


# ---------- 1. definition & linkage ----------
def test_auto_detects_pk_and_fk():
    up(depts=DEPTS, staff=STAFF)
    r = build().json()
    assert r["primary_keys"] == {"depts": ["dept_id"], "staff": ["staff_id"]}
    assert [(f["child_table"], f["parent_table"]) for f in r["foreign_keys_applied"]] == [("staff", "depts")]
    assert r["verified"] is True


def test_parent_must_be_pk_or_unique():
    up(depts=pd.DataFrame({"dept_id": [1, 2, 3], "code": ["A", "A", "C"]}),
       staff=pd.DataFrame({"staff_id": [1, 2], "dept_code": ["A", "C"]}))
    r = build(relations=[R("staff", "dept_code", "depts", "code")])
    assert r.status_code == 422 and "unique" in r.json()["detail"]


def test_unique_non_pk_parent_is_declared_unique():
    up(depts=pd.DataFrame({"dept_id": [1, 2, 3], "code": ["A", "B", "C"]}),
       staff=pd.DataFrame({"staff_id": [1, 2, 3], "dept_code": ["A", "A", "C"]}))
    one([R("staff", "dept_code", "depts", "code")])
    blocked("INSERT INTO depts VALUES (9, 'A')")


def test_type_mismatch_rejected():
    up(depts=pd.DataFrame({"dept_id": ["A1", "A2"], "n": ["x", "y"]}),
       staff=pd.DataFrame({"staff_id": [1, 2], "dept_id": [1, 2]}))
    r = build(relations=[R("staff", "dept_id", "depts", "dept_id")])
    assert r.status_code == 422 and "mismatch" in r.json()["detail"]


def test_linked_columns_get_identical_type_and_length():
    up(depts=pd.DataFrame({"code": ["A", "B"], "n": ["x", "y"]}),
       staff=pd.DataFrame({"staff_id": [1, 2, 3], "dept_code": ["A", "B", "A" * 1] }))
    one([R("staff", "dept_code", "depts", "code")])
    t = {r[1]: r[2] for t in ("depts", "staff") for r in db().execute(f"PRAGMA table_info({t})")}
    assert t["code"] == t["dept_code"] and t["code"].startswith("VARCHAR(")


def test_composite_fk_column_count_must_match():
    up(depts=DEPTS, staff=STAFF)
    r = build(relations=[{"child_table": "staff", "parent_table": "depts",
                          "child_columns": ["dept_id", "staff_id"], "parent_columns": ["dept_id"]}])
    assert r.status_code == 422 and "column count" in r.json()["detail"]


# ---------- composite keys & many-to-many ----------
def test_junction_table_gets_composite_pk_and_two_fks():
    up(students=pd.DataFrame({"student_id": [1, 2, 3], "n": list("abc")}),
       courses=pd.DataFrame({"course_id": [7, 8], "t": list("xy")}),
       enrollments=pd.DataFrame({"student_id": [1, 1, 2, 3, 3], "course_id": [7, 8, 7, 7, 8]}))
    r = build().json()
    assert r["primary_keys"]["enrollments"] == ["student_id", "course_id"]
    assert len(r["foreign_keys_applied"]) == 2
    with pytest.raises(sqlite3.IntegrityError):                   # duplicate enrollment blocked
        db().execute("INSERT INTO enrollments VALUES (1, 7)")


def test_composite_foreign_key():
    up(orders=pd.DataFrame({"order_id": [1, 2], "cust": ["x", "y"]}),
       order_items=pd.DataFrame({"order_id": [1, 1, 2], "line_no": [1, 2, 1], "sku": list("abc")}),
       shipments=pd.DataFrame({"ship_id": [5, 6], "order_id": [1, 2], "line_no": [2, 1]}))
    r = build().json()
    assert r["primary_keys"]["order_items"] == ["order_id", "line_no"]
    fk = [f for f in r["foreign_keys_applied"] if f["child_table"] == "shipments" and len(f["child_columns"]) == 2][0]
    assert fk["child_columns"] == ["order_id", "line_no"]
    blocked("INSERT INTO shipments VALUES (7, 1, 99)")


# ---------- 2. referential integrity ----------
def test_orphan_child_value_rejected_before_anything_is_built():
    up(depts=DEPTS, staff=pd.DataFrame({"staff_id": [1, 2], "dept_id": [1, 99]}))
    r = build(relations=[R("staff", "dept_id", "depts", "dept_id")])
    assert r.status_code == 422 and "99" in r.json()["detail"]
    assert not (config.DATA_DIR / "final.db").exists()


def test_failed_request_keeps_previous_good_database():
    up(depts=DEPTS, staff=STAFF); build()
    r = build(relations=[R("staff", "staff_id", "depts", "dept_id")])   # orphans
    assert r.status_code == 422
    assert db().execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 3


def test_child_insert_with_missing_parent_blocked_and_parent_delete_blocked():
    up(depts=DEPTS, staff=STAFF); build()
    blocked("INSERT INTO staff VALUES (99, 77)")
    blocked("DELETE FROM depts WHERE dept_id = 1")


# ---------- 3. referential actions ----------
def _action(on_delete=None, **extra):
    up(depts=DEPTS, staff=STAFF)
    one([R("staff", "dept_id", "depts", "dept_id", on_delete=on_delete, **extra)])


def test_cascade_deletes_children():
    _action("CASCADE"); con = db()
    con.execute("DELETE FROM depts WHERE dept_id = 1")
    assert con.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 1


def test_set_null_nulls_children():
    _action("SET NULL"); con = db()
    con.execute("DELETE FROM depts WHERE dept_id = 1")
    assert con.execute("SELECT COUNT(*) FROM staff WHERE dept_id IS NULL").fetchone()[0] == 2


def test_restrict_blocks_parent_delete():
    _action("RESTRICT")
    blocked("DELETE FROM depts WHERE dept_id = 1")


def test_set_default_uses_default():
    _action("SET DEFAULT", child_default=3); con = db()
    con.execute("DELETE FROM depts WHERE dept_id = 1")
    assert con.execute("SELECT COUNT(*) FROM staff WHERE dept_id = 3").fetchone()[0] == 2


def test_set_default_needs_valid_default():
    up(depts=DEPTS, staff=STAFF)
    assert build(relations=[R("staff", "dept_id", "depts", "dept_id", on_delete="SET DEFAULT")]).status_code == 422
    assert build(relations=[R("staff", "dept_id", "depts", "dept_id", on_delete="SET DEFAULT",
                              child_default=999)]).status_code == 422


def test_set_null_on_primary_key_column_refused():
    up(depts=DEPTS, staff=pd.DataFrame({"dept_id": [1, 2], "x": [5, 6]}))
    r = build(primary_keys={"staff": "dept_id"},
              relations=[R("staff", "dept_id", "depts", "dept_id", on_delete="SET NULL")])
    assert r.status_code == 422 and "SET NULL" in r.json()["detail"]


def test_unknown_field_is_an_error_not_ignored():
    up(depts=DEPTS, staff=STAFF)
    assert build(relations=[{**R("staff", "dept_id", "depts", "dept_id"), "ondelete": "CASCADE"}]).status_code == 422


# ---------- 4. nullability & indexing ----------
def test_fk_accepts_null_by_default():
    up(depts=DEPTS, staff=STAFF); build()
    db().execute("INSERT INTO staff VALUES (50, NULL)")               # no parent link, allowed


def test_required_makes_fk_not_null():
    up(depts=DEPTS, staff=STAFF); one([R("staff", "dept_id", "depts", "dept_id", required=True)])
    blocked("INSERT INTO staff VALUES (50, NULL)")


def test_required_rejected_when_data_has_nulls():
    up(depts=DEPTS, staff=pd.DataFrame({"staff_id": [1, 2], "dept_id": [1, None]}))
    assert build(relations=[R("staff", "dept_id", "depts", "dept_id", required=True)]).status_code == 422


def test_fk_column_is_indexed():
    up(depts=DEPTS, staff=STAFF); build()
    assert [r[1] for r in db().execute("PRAGMA index_list(staff)")] == ["ix_staff_dept_id"]


# ---------- cardinality ----------
def test_cardinality_reported_and_one_to_one_enforced_on_request():
    up(depts=DEPTS, staff=STAFF)
    assert c.get("/relations/preview").json()["relations"][0]["cardinality"] == "1:N"
    up(profiles=pd.DataFrame({"profile_id": [1, 2], "dept_id": [1, 2]}), depts=DEPTS)
    one([R("profiles", "dept_id", "depts", "dept_id", unique=True)])
    blocked("INSERT INTO profiles VALUES (3, 1)")


def test_unique_rejected_when_data_is_not_one_to_one():
    up(depts=DEPTS, staff=STAFF)
    assert build(relations=[R("staff", "dept_id", "depts", "dept_id", unique=True)]).status_code == 422


# ---------- self reference, shared keys, overrides ----------
def test_self_reference_loads_even_when_child_row_comes_first():
    up(emps=pd.DataFrame({"emp_id": [1, 2, 3], "manager_id": [3, 3, None]}))
    r = build().json()
    assert [(f["child_table"], f["parent_table"]) for f in r["foreign_keys_applied"]] == [("emps", "emps")]
    with pytest.raises(sqlite3.IntegrityError):
        con = db(); con.execute("INSERT INTO emps VALUES (9, 77)"); con.commit()


def test_shared_key_tables_need_a_parent_choice_or_hint():
    same = lambda col: pd.DataFrame({"employee_id": ["E1", "E2"], col: [1, 2]})
    up(staff_a=same("a"), staff_b=same("b"))
    assert build().json()["foreign_keys_applied"] == []                # ambiguous: refuses to guess
    for x in c.get("/uploads/tables").json(): c.delete("/uploads/tables/" + x["table_name"])
    up(employee_basic_info=pd.DataFrame({"employee_id": ["E1", "E2"], "n": ["x", "y"]}),
       employee_pay=pd.DataFrame({"employee_id": ["E1", "E2"], "pay": [1, 2]}))
    assert build().json()["foreign_keys_applied"] == []                # 'suggested' is opt-in
    assert len(build(include_suggested=True).json()["foreign_keys_applied"]) == 1


def test_bad_primary_key_override_rejected():
    up(depts=DEPTS, staff=STAFF)
    r = build(primary_keys={"staff": "dept_id"})                        # duplicates
    assert r.status_code == 422 and "duplicate" in r.json()["detail"]


def test_rebuild_repeatedly_works():
    up(depts=DEPTS, staff=STAFF)
    assert [build().status_code for _ in range(3)] == [200, 200, 200]


def test_unrelated_id_columns_are_not_linked():
    up(customers=pd.DataFrame({"customer_id": [1, 2, 3], "n": list("abc")}),
       orders=pd.DataFrame({"order_id": [1, 2, 3], "qty": [1, 2, 3]}))
    assert build().json()["foreign_keys_applied"] == []