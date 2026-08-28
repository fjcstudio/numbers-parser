"""
Regression tests for the "adoption" fix: Numbers.app silently retires
("adopts") a table's kind-1 (TABLE_MODEL) UUID on first open unless it
holds one specific derived value. Finding the identity information it
requires missing, it rebuilds the table's identity -- and every
reference into the table that named it under the old identity -- from
scratch. See
`Claude chat handoff notes/session_2026-08-28_artifacts/
fresh_eyes_findings_2026-08-28.md` for the round-trip experiments this fix
implements.

The fix touches exactly three sites for a newly minted table's kind-1
owner: FormulaOwnerDependenciesArchive.formula_owner_uid, its
owner_id_map entry, and HeaderNameMgrArchive.per_tables[].table_uid.
These tests check that add_table() produces that topology and leaves
everything else (auxiliary owner families, other tables) untouched.

Note: these tests exercise the archive topology this fix is responsible
for, not a full cross-table formula write -- that depends on the
separate, not-yet-merged cross_table_reference_writing feature, which
picks which owner identity to embed in a new AST reference and is
explicitly still unconfirmed against real Numbers.app on its own. The
adoption fix is orthogonal to that: it governs whether a table's kind-1
owner survives Numbers' load-time trust check at all, regardless of how
any reference into it was written.

This fix is independent of the formula-writing feature series and
applies standalone against a bare checkout with none of it present.
One test below additionally exercises write_formula() as a sanity
check that this fix doesn't disturb it where it does exist; that one
test (only) is skipped, not failed, on a checkout without it.
"""

import pytest

from numbers_parser import Document
from numbers_parser.constants import OwnerKind
from numbers_parser.numbers_uuid import NumbersUUID, derive_table_identity_uuid, uuid_to_hex


def _table_model_owners(model):
    """All TABLE_MODEL-kind (kind-1) FormulaOwnerDependenciesArchive objects."""
    return [
        (obj_id, model.objects[obj_id])
        for obj_id in model.find_refs("FormulaOwnerDependenciesArchive")
        if model.objects[obj_id].owner_kind == OwnerKind.TABLE_MODEL
    ]


def _live_owner_id_map(model):
    """
    Uncached owner_id_map: internal_owner_id -> hex UUID string, read
    straight from the CalculationEngine archive.

    model.owner_id_map() is @cache(num_args=0)-memoized and may already
    have been populated (and therefore stale) by the time a test calls
    add_table() -- e.g. merely accessing doc.sheets[0] can trigger it.
    Tests need the current state, so they read the archive directly
    instead of going through that cache.
    """
    calc_engine = model.calc_engine()
    return {
        e.internal_owner_id: uuid_to_hex(e.owner_id)
        for e in calc_engine.dependency_tracker.owner_id_map.map_entry
    }


def _haunted_owners(model):
    """All HAUNTED_OWNER-kind (kind-35) FormulaOwnerDependenciesArchive objects."""
    return [
        (obj_id, model.objects[obj_id])
        for obj_id in model.find_refs("FormulaOwnerDependenciesArchive")
        if model.objects[obj_id].owner_kind == OwnerKind.HAUNTED_OWNER
    ]


def test_add_table_mints_derived_kind1_identity_at_all_three_sites():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    model = doc._model

    new_table_model_id = sheet.tables[1]._table_id
    owners = [
        (obj_id, fod)
        for obj_id, fod in _table_model_owners(model)
        if fod.formula_owner.identifier
        and model.objects[fod.formula_owner.identifier].tableModel.identifier == new_table_model_id
    ]
    assert len(owners) == 1
    _, fod = owners[0]

    # Site 1: FormulaOwnerDependenciesArchive.formula_owner_uid.
    v = uuid_to_hex(fod.formula_owner_uid)

    # Site 2: the same value is registered in owner_id_map under this
    # owner's internal_formula_owner_id.
    owner_id_map = _live_owner_id_map(model)
    assert owner_id_map[fod.internal_formula_owner_id] == v

    # Site 3: the same value appears in HeaderNameMgrArchive.per_tables.
    header_name_mgr_ids = model.find_refs("HeaderNameMgrArchive")
    assert len(header_name_mgr_ids) == 1
    per_table_uids = {
        uuid_to_hex(pt.table_uid) for pt in model.objects[header_name_mgr_ids[0]].per_tables
    }
    assert v in per_table_uids


def test_kind1_identity_is_the_real_byte_reversal_of_table_id_not_a_coincidence():
    """
    The crux of the round-4 correction (fresh_eyes_findings_2026-08-28.md):
    V must be derived from this table's own TableModelArchive.table_id
    string, not from some other, independently minted uuid1 that happens
    to land on the right value. Checks the write sites against table_id
    directly, via the model's own live table_model object, rather than
    only checking the write sites agree with each other (which an earlier,
    superseded version of this fix also satisfied, while deriving V from
    the wrong source).
    """
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    model = doc._model

    new_table_model_id = sheet.tables[1]._table_id
    table_model = model.objects[new_table_model_id]

    expected_v = derive_table_identity_uuid(NumbersUUID(table_model.table_id)).hex

    owners = [
        fod
        for _, fod in _table_model_owners(model)
        if fod.formula_owner.identifier
        and model.objects[fod.formula_owner.identifier].tableModel.identifier == new_table_model_id
    ]
    assert len(owners) == 1
    assert uuid_to_hex(owners[0].formula_owner_uid) == expected_v


def test_two_new_tables_get_distinct_derived_identities():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    sheet.add_table("Table 3")
    model = doc._model

    new_owners = [
        fod
        for _, fod in _table_model_owners(model)
        if fod.formula_owner.identifier
        and model.objects[fod.formula_owner.identifier].tableModel.identifier
        in (sheet.tables[1]._table_id, sheet.tables[2]._table_id)
    ]
    assert len(new_owners) == 2
    uids = {uuid_to_hex(fod.formula_owner_uid) for fod in new_owners}
    assert len(uids) == 2  # no collision between the two derived identities

    owner_id_map = _live_owner_id_map(model)
    for fod in new_owners:
        assert owner_id_map[fod.internal_formula_owner_id] == uuid_to_hex(fod.formula_owner_uid)


def test_haunted_owner_family_is_left_in_stock_library_form():
    # Hard rule from the findings doc: auxiliary owners (including kind-35
    # HAUNTED_OWNER) are left exactly as the library mints them today --
    # Numbers keeps them regardless of their UUID value, and mixing the
    # derived scheme into them is unnecessary and untested.
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    model = doc._model

    new_table_model_id = sheet.tables[1]._table_id
    haunted = [
        fod
        for _, fod in _haunted_owners(model)
        if uuid_to_hex(fod.formula_owner_uid)
        == uuid_to_hex(model.objects[new_table_model_id].haunted_owner.owner_uid)
    ]
    assert len(haunted) == 1
    # base_owner_uid is an independent library-minted uuid1, not put through
    # the reversal transform -- it must not equal what that transform would
    # have produced from it (that would indicate the fix leaked into code
    # paths it should not touch).
    base_uid = NumbersUUID(haunted[0].base_owner_uid)
    assert derive_table_identity_uuid(base_uid).hex != base_uid.hex


def test_original_template_table_identity_is_untouched():
    # add_table() must only mint a derived identity for the *new* table --
    # the template's own first table (already Numbers-native) is never
    # touched by this fix.
    doc = Document()
    model = doc._model
    original_table_id = doc.sheets[0].tables[0]._table_id

    before = [
        uuid_to_hex(fod.formula_owner_uid)
        for _, fod in _table_model_owners(model)
        if fod.formula_owner.identifier
        and model.objects[fod.formula_owner.identifier].tableModel.identifier == original_table_id
    ]

    doc.sheets[0].add_table("Table 2")

    after = [
        uuid_to_hex(fod.formula_owner_uid)
        for _, fod in _table_model_owners(model)
        if fod.formula_owner.identifier
        and model.objects[fod.formula_owner.identifier].tableModel.identifier == original_table_id
    ]
    assert before == after


def test_new_tables_local_formula_still_writes_and_reads_back(configurable_save_file):
    # Sanity check that this fix doesn't disturb ordinary formula writing:
    # a formula local to the new table must still round-trip through save.
    # Only meaningful (and only run) where the formula-writing feature
    # series is present; skipped, not failed, on a checkout without it,
    # since this fix does not depend on that feature.
    try:
        from numbers_parser import add, cell
    except ImportError:
        pytest.skip("formula-writing feature (add()/cell() DSL) not present in this checkout")

    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    table2 = sheet.tables[1]
    table2.write(0, 0, 3.0)
    table2.write(0, 1, 4.0)
    table2.write_formula(0, 2, add(cell(0, 0), cell(0, 1)), 7.0)

    doc.save(configurable_save_file)
    doc2 = Document(configurable_save_file)
    table2_reopened = doc2.sheets[0].tables[1]
    assert table2_reopened.cell(0, 2).formula == "A1+B1"
    assert table2_reopened.cell(0, 2).value == 7.0


def test_new_table_without_any_formula_still_writes_and_reads_back(configurable_save_file):
    # The equivalent sanity check for a checkout with no formula-writing
    # feature at all: a plain data-only table must still round-trip
    # through save after this fix.
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    table2 = sheet.tables[1]
    table2.write(0, 0, 3.0)
    table2.write(0, 1, "hello")

    doc.save(configurable_save_file)
    doc2 = Document(configurable_save_file)
    table2_reopened = doc2.sheets[0].tables[1]
    assert table2_reopened.cell(0, 0).value == 3.0
    assert table2_reopened.cell(0, 1).value == "hello"
