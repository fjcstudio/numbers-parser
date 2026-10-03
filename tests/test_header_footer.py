import pytest

from numbers_parser import Document


def test_header_and_footer_text_round_trip(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    assert sheet.header_text() == ""
    sheet.set_header_text("Nominated architects: A and B")
    sheet.set_header_text("Centre", zone=1)
    sheet.set_footer_text("Level 6, Australia Square", zone=0)
    doc.save(configurable_save_file)

    sheet = Document(configurable_save_file).sheets[0]
    assert sheet.header_text() == "Nominated architects: A and B"
    assert sheet.header_text(1) == "Centre"
    assert sheet.footer_text() == "Level 6, Australia Square"


def test_shorter_text_drops_style_runs_past_the_end(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    sheet.set_header_text("x" * 100)
    storage = doc._model._header_footer_storage(sheet._sheet_id, "headers", 0)
    run = storage.table_char_style.entries.add()
    run.character_index = 60
    sheet.set_header_text("short")
    assert all(e.character_index < 5 for e in storage.table_char_style.entries)
    doc.save(configurable_save_file)
    assert Document(configurable_save_file).sheets[0].header_text() == "short"


def test_clearing_text(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    sheet.set_footer_text("something")
    sheet.set_footer_text("")
    doc.save(configurable_save_file)
    assert Document(configurable_save_file).sheets[0].footer_text() == ""


def test_missing_zone_raises():
    sheet = Document().sheets[0]
    with pytest.raises(IndexError, match="zone 9 does not exist"):
        sheet.set_header_text("x", zone=9)
    with pytest.raises(IndexError, match="zone 9 does not exist"):
        sheet.footer_text(9)


def test_char_style_runs_can_be_replaced(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    sheet.set_footer_text("abc def")
    storage = doc._model._header_footer_storage(sheet._sheet_id, "footers", 0)
    storage.table_char_style.entries.add().character_index = 0
    sheet.set_footer_text("abc def ghi", runs=[(0, None), (4, None), (8, None)])
    assert [i for i, _ in sheet.footer_char_runs()] == [0, 4, 8]
    doc.save(configurable_save_file)
    assert [i for i, _ in Document(configurable_save_file).sheets[0].footer_char_runs()] == [
        0,
        4,
        8,
    ]


def test_replacing_a_field_drops_its_attachment(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    # The default template's centre footer is a page number field: U+FFFC
    # anchored to an attachment at index 0.
    assert sheet.footer_text(1) == "\ufffc"
    storage = doc._model._header_footer_storage(sheet._sheet_id, "footers", 1)
    assert len(storage.table_attachment.entries) == 1
    sheet.set_footer_text("Draft", zone=1)
    assert len(storage.table_attachment.entries) == 0
    doc.save(configurable_save_file)
    assert Document(configurable_save_file).sheets[0].footer_text(1) == "Draft"


def test_field_kept_where_its_placeholder_remains():
    doc = Document()
    sheet = doc.sheets[0]
    storage = doc._model._header_footer_storage(sheet._sheet_id, "footers", 1)
    field = storage.table_attachment.entries[0].object.identifier
    sheet.set_footer_text("\ufffc of 3", zone=1)
    assert [(e.character_index, e.object.identifier) for e in storage.table_attachment.entries] == [
        (0, field),
    ]


def test_runs_on_a_zone_without_char_styles(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    assert sheet.footer_char_runs() == []
    sheet.set_footer_text("abc def", runs=[(0, None), (4, None)])
    assert sheet.footer_char_runs() == [(0, None), (4, None)]
    doc.save(configurable_save_file)
    assert Document(configurable_save_file).sheets[0].footer_char_runs() == [(0, None), (4, None)]


def test_invalid_runs_leave_the_zone_unchanged():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.set_footer_text("before")
    with pytest.raises(ValueError, match="run"):
        sheet.set_footer_text("after", runs=[(99, None)])
    assert sheet.footer_text() == "before"
