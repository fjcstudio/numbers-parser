import pytest
import pytest_check as check

from numbers_parser import Document, ErrorCell, UnsupportedWarning
from numbers_parser.constants import OwnerKind
from numbers_parser.generated import TSCEArchives_pb2 as TSCEArchives
from numbers_parser.numbers_uuid import NumbersUUID

_CELL_REFERENCE_NODE = 36

TABLE_1_FORMULAS = [
    [None, "A1", "$B$1=1"],
    [None, "A1+A2", "A$2&B2"],
    [None, "A1×A2", "NOW()"],
    [None, "A1-A2", "NOW()+0.1"],
    [None, "A1÷A2", "$C4-C3"],
    [None, "SUM(A1:A2)", "IF(A6>6,TRUE,FALSE)"],
    [None, "MEDIAN(A1:A2)", "IF(A7>0,TRUE,FALSE)"],
    [None, "AVERAGE(A1:A2)", "A8≠10"],
    ["A9", None, None],
]

TABLE_2_FORMULAS = [
    [None, "A1&A2&A3"],
    [None, "LEN(A2)+LEN(A3)"],
    [None, "LEFT(A3,1)"],
    [None, "MID(A4,2,2)"],
    [None, "RIGHT(A5,2)"],
    [None, 'FIND("_",A6)'],
    [None, 'FIND("YYY",A7)'],
    [None, 'IF(FIND("_",A8)>2,A1,A2)'],
    [None, "100×(A9×2)%"],
    [None, 'IF(A10<5,"smaller","larger")'],
    [None, 'IF(A11≤5,"smaller","larger")'],
]


def compare_tables(table, ref):
    for row in range(table.num_rows):
        for col in range(table.num_cols):
            if ref[row][col] is None:
                check.is_none(
                    table.cell(row, col).formula,
                    f"!exists@[{row},{col}]",
                )
            else:
                check.is_true(table.cell(row, col).is_formula, f"formula@[{row},{col}]")
                check.is_not_none(
                    table.cell(row, col).formula,
                    f"exists@[{row},{col}]",
                )
                check.equal(
                    table.cell(row, col).formula,
                    ref[row][col],
                    f"formula@[{row},{col}]",
                )


def test_table_functions():
    doc = Document("tests/data/test-10.numbers")
    sheets = doc.sheets
    table = sheets[0].tables[0]
    compare_tables(table, TABLE_1_FORMULAS)

    table = sheets[1].tables[0]
    compare_tables(table, TABLE_2_FORMULAS)


def test_exceptions(configurable_save_file):
    def get_formula(doc):
        table_id = doc.sheets[0].tables[0]._table_id
        base_data_store = doc._model.objects[table_id].base_data_store
        formula_table_id = base_data_store.formula_table.identifier
        formula_table = doc._model.objects[formula_table_id]
        return formula_table.entries[0].formula

    doc = Document("tests/data/simple-func.numbers")
    formula = get_formula(doc)
    formula.AST_node_array.AST_node[2].AST_function_node_index = 999
    with pytest.warns(UnsupportedWarning) as record:
        value = doc.sheets[0].tables[0].cell(0, 1).formula
    assert value == "UNDEFINED!(1,2)"
    assert str(record[0].message) == "Table 1@[0,1]: function ID 999 is unsupported"

    doc = Document("tests/data/simple-func.numbers")
    formula = get_formula(doc)
    formula.AST_node_array.AST_node[2].AST_function_node_numArgs = 3
    with pytest.warns(UnsupportedWarning) as record:
        value = doc.sheets[0].tables[0].cell(0, 1).formula
    assert str(record[0].message) == "Table 1@[0,1]: stack too small for SUM"

    doc = Document("tests/data/simple-func.numbers")
    formula = get_formula(doc)
    formula.AST_node_array.AST_node[2].AST_function_node_numArgs = 3
    with pytest.warns(UnsupportedWarning) as record:
        value = doc.sheets[0].tables[0].cell(0, 1).formula
    assert str(record[0].message) == "Table 1@[0,1]: stack too small for SUM"

    doc = Document("tests/data/simple-func.numbers")
    formula = get_formula(doc)
    formula.AST_node_array.AST_node[2].AST_node_type = 68
    with pytest.warns(UnsupportedWarning) as record:
        value = doc.sheets[0].tables[0].cell(0, 1).formula
    assert str(record[0].message) == "Table 1@[0,1]: node type VIEW_TRACT_REF_NODE is unsupported"

    doc = Document("tests/data/simple-func.numbers")
    doc.sheets[0].tables[0].cell(0, 1)._formula_id = 999
    with pytest.warns(UnsupportedWarning) as record:
        _ = doc.sheets[0].tables[0].cell(0, 1).formula

    assert str(record[0].message) == "Table 1@[0,1]: key #999 not found"


def test_error_cell_formula_survives_save(configurable_save_file):
    doc = Document("tests/data/issue-42.numbers")
    table = doc.sheets[0].tables[0]

    error_cells = [
        (row, col)
        for row in range(table.num_rows)
        for col in range(table.num_cols)
        if isinstance(table.cell(row, col), ErrorCell)
    ]
    assert len(error_cells) > 0
    for row, col in error_cells:
        assert table.cell(row, col).is_formula

    doc.save(configurable_save_file)

    reopened = Document(configurable_save_file)
    reopened_table = reopened.sheets[0].tables[0]
    for row, col in error_cells:
        cell = reopened_table.cell(row, col)
        assert isinstance(cell, ErrorCell), f"cell({row},{col}) became {type(cell).__name__}"
        assert cell.is_formula


def test_cross_table_reference_resolves_via_table_model_owner():
    # Every table this library creates gets TWO FormulaOwnerDependenciesArchive
    # records: a HAUNTED_OWNER (the only one table_uuids_to_id() used to check)
    # and a TABLE_MODEL owner with its own, independent UUID pointing at the
    # same table via a TableInfoArchive. Confirmed empirically that Numbers.app
    # can embed either UUID in a cross-table reference's own AST -- this builds
    # a reference using the second, previously-unresolvable one directly, since
    # doing so needs no real Numbers.app-authored fixture: the TABLE_MODEL
    # owner already exists in any in-memory Document()'s own object graph.
    doc = Document()
    doc.add_sheet("Sheet 2", "Table B")
    table_a = doc.sheets[0].tables[0]
    table_b = doc.sheets["Sheet 2"].tables["Table B"]
    model = table_a._model

    table_b_info_id = model.table_info_id(table_b._table_id)
    table_model_owner_ids = [
        obj_id
        for obj_id in model.find_refs("FormulaOwnerDependenciesArchive")
        if model.objects[obj_id].owner_kind == OwnerKind.TABLE_MODEL
        and model.objects[obj_id].formula_owner.identifier == table_b_info_id
    ]
    assert len(table_model_owner_ids) == 1
    table_model_owner = model.objects[table_model_owner_ids[0]]

    ref_node = TSCEArchives.ASTNodeArrayArchive.ASTNodeArchive()
    ref_node.AST_node_type = _CELL_REFERENCE_NODE
    ref_node.AST_row.row = 0
    ref_node.AST_row.absolute = True
    ref_node.AST_column.column = 0
    ref_node.AST_column.absolute = True
    ref_node.AST_cross_table_reference_extra_info.table_id.CopyFrom(
        NumbersUUID(table_model_owner.formula_owner_uid).protobuf4,
    )

    aa = TSCEArchives.ASTNodeArrayArchive()
    aa.AST_node.append(ref_node)
    fa = TSCEArchives.FormulaArchive()
    fa.AST_node_array.CopyFrom(aa)
    key = model._formulas.lookup_key(table_a._table_id, fa)
    table_a.write(1, 0, 0.0)
    table_a.cell(1, 0)._formula_id = key

    assert table_a.cell(1, 0).formula == "Table B::$A$1"


def test_named_ranges():
    doc = Document("tests/data/create-formulas.numbers")
    table = doc.sheets["Main Sheet"].tables["Reference Tests"]
    for row_num, row in enumerate(table.iter_rows(min_row=1), start=1):
        if len(row) == 2 or row[2].value:
            assert row[0].formula == row[1].value, f"Reference Tests: row {row_num + 1}"
