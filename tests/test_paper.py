import pytest

from numbers_parser import Document


def test_default_paper_and_orientation():
    doc = Document()
    assert doc.paper_size == "a4"
    assert doc.page_dimensions == (595.0, 842.0)
    assert doc.sheets[0].orientation == "portrait"


def test_paper_size_and_orientation_round_trip(configurable_save_file):
    doc = Document()
    doc.paper_size = "a3"
    doc.sheets[0].orientation = "landscape"
    doc.save(configurable_save_file)

    saved = Document(configurable_save_file)
    assert saved.paper_size == "a3"
    assert saved.page_dimensions == (842.0, 1191.0)
    assert saved.sheets[0].orientation == "landscape"


def test_custom_paper_size(configurable_save_file):
    doc = Document()
    doc.set_paper_size("us-letter", 612, 792)
    assert doc.paper_size == "us-letter"
    doc.save(configurable_save_file)
    saved = Document(configurable_save_file)
    assert saved.paper_size == "us-letter"
    assert saved.page_dimensions == (612.0, 792.0)


def test_invalid_values_raise():
    doc = Document()
    with pytest.raises(ValueError, match="unknown paper size"):
        doc.paper_size = "a0"
    with pytest.raises(ValueError, match="orientation"):
        doc.sheets[0].orientation = "diagonal"
