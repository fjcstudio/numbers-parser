"""
Regression tests for formula-to-text rendering losing operator
precedence for nested expressions.

Formula.add()/sub()/mul()/div()/negate()/etc (formula.py) used to
concatenate operand strings with no memory of what produced them, so
a nested expression like (A1+B1)*2 rendered back as "A1+B1*2",
identical to what an unparenthesised "A1+B1*2" would also render as:
silently different math (11 vs 16 for A1=5, B1=3) if that text were
ever read back. Confirmed present on the underlying AST already
before this fix (the stored node sequence was always correct, a
CELL_REFERENCE_NODE pair, then ADDITION_NODE, then NUMBER_NODE, then
MULTIPLICATION_NODE, in that unambiguous RPN order); only the text
reconstruction was lossy.

This test file is deliberately standalone: it builds formulas via
raw protobuf AST nodes directly, the same low-level mechanism
formula-writing itself is built on, rather than importing the
formula-writing feature's own DSL. The bug and this fix are entirely
in the read side (formula.py) and have nothing to do with
formula-writing; this file should apply and pass on its own, with or
without that feature present.
"""

from numbers_parser import Document
from numbers_parser.generated import TSCEArchives_pb2 as TSCEArchives

_ADD, _SUB, _MUL, _DIV, _POWER, _NEG, _PERCENT, _NUM, _REF = 1, 2, 3, 4, 5, 13, 15, 17, 36


def _num_node(value):
    node = TSCEArchives.ASTNodeArrayArchive.ASTNodeArchive()
    node.AST_node_type = _NUM
    node.AST_number_node_number = float(value)
    return node


def _ref_node(formula_row, formula_col, row, col):
    node = TSCEArchives.ASTNodeArrayArchive.ASTNodeArchive()
    node.AST_node_type = _REF
    node.AST_row.row = row - formula_row
    node.AST_row.absolute = False
    node.AST_column.column = col - formula_col
    node.AST_column.absolute = False
    return node


def _op_node(op_type):
    node = TSCEArchives.ASTNodeArrayArchive.ASTNodeArchive()
    node.AST_node_type = op_type
    return node


def _write_raw_formula(table, row, col, node_sequence, value):
    aa = TSCEArchives.ASTNodeArrayArchive()
    for node in node_sequence:
        aa.AST_node.append(node)
    fa = TSCEArchives.FormulaArchive()
    fa.AST_node_array.CopyFrom(aa)
    key = table._model._formulas.lookup_key(table._table_id, fa)
    table.write(row, col, float(value))
    table.cell(row, col)._formula_id = key


def _table_with_values():
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(0, 0, 5.0)
    table.write(0, 1, 3.0)
    table.write(0, 2, 2.0)
    return doc, table


def test_left_operand_needing_parens():
    # (A1+B1)*2 != A1+B1*2 (16 vs 11 for A1=5, B1=3).
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_ADD),
        _num_node(2), _op_node(_MUL),
    ]
    _write_raw_formula(table, 1, 0, seq, 16.0)
    assert table.cell(1, 0).formula == "(A1+B1)×2.0"


def test_right_operand_at_lower_precedence_is_safe_unparenthesised():
    # A1+B1*2 already means A1+(B1*2), the standard reading, since *
    # binds tighter than + regardless of parentheses.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _num_node(2), _op_node(_MUL),
        _op_node(_ADD),
    ]
    _write_raw_formula(table, 1, 0, seq, 11.0)
    assert table.cell(1, 0).formula == "A1+B1×2.0"


def test_subtraction_is_not_associative_on_the_right():
    # A1-(B1-C1) != A1-B1-C1 (4 vs 0 for A1=5, B1=3, C1=2).
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0),
        _ref_node(1, 0, 0, 1), _ref_node(1, 0, 0, 2), _op_node(_SUB),
        _op_node(_SUB),
    ]
    _write_raw_formula(table, 1, 0, seq, 4.0)
    assert table.cell(1, 0).formula == "A1-(B1-C1)"


def test_subtraction_flattens_safely_on_the_left():
    # (A1-B1)-C1 == A1-B1-C1: safe to render without parens.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_SUB),
        _ref_node(1, 0, 0, 2), _op_node(_SUB),
    ]
    _write_raw_formula(table, 1, 0, seq, 0.0)
    assert table.cell(1, 0).formula == "A1-B1-C1"


def test_division_is_not_associative_on_the_right():
    # A1/(B1/C1) != A1/B1/C1 (10/3 vs 5/6 for A1=5, B1=3, C1=2).
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0),
        _ref_node(1, 0, 0, 1), _ref_node(1, 0, 0, 2), _op_node(_DIV),
        _op_node(_DIV),
    ]
    _write_raw_formula(table, 1, 0, seq, 5.0 / (3.0 / 2.0))
    assert table.cell(1, 0).formula == "A1÷(B1÷C1)"


def test_division_flattens_safely_on_the_left():
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_DIV),
        _ref_node(1, 0, 0, 2), _op_node(_DIV),
    ]
    _write_raw_formula(table, 1, 0, seq, (5.0 / 3.0) / 2.0)
    assert table.cell(1, 0).formula == "A1÷B1÷C1"


def test_addition_flattens_safely_on_the_left():
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_SUB),
        _ref_node(1, 0, 0, 2), _op_node(_ADD),
    ]
    _write_raw_formula(table, 1, 0, seq, 6.0)
    assert table.cell(1, 0).formula == "A1-B1+C1"


def test_addition_flattens_safely_on_the_right():
    # A separate table/session from the left-side test above,
    # deliberately: reading one formula's text then writing a second
    # elsewhere in the same session is a different, already-fixed bug
    # (formula_ast cache staleness) this test has no need to exercise.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0),
        _ref_node(1, 0, 0, 1), _ref_node(1, 0, 0, 2), _op_node(_SUB),
        _op_node(_ADD),
    ]
    _write_raw_formula(table, 1, 0, seq, 6.0)
    assert table.cell(1, 0).formula == "A1+B1-C1"


def test_negate_needs_parens_around_additive_operand():
    # -(A1+B1) != -A1+B1 (-8 vs 2 for A1=5, B1=3).
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_ADD),
        _op_node(_NEG),
    ]
    _write_raw_formula(table, 1, 0, seq, -8.0)
    assert table.cell(1, 0).formula == "-(A1+B1)"


def test_negate_does_not_need_parens_around_multiplicative_operand():
    # -A1*B1 is unambiguous (means -(A1*B1) either way): -15 = -15.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_MUL),
        _op_node(_NEG),
    ]
    _write_raw_formula(table, 1, 0, seq, -15.0)
    assert table.cell(1, 0).formula == "-A1×B1"


def test_negate_as_right_operand_of_multiplication():
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0),
        _ref_node(1, 0, 0, 1), _op_node(_NEG),
        _op_node(_MUL),
    ]
    _write_raw_formula(table, 1, 0, seq, -15.0)
    assert table.cell(1, 0).formula == "A1×-B1"


def test_negate_needs_parens_around_power_operand():
    # -(A1^B1) != -A1^B1: Excel's own documented precedence ranks unary
    # minus tighter than exponentiation (-2^2 is 4 in Excel, not -4), so
    # without parentheses "-A1^B1" re-parses as (-A1)^B1, not -(A1^B1).
    # For A1=5, B1=2: -(A1^B1) = -25, (-A1)^B1 = 25.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_POWER),
        _op_node(_NEG),
    ]
    _write_raw_formula(table, 1, 0, seq, -25.0)
    assert table.cell(1, 0).formula == "-(A1^B1)"


def test_percent_needs_parens_around_additive_operand():
    # (A1+B1)% != A1+B1%: 0.08 vs 5.03 for A1=5, B1=3.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_ADD),
        _op_node(_PERCENT),
    ]
    _write_raw_formula(table, 1, 0, seq, 0.08)
    assert table.cell(1, 0).formula == "(A1+B1)%"


def test_percent_does_not_need_parens_around_multiplicative_operand():
    # (A1*B1)% == A1*(B1%): both 0.15 for A1=5, B1=3.
    _doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_MUL),
        _op_node(_PERCENT),
    ]
    _write_raw_formula(table, 1, 0, seq, 0.15)
    assert table.cell(1, 0).formula == "A1×B1%"


def test_deeply_nested_expression_round_trips_through_save_reload(configurable_save_file):
    doc, table = _table_with_values()
    seq = [
        _ref_node(1, 0, 0, 0), _ref_node(1, 0, 0, 1), _op_node(_SUB),
        _ref_node(1, 0, 0, 2), _num_node(1), _op_node(_ADD),
        _op_node(_MUL),
    ]
    _write_raw_formula(table, 1, 0, seq, (5.0 - 3.0) * (2.0 + 1.0))
    assert table.cell(1, 0).formula == "(A1-B1)×(C1+1.0)"

    doc.save(configurable_save_file)
    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    assert table2.cell(1, 0).formula == "(A1-B1)×(C1+1.0)"
    assert table2.cell(1, 0).value == 6.0
