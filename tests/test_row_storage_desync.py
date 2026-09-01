"""
A real Numbers.app resave can leave a table's row storage sparser than
this library ever writes it -- dropping the stored buffer for an
entirely-blank row, and even an entirely-blank tile, as a space
optimisation -- without correspondingly updating ``rowHeaders.buckets``,
a separate bookkeeping structure ``model.py`` used to rely on to work
out "which flattened storage position is row N". See
``row_storage_desync_bug_report.md`` for the full, confirmed mechanism
and how it was found (a live round trip of a genuine 1000-row table).

These tests reproduce the desync directly against this library's own
archive objects -- no live Numbers.app round trip needed -- by writing
a table normally, reloading its saved (still dense) archive with a
fresh, empty-cache model, mutating its tile storage the same way a real
Numbers.app resave does (dropping blank rowInfos, and an entirely-blank
tile) while deliberately leaving ``rowHeaders.buckets`` untouched at its
original, now-stale, dense count, and only then constructing the
``Table`` that reads it -- so the read genuinely exercises the mutated
state rather than a cache already populated before the mutation, and
without a second ``Document.save()`` silently regenerating dense
storage out from under the test.
"""

import pytest

from numbers_parser import Document, UnsupportedWarning
from numbers_parser.constants import DEFAULT_TILE_SIZE
from numbers_parser.document import Table
from numbers_parser.generated import TSTArchives_pb2 as TSTArchives
from numbers_parser.model import _NumbersModel


def _sparsify_tile_in_place(model, table_id, tile_index, rows_to_keep):
    """
    Drop every rowInfo in the given tile except the ones for the given
    absolute rows, keeping each surviving rowInfo's own ``tile_row_index``
    unchanged -- exactly what was found still correct in a real
    Numbers.app-resaved file. Leaves ``rowHeaders.buckets`` untouched, so
    it stays dense at whatever the table's row count already had it at.
    """
    bds = model.objects[table_id].base_data_store
    tile_size = bds.tiles.tile_size or DEFAULT_TILE_SIZE
    tile_ref = bds.tiles.tiles[tile_index]
    tile_base_row = tile_ref.tileid * tile_size
    tile = model.objects[tile_ref.tile.identifier]
    kept = [
        r for r in tile.rowInfos if (tile_base_row + r.tile_row_index) in rows_to_keep
    ]
    del tile.rowInfos[:]
    tile.rowInfos.extend(kept)


def test_row_read_survives_a_sparsified_tile_with_stale_row_headers(configurable_save_file):
    doc = Document()
    table = doc.sheets[0].tables[0]
    for row in (2, 7, 11):
        table.write(row, 0, f"row {row} content")
    doc.save(configurable_save_file)

    # Fresh, empty-cache model -- nothing has read via storage_buffer() yet.
    model = _NumbersModel(configurable_save_file)
    table_id = model.table_ids(model.sheet_ids()[0])[0]
    num_rows = model.number_of_rows(table_id)

    bds = model.objects[table_id].base_data_store
    bucket = model.objects[bds.rowHeaders.buckets[0].identifier]
    tile = model.objects[bds.tiles.tiles[0].tile.identifier]
    assert len(bucket.headers) == num_rows, "starting point should be dense"
    assert len(tile.rowInfos) == num_rows, "starting point should be dense"

    # Simulate a real Numbers.app resave dropping the blank rows' storage
    # -- but leaving rowHeaders.buckets exactly as dense as it was.
    _sparsify_tile_in_place(model, table_id, 0, rows_to_keep={2, 7, 11})
    assert len(bucket.headers) == num_rows, "rowHeaders.buckets must stay stale/dense"
    assert len(tile.rowInfos) == 3

    # Only now build the Table that reads this state -- its own cache was
    # never touched before the mutation above.
    table = Table(model, table_id)

    assert table.cell(2, 0).value == "row 2 content"
    assert table.cell(7, 0).value == "row 7 content"
    assert table.cell(11, 0).value == "row 11 content"
    for row in range(num_rows):
        if row not in (2, 7, 11):
            assert table.cell(row, 0).value is None, f"row {row} should read as blank"


def test_row_read_survives_a_dropped_all_blank_tile(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    table = sheet.add_table("Big", num_rows=300, num_cols=2)
    # Row 0 lives in tile 0 (rows 0-255), row 280 lives in tile 1 (rows
    # 256-511 truncated to 300) -- tile 0 is left entirely blank so it can
    # be dropped outright, the way Numbers.app drops an all-blank tile.
    table.write(280, 0, "far row content")
    doc.save(configurable_save_file)

    model = _NumbersModel(configurable_save_file)
    table_ids = model.table_ids(model.sheet_ids()[0])
    table_id = next(t for t in table_ids if model.table_name(t) == "Big")
    num_rows = model.number_of_rows(table_id)

    bds = model.objects[table_id].base_data_store
    assert len(bds.tiles.tiles) == 2, "300 rows should split into two 256-row tiles"
    bucket = model.objects[bds.rowHeaders.buckets[0].identifier]
    assert len(bucket.headers) == num_rows

    # Simulate Numbers.app dropping tile 0 outright (entirely blank) while
    # rowHeaders.buckets stays at its original, now-stale row count.
    del bds.tiles.tiles[0]
    assert len(bucket.headers) == num_rows, "rowHeaders.buckets must stay stale/dense"

    table = Table(model, table_id)

    assert table.cell(280, 0).value == "far row content"
    for row in (0, 1, 50, 255, 279, 281, 299):
        assert table.cell(row, 0).value is None, f"row {row} should read as blank"


def test_duplicate_rowinfo_for_same_row_warns(configurable_save_file):
    """
    Two rowInfos deriving to the same absolute row is a corrupt file, not
    a real Numbers.app resave -- but silently picking one over the other
    would mask that corruption rather than surface it. Confirms the fix
    warns (via UnsupportedWarning) and still returns a usable value rather
    than raising.
    """
    doc = Document()
    table = doc.sheets[0].tables[0]
    table.write(2, 0, "original row 2 content")
    doc.save(configurable_save_file)

    model = _NumbersModel(configurable_save_file)
    table_id = model.table_ids(model.sheet_ids()[0])[0]
    bds = model.objects[table_id].base_data_store
    tile = model.objects[bds.tiles.tiles[0].tile.identifier]

    # Duplicate row 2's own rowInfo under a second, distinct tile_row_index
    # collision by cloning it and re-appending -- both now derive to row 2.
    duplicate = TSTArchives.TileRowInfo()
    duplicate.CopyFrom(next(r for r in tile.rowInfos if r.tile_row_index == 2))
    tile.rowInfos.append(duplicate)

    with pytest.warns(UnsupportedWarning, match="row 2 has more than one stored rowInfo"):
        table = Table(model, table_id)
    assert table.cell(2, 0).value == "original row 2 content"
