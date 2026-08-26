from numbers_parser import Document, MergedCell

XXX_TABLE_1_REF = [
    ["XXX_COL_1", "XXX_COL_2", "XXX_COL_3", "XXX_COL_4", "XXX_COL_5"],
    ["XXX_1_1__1_2", None, "XXX_1_3", "XXX_1_4", "XXX_1_5"],
    ["XXX_2_1", "XXX_2_2", "XXX_2_3", "XXX_2_4", "XXX_2_5"],
    ["XXX_3_1", "XXX_3_2", "XXX_3_3__3_5", None, None],
    ["XXX_4_1", "XXX_4_2__4_5", None, None, None],
    ["XXX_5_1", "XXX_5_2__XXX_7_2", "XXX_5_3", "XXX_5_4", "XXX_5_5"],
    ["XXX_6_1", None, "XXX_6_3", "XXX_6_4__XXX_7_5", None],
    ["XXX_7_1", None, "XXX_7_3", None, None],
]


XXX_TABLE_1_CLASSES = [
    ["TextCell", "TextCell", "TextCell", "TextCell", "TextCell"],
    ["TextCell", "MergedCell", "TextCell", "TextCell", "TextCell"],
    ["TextCell", "TextCell", "TextCell", "TextCell", "TextCell"],
    ["TextCell", "TextCell", "TextCell", "MergedCell", "MergedCell"],
    ["TextCell", "TextCell", "MergedCell", "MergedCell", "MergedCell"],
    ["TextCell", "TextCell", "TextCell", "TextCell", "TextCell"],
    ["TextCell", "MergedCell", "TextCell", "TextCell", "MergedCell"],
    ["TextCell", "MergedCell", "TextCell", "MergedCell", "MergedCell"],
]


def test_table_contents():
    doc = Document("tests/data/test-9.numbers")
    sheets = doc.sheets
    tables = sheets[0].tables
    data = tables[0].rows(values_only=True)
    assert data == XXX_TABLE_1_REF


def test_cell_classes():
    doc = Document("tests/data/test-9.numbers")
    sheets = doc.sheets
    tables = sheets[0].tables
    data = []
    for row in tables[0].iter_rows():
        data.append([type(c).__name__ for c in row])
    assert data == XXX_TABLE_1_CLASSES


def test_merge_references():
    doc = Document("tests/data/test-9.numbers")
    sheets = doc.sheets
    table = sheets[0].tables[0]
    assert table.cell("B2").merge_range == "A2:B2"
    assert table.cell("B2").rect == (1, 0, 1, 1)
    assert table.cell("B2").size is None
    assert table.cell("C5").merge_range == "B5:E5"
    assert table.cell("C5").rect == (4, 1, 4, 4)
    assert table.cell("C5").size is None
    assert table.cell("C5").row_start == 4
    assert table.cell("C5").row_end == 4
    assert table.cell("C5").col_start == 1
    assert table.cell("C5").col_end == 4
    assert table.cell("A2").is_merged
    assert table.cell("A2").rect is None
    assert not table.cell("C1").is_merged
    assert table.cell("C1").size == (1, 1)
    assert table.cell("C1").merge_range is None
    assert table.cell("D7").size == (2, 2)

    table = sheets[1].tables[0]
    assert table.cell("A1").is_merged
    assert table.cell("A1").size == (1, 2)
    assert table.cell("B4").is_merged
    assert table.cell("B4").size == (2, 2)


def test_all_merged_ranges():
    doc = Document("tests/data/test-9.numbers")
    sheets = doc.sheets
    table = sheets[0].tables[0]
    assert table.merge_ranges == ["A2:B2", "B5:E5", "B6:B8", "C4:E4", "D7:E8"]
    table = sheets[1].tables[0]
    assert table.merge_ranges == ["A1:B1", "B4:C5"]


def test_wide_merge_on_a_table_with_fewer_rows_than_merged_columns(configurable_save_file):
    # recalculate_row_headers() previously computed a row's own cell count
    # as len(data) (the table's ROW count) minus that row's merged-cell
    # count, instead of len(cells) (that row's own COLUMN count) minus it
    # -- a copy/paste mistake against the correct pattern already used by
    # recalculate_column_headers(). A hard ValueError ("Value out of
    # range") from the protobuf's `required uint32 numberOfCells` field
    # once a row's merged-cell count exceeds the table's own row count,
    # since the (buggy) subtraction goes negative.
    #
    # The crash only appears on a SECOND save: table.merge_cells() only
    # marks a range for merging -- the affected cells don't actually
    # become MergedCell instances in memory until the document is
    # reopened (merge geometry is applied on the read path, in
    # Cell._set_merge()). So the first save writes a silently-wrong (but
    # non-negative) numberOfCells; only a save of an already-reopened
    # document, where the merged cells are genuinely MergedCell, can
    # drive the subtraction negative. Confirmed as the same shape of bug
    # that broke tests/data/issue-102-v14.4.numbers/
    # issue-102-v15.1.numbers on save (both real files being resaved,
    # i.e. already past their own first save).
    doc = Document()
    table = doc.sheets[0].tables[0]
    while table.num_rows > 2:
        table.delete_row()
    assert table.num_rows == 2
    assert table.num_cols == 8

    table.merge_cells("A1:H1")
    doc.save(configurable_save_file)

    reopened = Document(configurable_save_file)
    reopened_table = reopened.sheets[0].tables[0]
    assert reopened_table.merge_ranges == ["A1:H1"]
    for col in range(1, 8):
        assert isinstance(reopened_table.cell(0, col), MergedCell)

    reopened.save(configurable_save_file)

    resaved = Document(configurable_save_file)
    resaved_table = resaved.sheets[0].tables[0]
    assert resaved_table.merge_ranges == ["A1:H1"]
