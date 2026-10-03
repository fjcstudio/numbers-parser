from numbers_parser import RGB, Document


def _build(path, **kwargs):
    doc = Document()
    table = doc.sheets[0].tables[0]
    style = doc.add_style(name="Spaced", **kwargs)
    table.write(0, 0, "x")
    table.set_cell_style(0, 0, style)
    doc.save(path)
    return Document(path).sheets[0].tables[0].cell(0, 0).style


def test_baseline_shift_and_line_spacing_round_trip(configurable_save_file):
    style = _build(configurable_save_file, baseline_shift=-1.0, line_spacing=0.9)
    assert style.baseline_shift == -1.0
    assert round(style.line_spacing, 3) == 0.9


def test_unset_spacing_is_not_forced(configurable_save_file):
    style = _build(configurable_save_file)
    assert style.baseline_shift == 0.0
    assert style.line_spacing is None
    # The saved style must leave both fields unset rather than write defaults.
    doc = Document(configurable_save_file)
    stored = doc._model.cell_text_style(doc.sheets[0].tables[0].cell(0, 0))
    assert not stored.char_properties.HasField("baseline_shift")
    assert not stored.para_properties.HasField("line_spacing")


def test_existing_style_can_be_changed(configurable_save_file):
    _build(configurable_save_file, baseline_shift=-1.0, line_spacing=0.9)
    doc = Document(configurable_save_file)
    table = doc.sheets[0].tables[0]
    table.cell(0, 0).style.line_spacing = 1.2
    doc.save(configurable_save_file)
    again = Document(configurable_save_file).sheets[0].tables[0].cell(0, 0).style
    assert round(again.line_spacing, 3) == 1.2
    assert again.baseline_shift == -1.0


def test_unset_line_spacing_round_trips_as_none(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    explicit = doc.add_style(
        name="Explicit Spacing",
        font_size=12.0,
        line_spacing=1.5,
        baseline_shift=2.0,
        font_color=RGB(0, 0, 0),
    )
    default = doc.add_style(name="Default Spacing", font_size=12.0, font_color=RGB(0, 0, 0))
    table.write(0, 0, "explicit", style=explicit)
    table.write(0, 1, "default", style=default)
    doc.save(configurable_save_file)

    saved = Document(configurable_save_file).sheets[0].tables[0]
    assert saved.cell(0, 0).style.line_spacing == 1.5
    assert saved.cell(0, 0).style.baseline_shift == 2.0
    assert saved.cell(0, 1).style.line_spacing is None
    assert saved.cell(0, 1).style.baseline_shift == 0.0
