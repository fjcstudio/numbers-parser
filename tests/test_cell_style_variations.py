from numbers_parser import RGB, BackgroundImage, Document
from numbers_parser.model import DOCUMENT_ID

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0005fe02fea7d6cfd00000000049454e44ae426082",
)


def _style_object(doc, cell):
    model = doc._model
    return model.objects[model.cell_style_object_id(cell)]


def _count_cell_styles(doc):
    return sum(
        1 for o in doc._model.objects._objects.values() if type(o).__name__ == "CellStyleArchive"
    )


def _saved(doc, path, row=2, col=2):
    doc.save(path)
    saved = Document(path)
    return saved, saved.sheets[0].tables[0].cell(row, col)


def test_fill_edit_is_a_variation_holding_only_the_fill(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.bg_color = RGB(255, 0, 0)
    saved, cell = _saved(doc, configurable_save_file)
    style = _style_object(saved, cell)
    body = saved._model.default_cell_style(cell)

    assert style.super.is_variation
    assert style.super.parent.identifier == saved._model._default_cell_style_id(cell)
    assert not style.super.HasField("name")
    assert [f.name for f, _ in style.cell_properties.ListFields()] == ["cell_fill"]
    assert style.override_count == 1
    assert cell.style.bg_color == RGB(255, 0, 0)
    assert cell.style.text_inset == saved._model.cell_property(body, "padding").left
    assert cell.style.text_wrap == saved._model.cell_property(body, "text_wrap")


def test_variation_is_listed_under_its_parent(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.text_inset = 9.0
    saved, cell = _saved(doc, configurable_save_file)
    model = saved._model
    style_id = model.cell_style_object_id(cell)
    stylesheet = model.objects[model.objects[DOCUMENT_ID].stylesheet.identifier]
    assert style_id in {r.identifier for r in stylesheet.styles}
    parent_id = model.objects[style_id].super.parent.identifier
    children = next(
        e for e in stylesheet.parent_to_children_style_map if e.parent.identifier == parent_id
    )
    assert style_id in {c.identifier for c in children.children}
    assert cell.style.text_inset == 9.0


def test_edited_cell_keeps_following_the_table_style(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.bg_color = RGB(0, 128, 0)
    saved, cell = _saved(doc, configurable_save_file)
    body = saved._model.default_cell_style(cell)
    body.cell_properties.text_wrap = not body.cell_properties.text_wrap
    assert cell.style.text_wrap == saved._model.cell_property(body, "text_wrap")
    assert cell.style.bg_color == RGB(0, 128, 0)


def test_header_cell_edit_is_a_variation_of_the_header_style(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(0, 1, "h")
    table.cell(0, 1).style.text_wrap = not table.cell(0, 1).style.text_wrap
    saved, cell = _saved(doc, configurable_save_file, row=0, col=1)
    style = _style_object(saved, cell)
    table_model = saved._model.objects[cell._table_id]
    assert style.super.is_variation
    assert style.super.parent.identifier == table_model.header_row_style.identifier


def test_identical_edits_share_one_variation(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    for col in (2, 3, 4):
        table.write(2, col, "x")
        table.cell(2, col).style.bg_color = RGB(10, 20, 30)
    before = _count_cell_styles(doc)
    doc.save(configurable_save_file)
    saved = Document(configurable_save_file)
    ids = {
        saved._model.cell_style_object_id(saved.sheets[0].tables[0].cell(2, c)) for c in (2, 3, 4)
    }
    assert len(ids) == 1
    assert _count_cell_styles(saved) - before <= 1


def test_edit_matching_the_table_style_writes_no_cell_style(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.text_wrap = table.cell(2, 2).style.text_wrap
    saved, cell = _saved(doc, configurable_save_file)
    assert saved._model.cell_style_object_id(cell) is None


def test_removing_a_fill_the_table_style_gives_is_a_variation_with_an_empty_fill(
    configurable_save_file,
):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(0, 1, "h")
    assert table.cell(0, 1).style.bg_color is not None
    table.cell(0, 1).style.bg_color = None
    saved, cell = _saved(doc, configurable_save_file, row=0, col=1)
    style = _style_object(saved, cell)
    assert style.super.is_variation
    assert style.cell_properties.HasField("cell_fill")
    assert not style.cell_properties.cell_fill.ListFields()
    assert cell.style.bg_color is None


def test_empty_fill_is_not_written_when_the_table_style_has_none(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    assert table.cell(2, 2).style.bg_color is None
    table.cell(2, 2).style.bg_color = None
    table.cell(2, 2).style.text_inset = 7.0
    saved, cell = _saved(doc, configurable_save_file)
    style = _style_object(saved, cell)
    assert not style.cell_properties.HasField("cell_fill")


def test_gradient_fill_edit_is_a_variation(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.bg_color = [RGB(255, 0, 0), RGB(0, 0, 255)]
    saved, cell = _saved(doc, configurable_save_file)
    style = _style_object(saved, cell)
    assert style.super.is_variation
    assert len(style.cell_properties.cell_fill.gradient.stops) == 2
    assert cell.style.bg_color == [RGB(255, 0, 0), RGB(0, 0, 255)]


def test_editing_another_property_of_a_gradient_cell_keeps_the_gradient(configurable_save_file):
    doc = Document("tests/data/issue-7.numbers")
    table = doc.sheets[0].tables[0]
    before = table.cell(0, 0).style.bg_color
    assert isinstance(before, list)
    stored = doc._model.objects[doc._model.cell_style_object_id(table.cell(0, 0))]
    original = stored.cell_properties.cell_fill.SerializeToString()
    table.cell(0, 0).style.text_inset = 11.0
    doc.save(configurable_save_file)
    saved = Document(configurable_save_file)
    cell = saved.sheets[0].tables[0].cell(0, 0)
    assert cell.style.bg_color == before
    assert cell.style.text_inset == 11.0
    fill = saved._model.objects[saved._model.cell_style_object_id(cell)].cell_properties.cell_fill
    assert fill.SerializeToString() == original


def test_registered_gradient_style_saves(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    style = doc.add_style(name="Gradient", bg_color=[RGB(1, 2, 3), RGB(4, 5, 6)])
    table.write(2, 2, "x")
    table.set_cell_style(2, 2, style)
    saved, cell = _saved(doc, configurable_save_file)
    assert cell.style.bg_color == [RGB(1, 2, 3), RGB(4, 5, 6)]


def test_registered_style_is_still_a_named_full_style(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    style = doc.add_style(name="Named fill", bg_color=RGB(1, 2, 3))
    table.write(2, 2, "x")
    table.set_cell_style(2, 2, style)
    saved, cell = _saved(doc, configurable_save_file)
    cell_style = _style_object(saved, cell)
    assert not cell_style.super.is_variation
    assert cell_style.super.name == "Named fill"
    assert cell.style.bg_color == RGB(1, 2, 3)


def test_image_fill_edit_is_a_variation(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.bg_image = BackgroundImage(PNG, "dot.png")
    saved, cell = _saved(doc, configurable_save_file)
    style = _style_object(saved, cell)
    assert style.super.is_variation
    assert style.cell_properties.cell_fill.HasField("image")


def test_editing_an_edited_cell_again_keeps_earlier_edits(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 2, "x")
    table.cell(2, 2).style.bg_color = RGB(255, 0, 0)
    saved, _ = _saved(doc, configurable_save_file)
    saved.sheets[0].tables[0].cell(2, 2).style.text_inset = 12.0
    saved.save(configurable_save_file)
    again = Document(configurable_save_file).sheets[0].tables[0].cell(2, 2)
    assert again.style.bg_color == RGB(255, 0, 0)
    assert again.style.text_inset == 12.0


def test_dangling_style_id_reads_as_default():
    """Cells referencing a style key missing from the datalist read with defaults."""
    doc = Document("tests/data/custom-format-stress.numbers")
    table = doc.sheets[0].tables[0]
    cell = table.cell(2, 0)
    assert cell._cell_style_id is None
    assert cell.style is not None
    assert cell.value == table.cell(2, 0).value
