import re
import warnings
from datetime import datetime, timedelta

from numbers_parser.exceptions import UnsupportedWarning
from numbers_parser.generated import TSCEArchives_pb2 as TSCEArchives
from numbers_parser.generated.functionmap import FUNCTION_MAP

FUNCTION_NAME_TO_ID = {v: k for k, v in FUNCTION_MAP.items()}

_ATOM_PRECEDENCE = 99


class _PrecStr(str):
    """
    A rendered sub-expression tagged with the precedence of its own
    outermost operator, so an enclosing operator can decide whether it
    needs to be wrapped in parentheses to preserve the original
    grouping. Plain strings (cell references, numbers, function
    calls, ranges) are left untagged and treated as atomic: they never
    need parentheses under any enclosing operator.
    """

    def __new__(cls, value, precedence):
        obj = str.__new__(cls, value)
        obj.precedence = precedence
        return obj


# Lower binds looser, matching standard spreadsheet operator
# precedence: comparisons loosest, then concatenation, then addition/
# subtraction, then multiplication/division/unary-minus/percent
# (sharing a tier -- see _SCALING_OPS below), then exponentiation.
_PRECEDENCE = {
    "equals": 1,
    "not_equals": 1,
    "greater_than": 1,
    "greater_than_or_equal": 1,
    "less_than": 1,
    "less_than_or_equal": 1,
    "concat": 2,
    "add": 3,
    "sub": 3,
    "mul": 4,
    "div": 4,
    "negate": 4,
    "percent": 4,
    "power": 6,
}

# mul, div, negate, and percent are all pure scaling operations (multiply
# by a constant), which is why they share one precedence tier above: any
# one of them commutes exactly with any other, e.g. -(A*B) == (-A)*B ==
# A*(-B), and (A+B)/100 has no equivalent shortcut through percent, but
# (A*B)% == A*(B%) does -- confirmed by direct calculation, not assumed.
# That means negate()/percent()'s own operand never needs parentheses
# when it comes from this same family, but DOES need them from any other
# operator, INCLUDING power -- despite power's _PRECEDENCE value being
# numerically higher. Real spreadsheet precedence (confirmed against
# Excel's own documented operator precedence) actually ranks unary minus
# and percent tighter than exponentiation (-2^2 is 4 in Excel, not -4,
# the well-known inverse-of-plain-math-convention spreadsheet quirk) --
# but negate/percent can't simply be given a higher numeric tier than
# power, because that would also make them wrongly start
# parenthesising a same-family mul/div operand. So negate() and
# percent() use their own "is this the same family, or atomic" check
# (_wrap_unary below) instead of the generic magnitude-based
# _wrap_left/_wrap_right every other operator uses.

# Operators where a child at the SAME precedence can be flattened on
# the right without parentheses, confirmed by direct calculation
# rather than assumed: (A op B) op C == A op (B op C) for these three
# (string concatenation, and addition/multiplication chains where
# subtraction/division are just addition/multiplication by an
# inverse). sub and div are deliberately excluded: A-(B-C) != A-B-C,
# and A/(B/C) != A/B/C, so a same-precedence child on the right of
# those two always needs parentheses to preserve the original
# grouping.
_RIGHT_ASSOC_SAFE = {"add", "mul", "concat"}


def _prec(operand) -> int:
    return getattr(operand, "precedence", _ATOM_PRECEDENCE)


def _wrap_left(operand, parent_op: str) -> str:
    if _prec(operand) < _PRECEDENCE[parent_op]:
        return f"({operand})"
    return str(operand)


def _wrap_right(operand, parent_op: str) -> str:
    p = _prec(operand)
    parent_p = _PRECEDENCE[parent_op]
    if p < parent_p or (p == parent_p and parent_op not in _RIGHT_ASSOC_SAFE):
        return f"({operand})"
    return str(operand)


def _wrap_unary(operand, own_precedence: int) -> str:
    """
    Wrap negate()/percent()'s own operand, unless it's atomic or from
    the same scaling-operator family (see _PRECEDENCE's own comment
    above) -- a plain magnitude comparison against own_precedence isn't
    enough here, since power's numeric tier is higher than negate's/
    percent's despite needing parentheses.
    """
    p = _prec(operand)
    if p != _ATOM_PRECEDENCE and p != own_precedence:
        return f"({operand})"
    return str(operand)



class Formula(list):
    def __init__(self, model, table_id, row, col) -> None:
        self._stack = []
        self._model = model
        self._table_id = table_id
        self.row = row
        self.col = col

    def __str__(self) -> str:
        return "".join(reversed([str(x) for x in self._stack]))

    def pop(self) -> str:
        return self._stack.pop()

    def popn(self, num_args: int) -> tuple:
        values = ()
        for _ in range(num_args):
            values += (self._stack.pop(),)
        return values

    def push(self, val: str) -> None:
        self._stack.append(val)

    def add(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'add')}+{_wrap_right(arg2, 'add')}"
        self.push(_PrecStr(text, _PRECEDENCE["add"]))

    def array(self, *args) -> None:
        node = args[2]
        num_rows = node.AST_array_node_numRow
        num_cols = node.AST_array_node_numCol
        if num_rows == 1:
            # 1-dimensional array: {a,b,c,d}
            args = self.popn(num_cols)
            args = ",".join(reversed(args))
            self.push(f"{{{args}}}")
        else:
            # 2-dimensional array: {a,b;c,d}
            rows = []
            for _row_num in range(num_rows):
                args = self.popn(num_cols)
                args = ",".join(reversed(args))
                rows.append(f"{args}")
            args = ";".join(reversed(rows))
            self.push(f"{{{args}}}")

    def boolean(self, *args) -> None:
        node = args[2]
        if node.HasField("AST_token_node_boolean"):
            self.push(str(node.AST_token_node_boolean).upper())
        else:
            self.push(str(node.AST_boolean_node_boolean).upper())

    def concat(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'concat')}&{_wrap_right(arg2, 'concat')}"
        self.push(_PrecStr(text, _PRECEDENCE["concat"]))

    def date(self, *args) -> None:
        # Date literals exported as DATE()
        node = args[2]
        dt = datetime(2001, 1, 1) + timedelta(seconds=node.AST_date_node_dateNum)  # noqa: DTZ001
        self.push(f"DATE({dt.year},{dt.month},{dt.day})")

    def div(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'div')}÷{_wrap_right(arg2, 'div')}"
        self.push(_PrecStr(text, _PRECEDENCE["div"]))

    def empty(self, *args) -> None:
        self.push("")

    def equals(self, *args) -> None:
        # Arguments appear to be reversed
        arg1, arg2 = self.popn(2)
        text = f"{_wrap_left(arg2, 'equals')}={_wrap_right(arg1, 'equals')}"
        self.push(_PrecStr(text, _PRECEDENCE["equals"]))

    def function(self, *args) -> None:
        node = args[2]
        num_args = node.AST_function_node_numArgs
        node_index = node.AST_function_node_index
        if node_index not in FUNCTION_MAP:
            table_name = self._model.table_name(self._table_id)
            warnings.warn(
                f"{table_name}@[{self.row},{self.col}]: function ID {node_index} is unsupported",
                UnsupportedWarning,
                stacklevel=2,
            )
            func_name = "UNDEFINED!"
        else:
            func_name = FUNCTION_MAP[node_index]

        if len(self._stack) < num_args:
            table_name = self._model.table_name(self._table_id)
            warnings.warn(
                f"{table_name}@[{self.row},{self.col}]: stack too small for {func_name}",
                UnsupportedWarning,
                stacklevel=2,
            )
            num_args = len(self._stack)

        args = self.popn(num_args)
        args = ",".join(reversed([str(x) for x in args]))
        self.push(f"{func_name}({args})")

    def greater_than(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'greater_than')}>{_wrap_right(arg2, 'greater_than')}"
        self.push(_PrecStr(text, _PRECEDENCE["greater_than"]))

    def greater_than_or_equal(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = (
            f"{_wrap_left(arg1, 'greater_than_or_equal')}"
            f"≥{_wrap_right(arg2, 'greater_than_or_equal')}"
        )
        self.push(_PrecStr(text, _PRECEDENCE["greater_than_or_equal"]))

    def less_than(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'less_than')}<{_wrap_right(arg2, 'less_than')}"
        self.push(_PrecStr(text, _PRECEDENCE["less_than"]))

    def less_than_or_equal(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = (
            f"{_wrap_left(arg1, 'less_than_or_equal')}"
            f"≤{_wrap_right(arg2, 'less_than_or_equal')}"
        )
        self.push(_PrecStr(text, _PRECEDENCE["less_than_or_equal"]))

    def list(self, *args) -> None:
        node = args[2]
        args = self.popn(node.AST_list_node_numArgs)
        args = ",".join(reversed([str(x) for x in args]))
        self.push(f"({args})")

    def mul(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'mul')}×{_wrap_right(arg2, 'mul')}"
        self.push(_PrecStr(text, _PRECEDENCE["mul"]))

    def negate(self, *args) -> None:
        arg1 = self.pop()
        text = f"-{_wrap_unary(arg1, _PRECEDENCE['negate'])}"
        self.push(_PrecStr(text, _PRECEDENCE["negate"]))

    def not_equals(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'not_equals')}≠{_wrap_right(arg2, 'not_equals')}"
        self.push(_PrecStr(text, _PRECEDENCE["not_equals"]))

    def number(self, *args) -> None:
        node = args[2]
        if node.AST_number_node_decimal_high == 0x3040000000000000:
            # Integer: don't use decimals
            self.push(str(node.AST_number_node_decimal_low))
        else:
            self.push(number_to_str(node.AST_number_node_number))

    def percent(self, *args) -> None:
        arg1 = self.pop()
        text = f"{_wrap_unary(arg1, _PRECEDENCE['percent'])}%"
        self.push(_PrecStr(text, _PRECEDENCE["percent"]))

    def power(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'power')}^{_wrap_right(arg2, 'power')}"
        self.push(_PrecStr(text, _PRECEDENCE["power"]))

    def range(self, *args) -> None:
        arg2, arg1 = [str(x) for x in self.popn(2)]
        func_range = "(" in arg1 or "(" in arg2
        if "::" in arg1 and not func_range:
            # Assumes references are not cross-table
            arg1_parts = arg1.split("::")
            arg2_parts = arg2.split("::")
            self.push(f"{arg1_parts[0]}::{arg1_parts[1]}:{arg2_parts[1]}")
        else:
            self.push(f"{arg1}:{arg2}")

    def string(self, *args) -> None:
        node = args[2]
        # Numbers does not escape quotes in the AST; in the app, they are
        # doubled up just like in Excel
        value = node.AST_string_node_string.replace('"', '""')
        self.push(f'"{value}"')

    def sub(self, *args) -> None:
        arg2, arg1 = self.popn(2)
        text = f"{_wrap_left(arg1, 'sub')}-{_wrap_right(arg2, 'sub')}"
        self.push(_PrecStr(text, _PRECEDENCE["sub"]))

    def xref(self, *args) -> None:
        (row, col, node) = args
        self.push(self._model.node_to_ref(self._table_id, row, col, node))


NODE_FUNCTION_MAP = {
    "ADDITION_NODE": "add",
    "APPEND_WHITESPACE_NODE": None,
    "ARRAY_NODE": "array",
    "BEGIN_EMBEDDED_NODE_ARRAY": None,
    "BEGIN_THUNK_NODE": None,
    "BOOLEAN_NODE": "boolean",
    "CELL_REFERENCE_NODE": "xref",
    "COLON_NODE": "range",
    "COLON_NODE_WITH_UIDS": "range",
    "COLON_TRACT_NODE": "xref",
    "CONCATENATION_NODE": "concat",
    "DATE_NODE": "date",
    "DIVISION_NODE": "div",
    "EMPTY_ARGUMENT_NODE": "empty",
    "END_THUNK_NODE": None,
    "EQUAL_TO_NODE": "equals",
    "FUNCTION_NODE": "function",
    "GREATER_THAN_NODE": "greater_than",
    "GREATER_THAN_OR_EQUAL_TO_NODE": "greater_than_or_equal",
    "LESS_THAN_NODE": "less_than",
    "LESS_THAN_OR_EQUAL_TO_NODE": "less_than_or_equal",
    "LIST_NODE": "list",
    "MULTIPLICATION_NODE": "mul",
    "NEGATION_NODE": "negate",
    "NOT_EQUAL_TO_NODE": "not_equals",
    "NUMBER_NODE": "number",
    "PERCENT_NODE": "percent",
    "POWER_NODE": "power",
    "PLUS_SIGN_NODE": None,
    "PREPEND_WHITESPACE_NODE": None,
    "STRING_NODE": "string",
    "SUBTRACTION_NODE": "sub",
    "TOKEN_NODE": "boolean",
}


class TableFormulas:
    def __init__(self, model, table_id) -> None:
        self._model = model
        self._table_id = table_id
        self._formula_type_lookup = {
            k: v.name
            for k, v in TSCEArchives._ASTNODEARRAYARCHIVE_ASTNODETYPE.values_by_number.items()
        }

    def formula(self, formula_key, row, col):
        all_formulas = self._model.formula_ast(self._table_id)
        if formula_key not in all_formulas:
            table_name = self._model.table_name(self._table_id)
            warnings.warn(
                f"{table_name}@[{row},{col}]: key #{formula_key} not found",
                UnsupportedWarning,
                stacklevel=2,
            )
            return "INVALID_KEY!(" + str(formula_key) + ")"

        formula = Formula(self._model, self._table_id, row, col)
        for node in all_formulas[formula_key]:
            node_type = self._formula_type_lookup[node.AST_node_type]
            if node_type == "REFERENCE_ERROR_WITH_UIDS":
                formula.push("#REF!")
            elif node_type not in NODE_FUNCTION_MAP:
                table_name = self._model.table_name(self._table_id)
                warnings.warn(
                    f"{table_name}@[{row},{col}]: node type {node_type} is unsupported",
                    UnsupportedWarning,
                    stacklevel=2,
                )
            elif NODE_FUNCTION_MAP[node_type] is not None:
                func = getattr(formula, NODE_FUNCTION_MAP[node_type])
                func(row, col, node)

        return str(formula)


def number_to_str(v: int) -> str:
    """Format a float as a string."""
    # Number is never negative; formula will use NEGATION_NODE
    v_str = repr(v)
    if "e" in v_str:
        number, exp = v_str.split("e")
        number = re.sub(r"[,-.]", "", number)
        zeroes = "0" * (abs(int(exp)) - 1)
        if int(exp) > 0:
            return f"{number}{zeroes}"
        return f"0.{zeroes}{number}"
    return v_str
