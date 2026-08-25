from collections import defaultdict

import pytest
from pytest_check import check

from numbers_parser import RGB, Border, BorderType, Cell, Document, MergedCell, xl_rowcol_to_cell


def check_border(cell: Cell, side: str, test_value: str) -> bool:
    border_value = getattr(cell.border, side, None)
    if test_value == "None":
        valid = check.is_none(border_value)
        ref = "None"
    else:
        values = test_value.split(",")
        values[0] = float(values[0])
        values[1] = eval(values[1].replace(";", ","))  # noqa: S307
        if border_value is None:
            return False
        ref = Border(values[0], values[1], values[2])
        cell_name = xl_rowcol_to_cell(cell.row, cell.col)
        sheet_name = cell._model.sheet_name(cell._model.table_id_to_sheet_id(cell._table_id))
        valid = check.equal(
            border_value,
            ref,
            f"{sheet_name}@{cell_name}[{cell.row},{cell.col}].{side}",
        )
    return valid


TAG_TO_BORDER_MAP = {"T": "top", "R": "right", "B": "bottom", "L": "left"}
BORDER_TO_TAG_MAP = {v: k for k, v in TAG_TO_BORDER_MAP.items()}
ALL_BORDERS = ["top", "right", "bottom", "left"]


def unpack_test_string(test_value):
    # Cell test values are of the form:
    #
    # T=1,(0;162;255),dashes
    # R=1,(0;162;255),dashes
    # B=1,(0;162;255),dashes
    # L=1,(0;162;255),dashes
    #
    # Merge cells have multiple values T0, T1, etc.
    tests = test_value.split("\n")
    test_values = {}
    for test in tests:
        tag = TAG_TO_BORDER_MAP[test[0]]
        if test[1] == "=":
            test_values[tag] = test[2:]
        else:
            if tag not in test_values:
                test_values[tag] = defaultdict()
            offset = int(test[1])
            test_values[tag][offset] = test[3:]
    return test_values


def test_exceptions():
    with pytest.raises(TypeError) as e:
        _ = Border(width="invalid")
    assert "width must be a float number" in str(e)

    with pytest.raises(TypeError) as e:
        _ = Border(width="invalid")
    assert "width must be a float number" in str(e)

    with pytest.raises(TypeError) as e:
        _ = Border(color=(0, 0, 0, 0))
    assert "RGB color must be an RGB" in str(e)

    with pytest.raises(TypeError) as e:
        _ = Border(color=(0, 0, 1.0))
    assert "RGB color must be an RGB" in str(e)

    with pytest.raises(TypeError) as e:
        _ = Border(style="invalid")
    assert "invalid border style" in str(e)

    with pytest.raises(TypeError) as e:
        _ = Border(style=999)
    assert "invalid border style value" in str(e)

    border = Border(style=BorderType(3))
    assert str(border) == "Border(width=0.35, color=RGB(r=0, g=0, b=0), style=none)"
    assert str(border) == repr(border)

    doc = Document()
    with pytest.raises(TypeError) as e:
        doc.sheets[0].tables[0].set_cell_border("A1", 1)
    assert "invalid number of arguments to border_value()" in str(e)

    with pytest.raises(TypeError) as e:
        doc.sheets[0].tables[0].set_cell_border("A1", 1, 2, 3, 4)
    assert "invalid number of arguments to border_value()" in str(e)

    with pytest.raises(TypeError) as e:
        doc.sheets[0].tables[0].set_cell_border("A1", "invalid", Border(1.0, RGB(0, 0, 0), "solid"))
    assert "side must be a valid border segment" in str(e)

    with pytest.raises(TypeError) as e:
        doc.sheets[0].tables[0].set_cell_border("A1", "left", object())
    assert "border value must be a Border object" in str(e)

    with pytest.raises(TypeError) as e:
        doc.sheets[0].tables[0].set_cell_border(
            "A1",
            "left",
            Border(1.0, RGB(0, 0, 0), "solid"),
            "invalid",
        )
    assert "border length must be an int" in str(e)


def run_border_tests(filename):
    doc = Document(filename)

    for sheet_name in ["Borders", "Large Borders"]:
        table = doc.sheets[sheet_name].tables[0]

        with pytest.warns() as record:
            table.cell(0, 0).border = object()
        assert len(record) == 1
        assert "cell border values cannot be set" in str(record[0])

        for row, cells in enumerate(table.iter_rows()):
            for col, cell in enumerate(cells):
                if not cell.value or isinstance(cell, MergedCell):
                    continue
                tests = unpack_test_string(cell.value)
                if cell.is_merged:
                    valid = []
                    row_start = row
                    row_end = row + cell.size[0] - 1
                    col_start = col
                    col_end = col + cell.size[1] - 1
                    offset = 0
                    for offset, merge_row_num in enumerate(range(row_start, row_end + 1)):
                        merge_cell = table.cell(merge_row_num, col)
                        valid.append(check_border(merge_cell, "left", tests["left"][offset]))
                        merge_cell = table.cell(merge_row_num, col_end)
                        valid.append(check_border(merge_cell, "right", tests["right"][offset]))

                    for offset, merge_col_num in enumerate(range(col_start, col_end + 1)):
                        merge_cell = table.cell(row, merge_col_num)
                        valid.append(check_border(merge_cell, "top", tests["top"][offset]))
                        merge_cell = table.cell(row_end, merge_col_num)
                        valid.append(check_border(merge_cell, "bottom", tests["bottom"][offset]))
                else:
                    valid = [
                        check_border(cell, "top", tests["top"]),
                        check_border(cell, "right", tests["right"]),
                        check_border(cell, "bottom", tests["bottom"]),
                        check_border(cell, "left", tests["left"]),
                    ]
                assert valid


def test_borders():
    run_border_tests("tests/data/test-styles.numbers")


def test_empty_borders():
    doc = Document("tests/data/test-styles.numbers")
    sheet = doc.sheets["Large Borders"]
    table = sheet.tables[0]

    assert table.cell("F10").border.right is None
    assert table.cell("F10").border.bottom is None
    assert table.cell("F13").border.top is None
    assert table.cell("F13").border.right is None
    assert table.cell("H10").border.left is None
    assert table.cell("H10").border.bottom is None
    assert table.cell("H13").border.left is None
    assert table.cell("H13").border.top is None


def test_edit_borders(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    table = sheet.tables[0]

    table.set_cell_border("B7", "left", Border(8.0, RGB(29, 177, 0), "solid"), 3)
    table.set_cell_border(7, 1, "right", Border(5.0, RGB(29, 177, 0), "dashes"))
    table.merge_cells("C3:F5")

    with pytest.warns(RuntimeWarning) as record:  # noqa: PT031
        table.set_cell_border("C3", ALL_BORDERS, Border())
        table.set_cell_border("D4", ALL_BORDERS, Border())
    assert len(record) == 6
    assert "right edge of [2,2] is merged; border not set" in str(record[0])
    assert "bottom edge of [2,2] is merged; border not set" in str(record[1])
    assert "top edge of [3,3] is merged; border not set" in str(record[2])
    assert "right edge of [3,3] is merged; border not set" in str(record[3])
    assert "bottom edge of [3,3] is merged; border not set" in str(record[4])
    assert "left edge of [3,3] is merged; border not set" in str(record[5])

    table.set_cell_border("C5", "bottom", Border(4.0, RGB(29, 177, 0), "solid"))
    table.set_cell_border("D5", "bottom", Border(4.0, RGB(0, 0, 0), "solid"))
    table.set_cell_border("E5", "bottom", Border(4.0, RGB(0, 162, 255), "solid"))
    table.set_cell_border("F5", "bottom", Border(4.0, RGB(212, 24, 118), "solid"))

    doc.save(configurable_save_file)

    new_doc = Document(configurable_save_file)
    sheet = new_doc.sheets[0]
    table = sheet.tables[0]
    assert table.cell("B7").border.left == Border(8.0, RGB(29, 177, 0), "solid")
    assert table.cell("B8").border.right == Border(5.0, RGB(29, 177, 0), "dashes")

    for merge_ref in ["C4", "D4", "E4", "F4"]:
        assert table.cell(merge_ref).border.top is None
    for merge_ref in ["C4", "D4", "E4"]:
        assert table.cell(merge_ref).border.right is None

    assert table.cell("C5").border.bottom == Border(4.0, RGB(29, 177, 0), "solid")
    assert table.cell("D5").border.bottom == Border(4.0, RGB(0, 0, 0), "solid")
    assert table.cell("E5").border.bottom == Border(4.0, RGB(0, 162, 255), "solid")
    assert table.cell("F5").border.bottom == Border(4.0, RGB(212, 24, 118), "solid")


def invert_border_test(test):
    if test == "None":
        return None, None
    values = test.split(",")
    width = float(values[0])
    color = eval(values[1].replace(";", ","))  # noqa: S307
    style = values[2]
    width = round(width * 2.0, 1) if width < 4.0 else round(width / 2.0, 1)

    color = (abs(200 - color[0]), abs(200 - color[1]), abs(200 - color[2]))

    if style == "solid":
        style = "dashes"
    elif style == "dashes":
        style = "dots"
    elif style == "dots":
        style = "none"
        width = 0.0
        color = (0, 0, 0)
    elif style == "none":
        style = "solid"

    border = Border(width, color, style)

    color = "(" + ";".join([str(x) for x in color]) + ")"
    test_value = ",".join([str(width), color, style])
    return test_value, border


def invert_tests(tests):
    new_tests = []
    new_borders = []
    test_string = ""
    for side, test in tests.items():
        if isinstance(test, str):
            (new_test, border) = invert_border_test(test)
            new_tests.append(new_test)
            new_borders.append(border)
            test_string += BORDER_TO_TAG_MAP[side] + "=" + str(new_tests[-1]) + "\n"
        else:
            for i in range(len(test)):
                (new_test, border) = invert_border_test(test[i])
                new_tests.append(new_test)
                new_borders.append(border)
                test_string += BORDER_TO_TAG_MAP[side] + f"{i}=" + str(new_tests[-1]) + "\n"
    return test_string.strip(), new_borders


def test_extra_borders(configurable_save_file):
    doc = Document("tests/data/test-extra-borders.numbers")
    table = doc.sheets[0].tables[0]
    dots_border = Border(3.0, RGB(0, 162, 255), "dots")
    no_border = Border(0.0, RGB(0, 0, 0), "none")
    coords = [
        (1, 0, "right", 1, dots_border),
        (5, 0, "right", 1, dots_border),
        (11, 0, "right", 1, dots_border),
        (0, 1, "bottom", 1, dots_border),
        (0, 5, "bottom", 1, dots_border),
        (0, 11, "bottom", 1, dots_border),
        (1, 11, "right", 3, dots_border),
        (5, 11, "right", 3, dots_border),
        (9, 11, "right", 3, dots_border),
        (11, 1, "bottom", 3, dots_border),
        (11, 5, "bottom", 3, dots_border),
        (11, 9, "bottom", 3, dots_border),
        (14, 0, "right", 2, dots_border),
        (13, 1, "bottom", 11, dots_border),
        (14, 11, "right", 2, dots_border),
        (15, 1, "bottom", 11, dots_border),
        (17, 1, "bottom", 11, no_border),
        (18, 0, "right", 2, dots_border),
    ]
    for coord in coords:
        (row, col, side, length, border) = coord
        table.set_cell_border(row, col, side, border, length)

    doc.save(configurable_save_file)

    assert Border() == Border(width=0.35, color=RGB(r=0, g=0, b=0), style="solid")

    new_doc = Document(configurable_save_file)
    table = new_doc.sheets[0].tables[0]
    for coord in coords:
        (row, col, side, length, border) = coord
        for _ in range(length):
            assert getattr(table.cell(row, col).border, side) == border


def test_resave_borders(configurable_save_file):
    doc = Document("tests/data/test-styles.numbers")

    style = doc.add_style(font_size=8.0, bold=False, name="Border Test Style")
    for sheet_name in ["Borders", "Large Borders"]:
        table = doc.sheets[sheet_name].tables[0]
        merge_edges = []
        for row, cells in enumerate(table.iter_rows()):
            for col, cell in enumerate(cells):
                if not cell.value:
                    continue
                tests = unpack_test_string(cell.value)
                (test_string, borders) = invert_tests(tests)
                table.write(row, col, test_string, style=style)
                if cell.is_merged:
                    for side in ALL_BORDERS:
                        height = cell.size[0]
                        width = cell.size[1]
                        match side:
                            case "top":
                                for offset in range(width):
                                    border = borders.pop(0)
                                    table.set_cell_border(row, col + offset, side, border, 1)
                            case "right":
                                if height > 1:
                                    for offset in range(height):
                                        border = borders.pop(0)
                                        merge_edges.append(
                                            {
                                                "row": row + offset,
                                                "col": col + width - 1,
                                                "side": side,
                                                "border": border,
                                                "length": height,
                                            },
                                        )
                                else:
                                    border = borders.pop(0)
                                    table.set_cell_border(row, col + width, side, border, 1)
                            case "bottom":
                                if width > 1:
                                    for offset in range(width):
                                        border = borders.pop(0)
                                        merge_edges.append(
                                            {
                                                "row": row + height - 1,
                                                "col": col + offset,
                                                "side": side,
                                                "border": border,
                                                "length": width,
                                            },
                                        )
                                else:
                                    border = borders.pop(0)
                                    table.set_cell_border(row + height - 1, col, side, border, 1)
                            case "left":
                                for offset in range(height):
                                    border = borders.pop(0)
                                    table.set_cell_border(row + offset, col, side, border, 1)
                else:
                    for side, border in zip(ALL_BORDERS, borders, strict=True):
                        if border is not None:
                            table.set_cell_border(row, col, side, border, 1)

        for edge in merge_edges:
            table.set_cell_border(edge["row"], edge["col"], edge["side"], edge["border"], 1)

    doc.save(configurable_save_file)
    run_border_tests(configurable_save_file)


def stroke_runs(table, side: str, index: int) -> list:
    """Return (origin, length, red) for each stroke run in one layer of a table."""
    model = table._model
    sidecar = model.objects[model.objects[table._table_id].stroke_sidecar.identifier]
    layer_ids = {
        "top": sidecar.top_row_stroke_layers,
        "bottom": sidecar.bottom_row_stroke_layers,
        "left": sidecar.left_column_stroke_layers,
        "right": sidecar.right_column_stroke_layers,
    }[side]
    for layer_id in layer_ids:
        layer = model.objects[layer_id.identifier]
        if layer.row_column_index == index:
            return [
                (run.origin, run.length, round(run.stroke.color.r * 255))
                for run in layer.stroke_runs
            ]
    return []


def test_add_stroke_splits_overlap():
    black = Border(2.0, RGB(0, 0, 0), "solid")
    red = Border(2.0, RGB(255, 0, 0), "solid")

    # Changing the middle cell of a three cell border splits the original run
    # in two rather than leaving a stale run underneath the new one
    table = Document().sheets[0].tables[0]
    table._model.add_stroke(table._table_id, 0, 0, "top", black, 3)
    table._model.add_stroke(table._table_id, 0, 1, "top", red, 1)
    assert stroke_runs(table, "top", 0) == [(0, 1, 0), (1, 1, 255), (2, 1, 0)]

    # The same split with a wider original run and a multi-cell replacement
    table = Document().sheets[0].tables[0]
    table._model.add_stroke(table._table_id, 0, 0, "top", black, 10)
    table._model.add_stroke(table._table_id, 0, 3, "top", red, 3)
    assert stroke_runs(table, "top", 0) == [(0, 3, 0), (3, 3, 255), (6, 4, 0)]

    # A run entirely covered by the new one is replaced, not duplicated
    table = Document().sheets[0].tables[0]
    table._model.add_stroke(table._table_id, 0, 2, "top", black, 2)
    table._model.add_stroke(table._table_id, 0, 0, "top", red, 6)
    assert stroke_runs(table, "top", 0) == [(0, 6, 255)]


def test_border_survives_write(configurable_save_file):
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    table.set_cell_border(2, 0, "top", border, 1)
    # Writing a value replaces the cell object; the border must come with it
    table.write(2, 0, "some text")
    doc.save(configurable_save_file)

    table = Document(configurable_save_file).sheets[0].tables[0]
    assert table.cell(2, 0).value == "some text"
    assert table.cell(2, 0).border.top == border


def test_border_survives_row_growth(configurable_save_file):
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows > 3:
        table.delete_row()

    # Row 2 is the last row, so the border cannot be mirrored onto row 3
    table.set_cell_border(2, 0, "bottom", border, 1)
    table.add_row()
    table.add_row()
    doc.save(configurable_save_file)

    table = Document(configurable_save_file).sheets[0].tables[0]
    assert table.cell(2, 0).border.bottom == border
    assert table.cell(3, 0).border.top == border


def test_border_survives_column_growth(configurable_save_file):
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_cols > 3:
        table.delete_column()

    # Column 2 is the last column, so the border cannot be mirrored onto column 3
    table.set_cell_border(0, 2, "right", border, 1)
    table.add_column()
    table.add_column()
    doc.save(configurable_save_file)

    table = Document(configurable_save_file).sheets[0].tables[0]
    assert table.cell(0, 2).border.right == border
    assert table.cell(0, 3).border.left == border


def test_border_follows_row_on_insert(configurable_save_file):
    """Confirmed directly: add_row() correctly moves a row's own cell
    values/styles to their new row index, but previously left that
    row's own border behind at its OLD physical row index -- the
    stroke sidecar is a separate structure, keyed by its own
    row_column_index independent of any Cell object, that add_row()
    never touched. Only manifests after a genuine save/reopen -- an
    in-memory check immediately after add_row() can look correct before
    this actually surfaces, so this test goes through a real save and
    reopen, twice, matching how the bug was actually found."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 5:
        table.add_row()
    table.set_cell_border(3, 0, "top", border, table.num_cols)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.add_row(1, start_row=1)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    # Row 3's own content shifted to row 4 -- its border should too.
    assert table3.cell(4, 0).border.top == border
    assert table3.cell(3, 0).border.top is None


def test_border_follows_column_on_insert(configurable_save_file):
    """The column-axis mirror of test_border_follows_row_on_insert --
    see that test's own docstring for the full reasoning."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    table.set_cell_border(0, 3, "left", border, table.num_rows)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.add_column(1, start_col=1)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    # Column 3's own content shifted to column 4 -- its border should too.
    assert table3.cell(0, 4).border.left == border
    assert table3.cell(0, 3).border.left is None


def test_border_follows_row_on_delete(configurable_save_file):
    """delete_row() has the identical gap add_row() had, in the
    opposite direction: it correctly moves the remaining rows' own
    content up to close the gap, but previously left a border behind at
    its OLD row index rather than following its content up to the new
    one."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 8:
        table.add_row()
    table.set_cell_border(5, 0, "top", border, table.num_cols)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.delete_row(1, start_row=1)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    # Row 5's own content shifted up to row 4 -- its border should too.
    assert table3.cell(4, 0).border.top == border
    assert table3.cell(5, 0).border.top is None


def test_border_follows_column_on_delete(configurable_save_file):
    """The column-axis mirror of test_border_follows_row_on_delete."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_cols < 8:
        table.add_column()
    table.set_cell_border(0, 5, "left", border, table.num_rows)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.delete_column(1, start_col=1)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    # Column 5's own content shifted left to column 4 -- its border should too.
    assert table3.cell(0, 4).border.left == border
    assert table3.cell(0, 5).border.left is None


def test_border_removed_when_its_own_row_is_deleted(configurable_save_file):
    """Deleting the row a border is actually ON (not just a row before
    it) should remove that border entirely, rather than leaving it
    dangling on whatever content now occupies that physical index."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 8:
        table.add_row()
    table.set_cell_border(5, 0, "top", border, table.num_cols)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.delete_row(1, start_row=5)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    assert all(table3.cell(r, 0).border.top is None for r in range(table3.num_rows))


def test_multirow_vertical_border_grows_on_row_insert_within_span(configurable_save_file):
    """A vertical (left/right) border's own row-range lives in a
    DIFFERENT place than a horizontal border's row index does: the
    enclosing stroke layer's own row_column_index is the COLUMN (a
    vertical border is "on" a column), and the row range itself is the
    layer's own stroke_run origin/length. Confirmed directly this was
    a genuinely separate gap from the row_column_index one already
    fixed above: shift_stroke_rows originally only adjusted
    row_column_index on the row-indexed (top/bottom) layers, never
    touching left/right layers' own stroke_run origin/length at all --
    so a multi-row vertical border kept its exact original physical
    span even after a row was inserted within it, silently misaligning
    with the content that had actually shifted."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 10:
        table.add_row()
    table.set_cell_border(3, 2, "left", border, 3)  # rows 3-5
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.add_row(1, start_row=4)  # within the border's own row range
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    bordered = [r for r in range(table3.num_rows) if table3.cell(r, 2).border.left is not None]
    assert bordered == [3, 4, 5, 6]


def test_multirow_vertical_border_shifts_on_row_insert_before_span(configurable_save_file):
    """The same border as above, but the insertion falls entirely
    before its own row range -- the whole run should shift uniformly
    (origin += n), not grow."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 10:
        table.add_row()
    table.set_cell_border(3, 2, "left", border, 3)  # rows 3-5
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.add_row(1, start_row=0)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    bordered = [r for r in range(table3.num_rows) if table3.cell(r, 2).border.left is not None]
    assert bordered == [4, 5, 6]


def test_multirow_vertical_border_shrinks_on_row_delete_within_span(configurable_save_file):
    """Deleting a row from WITHIN a multi-row vertical border's own
    span should shrink it by the deleted amount, not leave it at its
    original length covering the wrong physical rows."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 12:
        table.add_row()
    table.set_cell_border(5, 2, "left", border, 3)  # rows 5-7
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.delete_row(1, start_row=6)  # the middle row of the span
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    bordered = [r for r in range(table3.num_rows) if table3.cell(r, 2).border.left is not None]
    assert bordered == [5, 6]


def test_multirow_vertical_border_removed_when_its_entire_span_is_deleted(configurable_save_file):
    """Deleting every row a vertical border's own span covers should
    remove it entirely, the same way test_border_removed_when_its_own_
    row_is_deleted already confirms for a horizontal border on a
    single row."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 12:
        table.add_row()
    table.set_cell_border(5, 2, "left", border, 3)  # rows 5-7
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.delete_row(3, start_row=5)  # the entire span
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    assert all(table3.cell(r, 2).border.left is None for r in range(table3.num_rows))


def test_multicolumn_horizontal_border_grows_on_column_insert_within_span(configurable_save_file):
    """The column-axis mirror of
    test_multirow_vertical_border_grows_on_row_insert_within_span --
    see that test's own docstring for the full reasoning."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_cols < 10:
        table.add_column()
    table.set_cell_border(2, 3, "top", border, 3)  # columns 3-5
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    table2.add_column(1, start_col=4)  # within the border's own column range
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    table3 = doc3.sheets[0].tables[0]
    bordered = [c for c in range(table3.num_cols) if table3.cell(2, c).border.top is not None]
    assert bordered == [3, 4, 5, 6]


def test_multirow_vertical_border_grows_on_row_insert_within_span_single_session(
    configurable_save_file,
):
    """The single-session mirror of
    test_multirow_vertical_border_grows_on_row_insert_within_span --
    no save/reopen between setting the border and inserting into its
    span. Previously failed silently: shift_stroke_rows() only touches
    the stroke sidecar, which is empty until the next save, so nothing
    propagated the border onto the newly-inserted row until this fix's
    propagate_borders_into_inserted_rows() started comparing the
    span's own boundary cells directly."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 10:
        table.add_row()
    table.set_cell_border(3, 2, "left", border, 3)  # rows 3-5
    table.add_row(1, start_row=4)  # within the border's own row range, same session

    bordered = [r for r in range(table.num_rows) if table.cell(r, 2).border.left is not None]
    assert bordered == [3, 4, 5, 6]

    # Confirm it also survives a save/reopen once materialised this way.
    doc.save(configurable_save_file)
    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    bordered2 = [r for r in range(table2.num_rows) if table2.cell(r, 2).border.left is not None]
    assert bordered2 == [3, 4, 5, 6]


def test_multicolumn_horizontal_border_grows_on_column_insert_within_span_single_session(
    configurable_save_file,
):
    """The column-axis mirror of
    test_multirow_vertical_border_grows_on_row_insert_within_span_single_session."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_cols < 10:
        table.add_column()
    table.set_cell_border(2, 3, "top", border, 3)  # columns 3-5
    table.add_column(1, start_col=4)  # within the border's own column range, same session

    bordered = [c for c in range(table.num_cols) if table.cell(2, c).border.top is not None]
    assert bordered == [3, 4, 5, 6]

    doc.save(configurable_save_file)
    doc2 = Document(configurable_save_file)
    table2 = doc2.sheets[0].tables[0]
    bordered2 = [c for c in range(table2.num_cols) if table2.cell(2, c).border.top is not None]
    assert bordered2 == [3, 4, 5, 6]


def test_border_shift_before_span_still_correct_single_session(configurable_save_file):
    """Guards against a regression in the opposite direction: an
    insertion entirely BEFORE a border's own span, in a single
    session, should still just shift the whole span uniformly -- not
    accidentally grow it or duplicate it -- now that
    propagate_borders_into_inserted_rows() runs on every add_row()
    call, not just ones that land inside an existing span."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 10:
        table.add_row()
    table.set_cell_border(4, 2, "left", border, 2)  # rows 4-5
    table.add_row(1, start_row=1)  # before the span, same session

    bordered = [r for r in range(table.num_rows) if table.cell(r, 2).border.left is not None]
    assert bordered == [5, 6]


def test_border_delete_within_span_still_correct_single_session(configurable_save_file):
    """Guards against a regression from this fix in the delete
    direction: deleting a row from within an existing span, in a
    single session, should shrink the span rather than leaving a
    duplicated or dangling entry behind."""
    border = Border(1.0, RGB(0, 0, 0), "solid")

    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows < 10:
        table.add_row()
    table.set_cell_border(2, 2, "left", border, 3)  # rows 2-4
    table.delete_row(1, start_row=3)  # middle row of the span, same session

    bordered = [r for r in range(table.num_rows) if table.cell(r, 2).border.left is not None]
    assert bordered == [2, 3]
