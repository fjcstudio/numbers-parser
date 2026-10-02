import pytest

from numbers_parser import Document


def test_sheet_with_no_guides_returns_empty_list():
    doc = Document()
    assert doc.sheets[0].ruler_guides == []


def test_add_ruler_guide_creates_storage_and_persists(configurable_save_file):
    """A sheet with no guides at all has no userDefinedGuideStorage
    reference to begin with -- confirms add_ruler_guide() correctly
    creates one the first time, and that it survives a genuine
    save/reopen (not just an in-memory check)."""
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_ruler_guide("horizontal", 100.0)
    sheet.add_ruler_guide("vertical", 50.0)
    doc.save(configurable_save_file)

    reopened = Document(configurable_save_file)
    guides = reopened.sheets[0].ruler_guides
    assert ("horizontal", 100.0) in guides
    assert ("vertical", 50.0) in guides
    assert len(guides) == 2


def test_add_ruler_guide_appends_to_existing_storage(configurable_save_file):
    """A sheet that already has at least one guide (and therefore
    already has its own userDefinedGuideStorage) should have a new
    guide appended to it, not have the existing ones replaced or
    disturbed."""
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_ruler_guide("horizontal", 100.0)
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    sheet2 = doc2.sheets[0]
    sheet2.add_ruler_guide("vertical", 200.0)
    doc2.save(configurable_save_file)

    doc3 = Document(configurable_save_file)
    guides = doc3.sheets[0].ruler_guides
    assert ("horizontal", 100.0) in guides
    assert ("vertical", 200.0) in guides
    assert len(guides) == 2


def test_add_ruler_guide_rejects_invalid_axis():
    doc = Document()
    with pytest.raises(ValueError, match="axis must be"):
        doc.sheets[0].add_ruler_guide("diagonal", 100.0)


def test_clear_ruler_guides(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    sheet.clear_ruler_guides()
    sheet.add_ruler_guide("horizontal", 10.0)
    sheet.add_ruler_guide("vertical", 20.0)
    sheet.clear_ruler_guides()
    assert sheet.ruler_guides == []
    sheet.add_ruler_guide("horizontal", 99.0)
    doc.save(configurable_save_file)
    assert Document(configurable_save_file).sheets[0].ruler_guides == [("horizontal", 99.0)]
