"""
Regression test for a copy-paste bug in add_formula_owner(): the kind-1
owner's spanning_row_dependencies used num_cols - 1 for
bottom_right_row on both total_range_for_table and body_range_for_table
(should be num_rows - 1). Found by ownaudit.py against a freshly
generated document; confirmed present on an unpatched checkout too
(independent of the kind-1 owner UUID/adoption fix and of the
formula-writing feature -- add_formula_owner() runs for every
add_table() call regardless of whether a formula is ever written to
the table).

Numbers.app's own behaviour here (repair vs. silent tolerance) was not
established live -- this fix is made on general correctness grounds
(the bottom-right corner of a table's own row-spanning range should
obviously use the table's row count, not its column count) and pinned
down with a direct structural check.
"""

from numbers_parser import Document
from numbers_parser.constants import OwnerKind


def _new_table_kind1_owner(model, table_id):
    table_info_id = model.table_info_id(table_id)
    owners = [
        model.objects[obj_id]
        for obj_id in model.find_refs("FormulaOwnerDependenciesArchive")
        if model.objects[obj_id].owner_kind == OwnerKind.TABLE_MODEL
        and model.objects[obj_id].HasField("formula_owner")
        and model.objects[obj_id].formula_owner.identifier == table_info_id
    ]
    assert len(owners) == 1
    return owners[0]


def test_add_table_spanning_ranges_match_table_dimensions():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    table2 = sheet.tables[1]
    model = doc._model

    owner = _new_table_kind1_owner(model, table2._table_id)
    rows, cols = table2.num_rows, table2.num_cols
    assert rows != cols, "test needs a non-square table to catch a row/column mixup"

    for spanning in (owner.spanning_column_dependencies, owner.spanning_row_dependencies):
        for range_field in ("total_range_for_table", "body_range_for_table"):
            r = getattr(spanning, range_field)
            assert r.bottom_right_column == cols - 1, range_field
            assert r.bottom_right_row == rows - 1, range_field


def test_add_table_spanning_ranges_respect_header_offsets():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2", num_header_rows=2, num_header_cols=1)
    table2 = sheet.tables[1]
    model = doc._model

    owner = _new_table_kind1_owner(model, table2._table_id)
    for spanning in (owner.spanning_column_dependencies, owner.spanning_row_dependencies):
        body = spanning.body_range_for_table
        assert body.top_left_row == 2
        assert body.top_left_column == 1
