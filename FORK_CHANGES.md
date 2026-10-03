# Changes in this fork

This fork of [masaccio/numbers-parser](https://github.com/masaccio/numbers-parser) is at version 4.20.4. It branches from upstream at commit `9ada0bd` (version 4.20.0) and adds 35 commits. This document lists every change, the problem it addresses, the commit that carries it, the tests that cover it, and what each change depends on. Commit hashes refer to this repository's `main` branch.

Each bug fix is described the way it was reported: a symptom, the cause in the code, and the fix. Where a change was checked by opening files in Numbers.app, the entry says so. Where it is covered by unit tests only, the entry says that too.

## Contents

1. [Dependencies between changes](#dependencies-between-changes)
2. [Bug fixes](#bug-fixes)
3. [New features](#new-features)
4. [Running the tests](#running-the-tests)

## Dependencies between changes

Most changes stand alone. These do not:

| Change | Depends on | Why |
| --- | --- | --- |
| `897350e` border span grows into inserted rows | `a01ff9d` | Uses `shift_stroke_rows()` and `shift_stroke_columns()` and the stroke sidecar that `a01ff9d` introduces |
| `959ffa8` materialise `format_table` | `54bcfdd` | Replaces the read-in-place fallback that `54bcfdd` added |
| `47b0419` create required DataStore lists | `959ffa8` | Seeds `format_table` through the `DataLists` path that `959ffa8` adds |
| `23e156f` sizes follow inserted and deleted rows | `c7c4109` | Its tests cover the 0.0 auto-fit height, which only survives a save once `c7c4109` is in place |
| `3d34716` `Sheet.duplicate_image` | `161b8c9` | Builds on the `Image` class and the image drawable list |
| `161b8c9` `clear_ruler_guides` | `ddf1aef` | Clears the guide storage that `ddf1aef` reads and writes |
| `ab18786` style spacing | `a01ff9d` | Writes spacing through `update_paragraph_styles()`, which `a01ff9d` extends to scan cell data, so editing the spacing of an existing cell's style relies on it |

Everything else (the remaining bug fixes, `register_font`, header and footer text, `Table.locked`, the merge warning, and paper size) can be taken on its own.

## Bug fixes

### Borders

**Border moves with its row or column** (`a01ff9d`). `add_row()`, `add_column()`, `delete_row()` and `delete_column()` shifted cell data but not the border stroke sidecar, so a row's border stayed at its old index after an insert or delete. They now shift `row_column_index` and the cross-axis stroke run origin and length with the data. Tests: `tests/test_borders.py`.

**Border span grows into rows inserted before the first save** (`897350e`). The stroke sidecar is only populated at save time, so a row or column inserted in the same session, before any save, was not covered by the neighbouring border. `add_row()` and `add_column()` now read each cell's border from the sidecar first, compare the cells either side of the insertion, and extend a matching border across the gap. Tests: `tests/test_borders.py`.

**Borders survive writes and growth, and overlapping runs are clipped** (`ab9d388`). Writing a cell replaced it with a cell that lost its border. `write()` now carries the displaced cell's border forward. New stroke runs now clip or split any existing run they overlap, and bottom and right borders are mirrored onto the adjacent cell when the adjacent row or column did not exist at the time the border was set. Tests: `test_add_stroke_splits_overlap`, `test_border_survives_write`, `test_border_survives_row_growth`, `test_border_survives_column_growth` in `tests/test_borders.py`.

### Styles

**Never-styled cells use the table's own defaults** (`a01ff9d`). `cell_text_inset()`, `cell_text_wrap()`, `cell_alignment()` and `cell_bg_color()` returned library constants for a cell with no explicit style. They now fall back to the table's body cell style.

**Style changes persist after reopening** (`a01ff9d`). `Style.from_storage()` now resets its dirty flags on construction, and `update_paragraph_styles()` scans cell data as well as the named style registry. Changing an existing cell's text style after reopening a document now persists on save. The same commit stops an unrecognised custom font name raising `KeyError` on save, matching the read-side fallback. Tests: `tests/test_styles.py`.

**`KeyError: 0` on documents with unset style parents** (`54bcfdd`). A theme's root paragraph style presets have no `super.parent`. `char_property()`, `para_property()` and `cell_property()` dereferenced the unset reference, looked up object 0, and raised. They now walk to the parent only when `HasField("parent")` is true and otherwise return the field's proto default. This also stops a misleading "Custom font '' unsupported" warning. Tests: `tests/test_styles.py`, `tests/test_formatting.py`.

### Document structure and formats

**`format_table` is created when missing** (`54bcfdd`, `959ffa8`). `DataStore.format_table` is optional and absent from some older documents, which only carry the legacy `format_table_pre_bnc`. `54bcfdd` fell back to reading the legacy list. `959ffa8` replaces that: it creates `format_table` by copying the legacy list, so existing keys and reference counts stay valid, and points cell format ids at the new list. The commit message gives the evidence: in a scan of 174 tables in `tests/data`, cell format ids track `format_table` exclusively, and `tests/data/test-2.numbers` has key 3 in both lists with different format types. Tests: `tests/test_formatting.py`.

**Custom format list is created when missing** (`543d3a3`). `document.super.custom_format_list` is unset in `tests/data/issue-18.numbers`, so `doc.custom_formats`, `add_custom_format()` and `custom_format_map()` raised. `custom_format_list_archive()` now creates an empty archive in `Index/Document.iwa` and wires the reference. Tests: `tests/test_formatting.py`.

**Required DataStore lists are created at save** (`47b0419`). The commit message records the check: "Verified in Numbers 15.3.1 with side-by-side files." A Numbers-written table with only `format_table` removed renders blank, and `tests/data/issue-18.numbers`, written by SheetJS, lacks `format_table` and six other lists that every Numbers-written table in `tests/data` carries. `ensure_table_data_lists()` runs from `recalculate_table_data()` and creates any list in `REQUIRED_DATA_LISTS` that is absent. `multipleChoiceListFormatTable` is excluded because 12 tables written by older Numbers versions lack it and render fine. Across the 174 tables in `tests/data` the step changes nothing for any Numbers-written file. Tests: `tests/test_save.py`. Release label: `1bab532` (4.19.3).

**Stale `ColumnRowUIDMapArchive` is removed on the first save** (`772b101`). `recalculate_table_data()` removed unreferenced objects and then cleared the reference, so the archive stayed in the file until the next save. It now clears the reference first. Tests: `tests/test_save.py` checks that the number of these archives does not change between a first and second save.

### Tables, sheets and rows

**Tables and sheets created by `add_table()` and `add_sheet()` are complete** (`7158166`). Three gaps, fixed together:

1. Table identity. Numbers.app does not trust a freshly minted uuid1 as a table's identity and silently rebuilds it on first open. The kind-1 owner's UUID is now the byte reversal of the table's own `TableModelArchive.table_id`, written at the three sites the commit message lists. `derive_table_identity_uuid()` is in `numbers_uuid.py` and `register_table_identity_in_header_name_mgr()` is in `model.py`.
2. Sidebar registration. `add_table()` never added the new table to `DocumentArchive.sidebar_order`, and `add_sheet()` had the same gap for sheets. `register_table_in_sidebar()` and `register_sheet_in_sidebar()` fix both and do nothing when there is no sidebar tree.
3. `total_range_for_table` and `body_range_for_table` set `bottom_right_row` to `num_cols - 1` instead of `num_rows - 1`, which is wrong for any table that is not square.

Tests: `tests/test_table_identity_adoption.py`, `tests/test_uuids.py`, `tests/test_sidebar_registration.py`, `tests/test_formula_owner_spanning_ranges.py`.

**Row storage position comes from each row's own tile data** (`f002f4d`). `row_storage_map()` counted through `rowHeaders.buckets` to find a row's storage position. A real Numbers.app resave can drop the storage of an entirely blank row or tile without updating that structure, after which every later row resolved to the wrong content. The position is now computed from each rowInfo's own tile id and tile row index, and `row_storage_map()` is removed because nothing else called it. The commit message records the original observation as a real Numbers.app round trip of a 1000-row table. Tests: `tests/test_row_storage_desync.py` reproduces the fault directly and fails against the unfixed code.

**Row header cell count uses the row's own column count** (`68666de`). `recalculate_row_headers()` computed `numberOfCells` as `len(data)` (the table's row count) minus merged cells instead of `len(cells)` (the row's column count).

**Saved row heights and column widths survive resaves** (`c7c4109`). `Document.save()` rebuilt row and column headers through `row_height()` and `col_width()`, which treat a stored 0.0 as "never set" and return the default. A row set to auto-fit with `row_height(row, 0)` was rewritten to the default height on the next save. Header recalculation now uses a size set this session, else the size already stored in the file including 0.0, else the default. The getters are unchanged. The commit message records: "Verified in Numbers.app: a row set to auto-fit still fits its content after a single save and after further reopen/save cycles." Tests: `tests/test_table_size.py`.

**Row heights and column widths follow inserted and deleted rows and columns** (`23e156f`). Sizes are matched to rows and columns by position, and `add_row`, `delete_row`, `add_column` and `delete_column` never shifted them, so a custom size stayed at its old index while the content moved. Both stores, the session override dicts and the file's header buckets, now shift. An inserted row or column takes the size of the one at the insertion point, and appending changes nothing. Tests: `tests/test_table_size.py`. Depends on `c7c4109` as noted above.

### Formulas and dates

These three fixes apply to formulas and dates that already exist in a document. They do not add formula writing.

**Formula text keeps operator precedence** (`c43e702`, `2c8affd`). `cell.formula` lost grouping when it rebuilt text from the stored AST, so `(A1+B1)*2` was indistinguishable from `A1+B1*2`.

**Timezone-aware dates are written with the correct offset** (`c43e702`). `DateCell` serialisation called `astimezone()` on a naive epoch, which assumed local time and shifted every timezone-aware write by the local UTC offset. The epoch is now anchored as UTC first. Documented in `docs/api/datetime.rst` (`fc554b6`).

**`disable_experimental_feature()` clears flags** (`c43e702`). It toggled flags with `^=`, so disabling a flag that was not set turned it on. It now uses `&= ~flags`.

**Formula cells with an error value are kept on save** (`164b3c6`). `Cell._to_buffer()` had no branch for `ErrorCell`, so the catch-all returned `None` and dropped the cell's whole storage record, including `formula_id`. Any formula cell with a cached error lost `is_formula` on the next reopen. A dedicated branch now matches the on-disk shape. Test fixture: `tests/data/issue-42.numbers`.

**Cross-table references resolve through either owner UUID** (`248ad88`). `table_uuids_to_id()` resolved a reference through only the `HAUNTED_OWNER`-kind chain. When a formula's AST embedded the table's `TABLE_MODEL`-kind owner instead, resolution failed silently and the reference rendered as local to the same table. It now indexes both owner kinds.

## New features

### `register_font()` (`24964d9`)

Makes a font that Numbers does not ship usable in a `Style`. Unregistered fonts are still rejected, so typos are still reported.

```python
from numbers_parser import register_font

register_font("Roboto-Regular", family="Roboto")
style = doc.add_style(font_name="Roboto")
```

`name` is the PostScript name stored in the document, `family` is the name passed as `Style.font_name`, and it defaults to `name`. `style`, `bold` and `italic` describe the face. Documented in `docs/styles.rst` and `docs/limitations.rst` (`fc554b6`). Test: `test_register_font` in `tests/test_issues.py`.

### `Table.table_name_height` (`ddf1aef`)

Read-only height in points of the table's name banner. Numbers.app computes this when it renders the caption, so it reports 0.0 when `table_name_enabled` is `False`, and also for a table created in this session that Numbers.app has never saved. The two cases cannot be told apart from this value. Test: `test_table_name_height` in `tests/test_tables.py`.

### Ruler guides (`ddf1aef`, `161b8c9`)

```python
sheet = doc.sheets[0]
sheet.ruler_guides                     # list of guides
sheet.add_ruler_guide("horizontal", 120.0)
sheet.add_ruler_guide("vertical", 36.0)
sheet.clear_ruler_guides()
```

`axis` is `"horizontal"` or `"vertical"` and `position` is in points. The first guide on a sheet creates the guide storage. An invalid axis raises `ValueError`. Tests: `tests/test_ruler_guides.py`. Checked by opening saved files in Numbers.app.

### Images on a sheet (`161b8c9`, `3d34716`)

```python
image = sheet.add_image(data, "logo.png", x=10, y=10, width=200, height=80)
sheet.images                           # free-standing images, back to front
image.x, image.y, image.width, image.height   # readable and settable
image.data, image.filename
copy = sheet.duplicate_image(image, y=900)    # placed directly behind the original
sheet.remove_image(image)
```

Identical file content is stored once and shared between images through SHA1 deduplication. `remove_image` removes the drawable and leaves the package file in place, because another image can share it. Tests: `tests/test_images.py`. `Image` is documented in `docs/api/sheet.rst`. Duplicated images were checked by opening saved files in Numbers.app.

### Header and footer text (`ab18786`)

```python
sheet.header_text(zone=0)              # zones 0, 1, 2 run left to right
sheet.set_header_text("Left text", zone=0)
sheet.set_footer_text("Footer text", zone=0)
sheet.footer_char_runs()               # [(character_index, style id or None), ...]
sheet.set_footer_text(text, runs=[(0, bold_id), (10, None), (12, light_id)])
```

Setting text keeps the zone's existing style. Character style runs and the other per-character run tables are keyed to character offsets in the old text, so runs that start at or past the end of a shorter new text are dropped and the text after the last kept run takes that run's style. The optional `runs` argument replaces the character style runs outright, using the ids that `header_char_runs()` and `footer_char_runs()` return. Setting a zone that does not exist raises. Tests: `tests/test_header_footer.py`. Checked by opening saved files in Numbers.app.

### `Style.baseline_shift` and `Style.line_spacing` (`ab18786`)

`baseline_shift` is in points. `line_spacing` is a relative multiple of the line height, where 1.0 is single spacing. Both default to `None`, which leaves the value unset so a new style inherits it. Reading a style returns the effective value, following the parent chain. Only relative line spacing is reported: a style that uses exact, minimum, maximum or space-between spacing returns `None`, because those amounts are in points and do not fit one relative value.

```python
style = doc.add_style(name="Tight", line_spacing=0.9, baseline_shift=-1.0)
table.set_cell_style(0, 0, style)
```

Tests: `tests/test_style_spacing.py`, covering create, save and reload, `None` round trip, and editing an existing style. Covered by unit tests only.

### `Table.locked` (`ab18786`)

Read and write property. A locked table cannot be moved, resized or edited in Numbers.app. The value is the `locked` field of the table's drawable wrapper, `TableInfoArchive.super`. Test: `test_table_locked_round_trip` in `tests/test_tables.py`. Covered by unit tests only.

### Merge boundary warning (`ab18786`)

`Table.merge_cells()` raises a `RuntimeWarning`, and still performs the merge, when the range crosses the header column boundary or the header row boundary. The header column case was confirmed by opening files in Numbers.app: Numbers.app disregards such a merge at render time even though `merge_cells()` completes and the merged cell reads back as merged after a save and reload. The header row warning says in its own text that the behaviour is inferred from the header column case and was not checked independently. Tests: `test_merge_crossing_header_column_boundary_warns` and `test_merge_crossing_header_row_boundary_warns` in `tests/test_merges.py`.

### Paper size and orientation (`ab18786`)

```python
doc.paper_size = "a3"                  # "a4" or "a3"; reads back the name
doc.page_dimensions                    # (width, height) in points, portrait
doc.set_paper_size("iso-a4", 595, 842) # any other size
doc.sheets[0].orientation = "landscape"
```

Numbers stores portrait dimensions for landscape documents and records the orientation on each sheet, so `page_dimensions` always reports portrait values. `paper_size` and `orientation` raise `ValueError` for values they do not accept. Margins and content scale are not part of this change. Tests: `tests/test_paper.py`. The paper and orientation values were checked by opening saved A4 and A3, portrait and landscape files in Numbers.app.

## Running the tests

```
pip install -e . pytest pytest-cov
pytest
```

Requirements outside the package:

- `python-snappy` 0.7 or later. It installs without a system snappy library.
- `tests/test_issues.py` and `tests/test_unpack_numbers.py` import `python-magic`, which needs a native libmagic.
- `tests/test_cat_numbers.py` and the CLI tests start subprocesses and need the `numbers-parser` console scripts on `PATH`.

On the maintainer's Mac the full suite passed at an earlier point in this fork's history with 305 passed and 6 skipped. In the sandbox used to prepare 4.20.4, the suite passes with 325 passed and 6 skipped when the subprocess tests are deselected (`-k "not subprocess"`), which is 9 tests.
