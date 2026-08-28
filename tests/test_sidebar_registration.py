"""
Regression tests for add_table()'s and add_sheet()'s sidebar/navigator
registration.

add_table() added new tables to the sheet's own drawable_infos but
never to DocumentArchive.sidebar_order (the separate TreeNode structure
Numbers.app's navigator sidebar reads), so library-created tables were
absent from the sidebar. add_sheet() had the identical gap for sheets
themselves -- and, as a consequence, for every table on a
library-added sheet too, since register_table_in_sidebar() has nothing
to attach a table's node to without a sidebar node for its sheet.
Neither has anything to do with formula writing -- both run the same
way whether or not a table ever gets a formula -- so this is a plain
base-library correctness gap, not part of the formula-writing feature.

It also has nothing to do with the kind-1 owner "adoption" fix
(derive_table_identity_uuid()): the two were previously observed
together only because Numbers minted a sidebar TreeNode as a side
effect of fully reprocessing a table it didn't trust. Now that
adoption no longer fires for a library-created table, nothing implicit
would add that TreeNode any more, so it has to be written directly.
"""

from numbers_parser import Document


def _sidebar_object_ids(model):
    """
    Every `object` id referenced anywhere in the sidebar tree
    (DocumentArchive.sidebar_order), read straight from the archive.
    """
    document = model.objects[1]
    assert document.HasField("sidebar_order")
    ids = set()

    def walk(node_id):
        node = model.objects[node_id]
        if node.HasField("object"):
            ids.add(node.object.identifier)
        for child in node.children:
            walk(child.identifier)

    walk(document.sidebar_order.identifier)
    return ids


def test_add_table_registers_in_sidebar():
    doc = Document()
    model = doc._model
    sheet = doc.sheets[0]

    before = _sidebar_object_ids(model)
    original_table_info_id = model.table_info_id(sheet.tables[0]._table_id)
    assert original_table_info_id in before

    sheet.add_table("Table 2")
    new_table_info_id = model.table_info_id(sheet.tables[1]._table_id)

    after = _sidebar_object_ids(model)
    assert new_table_info_id in after
    # The original table's own entry is untouched.
    assert original_table_info_id in after


def test_add_table_sidebar_node_is_a_child_of_its_sheets_node():
    doc = Document()
    model = doc._model
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")

    document = model.objects[1]
    root = model.objects[document.sidebar_order.identifier]
    sheet_node = next(
        model.objects[ref.identifier]
        for ref in root.children
        if model.objects[ref.identifier].object.identifier == sheet._sheet_id
    )
    child_object_ids = {
        model.objects[ref.identifier].object.identifier for ref in sheet_node.children
    }
    new_table_info_id = model.table_info_id(sheet.tables[1]._table_id)
    assert new_table_info_id in child_object_ids


def test_two_new_tables_both_get_distinct_sidebar_nodes():
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    sheet.add_table("Table 3")
    model = doc._model

    ids = _sidebar_object_ids(model)
    ti2 = model.table_info_id(sheet.tables[1]._table_id)
    ti3 = model.table_info_id(sheet.tables[2]._table_id)
    assert ti2 in ids
    assert ti3 in ids
    assert ti2 != ti3


def test_add_sheet_registers_the_sheet_and_its_table_in_sidebar():
    doc = Document()
    model = doc._model
    before = _sidebar_object_ids(model)

    doc.add_sheet("Sheet 2")
    sheet2 = doc.sheets[1]

    after = _sidebar_object_ids(model)
    assert sheet2._sheet_id in after
    assert sheet2._sheet_id not in before
    # add_sheet() itself creates the new sheet's first table via
    # add_table(), so that table's own registration must have found the
    # sheet node register_sheet_in_sidebar() just created.
    new_table_info_id = model.table_info_id(sheet2.tables[0]._table_id)
    assert new_table_info_id in after


def test_add_sheet_node_is_a_direct_child_of_the_root():
    doc = Document()
    model = doc._model
    doc.add_sheet("Sheet 2")
    sheet2 = doc.sheets[1]

    document = model.objects[1]
    root = model.objects[document.sidebar_order.identifier]
    top_level_object_ids = {
        model.objects[ref.identifier].object.identifier
        for ref in root.children
        if model.objects[ref.identifier].HasField("object")
    }
    assert sheet2._sheet_id in top_level_object_ids


def test_sidebar_registration_on_a_document_with_no_sidebar_order_does_not_raise():
    # Defensive fallback for any document shape without a sidebar tree at
    # all (there is no such fixture available, so this exercises the
    # no-op path directly rather than trying to construct one).
    doc = Document()
    model = doc._model
    document = model.objects[1]
    document.ClearField("sidebar_order")

    doc.add_sheet("Sheet 2")  # must not raise
    doc.sheets[1].add_table("Table X")  # must not raise


def test_sidebar_registration_survives_save(configurable_save_file):
    doc = Document()
    sheet = doc.sheets[0]
    sheet.add_table("Table 2")
    doc.save(configurable_save_file)

    doc2 = Document(configurable_save_file)
    model2 = doc2._model
    new_table_info_id = model2.table_info_id(doc2.sheets[0].tables[1]._table_id)
    assert new_table_info_id in _sidebar_object_ids(model2)
