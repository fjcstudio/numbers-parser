import zipfile

import pytest

from numbers_parser import Document
from numbers_parser.generated import TSAArchives_pb2 as TSAArchives
from numbers_parser.generated import TSDArchives_pb2 as TSDArchives
from numbers_parser.generated import TSWPArchives_pb2 as TSWPArchives


def test_add_image_persists(configurable_save_file):
    """
    A free-standing image, added to a sheet, should be registered
    both as a drawable object (with the right position/size) and as an
    actual file in the document's own package -- confirmed via a
    genuine save/reopen, not just an in-memory check.
    """
    image_data = open("tests/data/cat.jpg", mode="rb").read()

    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_image(image_data, "cat.jpg", x=50.0, y=100.0, width=80.0, height=40.0)
    doc.save(configurable_save_file)

    with zipfile.ZipFile(configurable_save_file) as z:
        assert z.testzip() is None
        assert "Data/cat.jpg" in z.namelist()
        assert z.read("Data/cat.jpg") == image_data

    reopened = Document(configurable_save_file)
    sheet2 = reopened.sheets[0]
    m = reopened._model
    sheet_obj = m.objects[sheet2._sheet_id]
    images = [
        m.objects[ref.identifier]
        for ref in sheet_obj.drawable_infos
        if type(m.objects[ref.identifier]).__name__ == "ImageArchive"
    ]
    assert len(images) == 1
    assert images[0].super.geometry.position.x == 50.0
    assert images[0].super.geometry.position.y == 100.0
    assert images[0].super.geometry.size.width == 80.0
    assert images[0].super.geometry.size.height == 40.0


def test_add_image_deduplicates_identical_content(configurable_save_file):
    """
    The same image content, added twice under different filenames,
    should be stored once (content-hash deduplication, the same
    mechanism add_cell_style()'s own bg_image support already uses),
    not duplicated in the package.
    """
    image_data = open("tests/data/cat.jpg", mode="rb").read()

    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_image(image_data, "cat_a.jpg", x=0.0, y=0.0, width=10.0, height=10.0)
    sheet.add_image(image_data, "cat_b.jpg", x=100.0, y=0.0, width=10.0, height=10.0)
    doc.save(configurable_save_file)

    with zipfile.ZipFile(configurable_save_file) as z:
        data_files = [n for n in z.namelist() if n.startswith("Data/") and n.endswith(".jpg")]
        assert len(data_files) == 1

    reopened = Document(configurable_save_file)
    m = reopened._model
    sheet_obj = m.objects[reopened.sheets[0]._sheet_id]
    images = [
        m.objects[ref.identifier]
        for ref in sheet_obj.drawable_infos
        if type(m.objects[ref.identifier]).__name__ == "ImageArchive"
    ]
    assert len(images) == 2
    assert images[0].data.identifier == images[1].data.identifier


def test_images_list_and_read_back(configurable_save_file):
    image_data = open("tests/data/cat.jpg", mode="rb").read()
    doc = Document()
    sheet = doc.sheets[0]
    assert sheet.images == []
    image = sheet.add_image(image_data, "cat.jpg", x=5.0, y=6.0, width=70.0, height=30.0)
    assert (image.x, image.y, image.width, image.height) == (5.0, 6.0, 70.0, 30.0)
    assert image.data == image_data
    assert image.filename == "cat.jpg"
    doc.save(configurable_save_file)

    images = Document(configurable_save_file).sheets[0].images
    assert len(images) == 1
    assert (images[0].x, images[0].y, images[0].width, images[0].height) == (5.0, 6.0, 70.0, 30.0)
    assert images[0].data == image_data
    assert images[0].filename == "cat.jpg"


def test_image_geometry_can_be_changed(configurable_save_file):
    image_data = open("tests/data/cat.jpg", mode="rb").read()
    doc = Document()
    sheet = doc.sheets[0]
    image = sheet.add_image(image_data, "cat.jpg", x=0.0, y=0.0, width=10.0, height=10.0)
    image.x = 11.5
    image.y = 22.5
    image.width = 33.0
    image.height = 44.0
    doc.save(configurable_save_file)

    moved = Document(configurable_save_file).sheets[0].images[0]
    assert (moved.x, moved.y, moved.width, moved.height) == (11.5, 22.5, 33.0, 44.0)


def test_remove_image_keeps_the_shared_file(configurable_save_file):
    image_data = open("tests/data/cat.jpg", mode="rb").read()
    doc = Document()
    sheet = doc.sheets[0]
    first = sheet.add_image(image_data, "cat_a.jpg", x=0.0, y=0.0, width=10.0, height=10.0)
    second = sheet.add_image(image_data, "cat_b.jpg", x=50.0, y=0.0, width=10.0, height=10.0)
    sheet.remove_image(first)
    assert len(sheet.images) == 1
    doc.save(configurable_save_file)

    remaining = Document(configurable_save_file).sheets[0].images
    assert len(remaining) == 1
    assert remaining[0].x == 50.0
    assert remaining[0].data == image_data
    assert second is not None


def test_remove_missing_image_raises():
    image_data = open("tests/data/cat.jpg", mode="rb").read()
    doc = Document()
    sheet = doc.sheets[0]
    image = sheet.add_image(image_data, "cat.jpg")
    sheet.remove_image(image)
    with pytest.raises(IndexError, match="no image with id"):
        sheet.remove_image(image)


def test_duplicate_image(configurable_save_file):
    image_data = open("tests/data/cat.jpg", mode="rb").read()
    doc = Document()
    sheet = doc.sheets[0]
    original = sheet.add_image(image_data, "cat.jpg", x=1.0, y=2.0, width=30.0, height=20.0)
    copy = sheet.duplicate_image(original, y=500.0)
    assert (copy.x, copy.y, copy.width, copy.height) == (1.0, 500.0, 30.0, 20.0)
    assert [i.y for i in sheet.images] == [500.0, 2.0]
    doc.save(configurable_save_file)

    images = Document(configurable_save_file).sheets[0].images
    assert [i.y for i in images] == [500.0, 2.0]
    assert images[0].data == image_data
    assert images[0].filename == images[1].filename == "cat.jpg"


def test_failed_add_image_leaves_no_record():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_image(b"first", "x.png")
    with pytest.raises(IndexError, match="already exists"):
        sheet.add_image(b"second", "x.png")
    image = sheet.add_image(b"second", "y.png")
    assert image.data == b"second"
    assert image.filename == "y.png"
    assert len(sheet.images) == 2


def test_duplicate_image_owns_its_caption_title_and_mask(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    original = sheet.add_image(b"data", "x.png")
    model = doc._model
    source = model.objects[original._image_id]

    def owned(field):
        storage_id, _ = model.objects.create_object_from_dict(
            "Document",
            {},
            TSWPArchives.StorageArchive,
        )
        info_id, info = model.objects.create_object_from_dict(
            "Document",
            {},
            TSAArchives.CaptionInfoArchive,
        )
        info.super.owned_storage.identifier = storage_id
        info.super.super.super.parent.identifier = original._image_id
        getattr(source.super, field).identifier = info_id

    owned("title")
    owned("caption")
    mask_id, mask = model.objects.create_object_from_dict("Document", {}, TSDArchives.MaskArchive)
    mask.super.parent.identifier = original._image_id
    source.mask.identifier = mask_id

    copy = sheet.duplicate_image(original, y=100.0)
    copied = model.objects[copy._image_id]
    for field in ("title", "caption"):
        old_info = model.objects[getattr(source.super, field).identifier]
        new_info = model.objects[getattr(copied.super, field).identifier]
        assert getattr(copied.super, field).identifier != getattr(source.super, field).identifier
        assert new_info.super.owned_storage.identifier != old_info.super.owned_storage.identifier
        assert new_info.super.super.super.parent.identifier == copy._image_id
    assert copied.mask.identifier != mask_id
    assert model.objects[copied.mask.identifier].super.parent.identifier == copy._image_id
    doc.save(configurable_save_file)
    assert len(Document(configurable_save_file).sheets[0].images) == 2


def test_duplicate_image_from_another_sheet_raises():
    doc = Document()
    doc.add_sheet("Other")
    image = doc.sheets[0].add_image(b"data", "x.png")
    objects_before = len(doc._model.objects)
    with pytest.raises(IndexError, match="no image with id"):
        doc.sheets[1].duplicate_image(image)
    assert len(doc._model.objects) == objects_before
