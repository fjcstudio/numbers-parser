# Review of the fjcstudio fork, 2026-10-03

This document records two code reviews of the `fjcstudio/numbers-parser` fork, what each found, how each finding was confirmed, the commit that fixed it, the tests that cover it, and what is still open. `FORK_CHANGES.md` at the repository root describes every fork change as it stands; this document is the review trail behind the changes made on 2026-10-03.

All commit hashes refer to this repository. Both reviews' fixes reached `main` in the merge of #3 (`3774dd3`), version 4.20.4. "All tests" passed on that commit on Python 3.10, 3.11, 3.12, 3.13 and 3.14 (GitHub Actions run 37096378336).

## Contents

1. [Scope and method](#scope-and-method)
2. [Review 1: the 4.20.4 commits](#review-1-the-4204-commits)
3. [Review 2: the fork's older commits](#review-2-the-forks-older-commits)
4. [Findings made while fixing](#findings-made-while-fixing)
5. [CI investigation: `test_memory_leaks` on Python 3.12](#ci-investigation-test_memory_leaks-on-python-312)
6. [Effect on the fjcstudio skills](#effect-on-the-fjcstudio-skills)
7. [Still open](#still-open)
8. [Pull requests and commits](#pull-requests-and-commits)

## Scope and method

| Review | Range | Commits | Trigger |
| --- | --- | --- | --- |
| 1 | `7b871d2..1f3ca16` | 5 (`ddf1aef`, `161b8c9`, `3d34716`, `ab18786`, `1f3ca16`) | Merged to `main` without review |
| 2 | `9ada0bd..7b871d2` | 29 non-merge commits | The fork's changes from upstream 4.20.0 to 4.20.3 |

Each review read the diff for correctness bugs, checked documentation claims against the code, and ran `ruff check --no-fix` and `ruff format --check` on lines added in the range. A finding is marked:

- **Reproduced**: a script against `Document()` or a file in `tests/data` showed the fault.
- **Read**: found by reading the code; the fix's test then showed the fault on the unfixed code.

Every fix has a regression test that failed on the commit before the fix and passes after it, unless the table says otherwise. Ruff's configuration sets `fix = true`, so all lint runs used `--no-fix`.

## Review 1: the 4.20.4 commits

### CI had not run

Every "All tests" job on the five commits stopped at install with `error: The lockfile at uv.lock needs to be updated, but --locked was provided.` `pyproject.toml` moved 4.20.3, 4.21.0, 4.22.0, 4.20.4 without `uv.lock`. `uv lock --check` passed on `7b871d2` and failed from `ddf1aef` onward. No test had run in CI on any of the five commits.

Fix: `46da2bb` regenerates `uv.lock` with uv 0.12.20, which keeps lockfile revision 3; the only change is the `numbers-parser` version line. uv 0.12.22 accepts it.

### Findings

| # | Finding | Where | Confirmed | Fix | Tests |
| --- | --- | --- | --- | --- | --- |
| 1.1 | `add_image()` recorded the DataInfo and digest before `store_image()`, which raises when the file name is taken. The next `add_image()` of the same content returned the first image's data (`b'first'`) and file name. | `model.py` `add_image()` | Reproduced | `ade2064`: store the file first | `test_failed_add_image_leaves_no_record` |
| 1.2 | `set_header_text()` / `set_footer_text()` replaced the text but kept `table_attachment` entries. On the default template, `set_footer_text("Draft", zone=1)` left the page number field attached to "D". | `model.py` `set_header_footer_text()` | Reproduced | `ade2064`: keep an attachment or footnote only where the new text has U+FFFC at its index | `test_replacing_a_field_drops_its_attachment`, `test_field_kept_where_its_placeholder_remains` |
| 1.3 | `runs=` raised `ValueError: zone has no character style runs to extend` on every default zone, after the text had already been replaced. | `model.py` `set_header_footer_text()` | Reproduced | `ade2064`: build entries directly; validate `runs` first | `test_runs_on_a_zone_without_char_styles`, `test_invalid_runs_leave_the_zone_unchanged` |
| 1.4 | `duplicate_image()` copied the title and caption one level deep, so the copy shared their text storage, and shared the mask. | `model.py` `duplicate_image()` | Read; the test showed both images' caption storage as id 1000002 | `ade2064`: copy `owned_storage`, `placement` and the mask; set each copy's parent to the new image | `test_duplicate_image_owns_its_caption_title_and_mask` |
| 1.5 | `duplicate_image()` with an image from another sheet raised a bare `StopIteration` after creating orphan objects. | `model.py` `duplicate_image()` | Reproduced | `ade2064`: raise `IndexError` before creating anything | `test_duplicate_image_from_another_sheet_raises` |
| 1.6 | `test_unset_spacing_is_not_forced` asserted `baseline_shift == 0.0`, the default, so it could not fail. | `tests/test_style_spacing.py` | Read; confirmed by forcing `baseline_shift` in `add_style()`: the old test passed, the new one failed | `ade2064`: assert the saved style leaves both fields unset | The test itself |
| 1.7 | `FORK_CHANGES.md` and the `Style` docstring said `baseline_shift` follows the parent chain and round-trips as `None`. It is read through `char_property()` (style and direct parent) and returns 0.0 when unset, which `test_unset_line_spacing_round_trips_as_none` already asserts. | `FORK_CHANGES.md`, `cell.py` | Read | `ade2064`: documentation corrected; code unchanged | n/a |
| 1.8 | 35 ruff findings and 4 unformatted files on new lines. | Several | `ruff check --no-fix` | `ade2064`; `PLR0917` on `add_image` left, since fixing it changes the public signature | n/a |

## Review 2: the fork's older commits

The review found the following, in addition to confirming that these hold: the `ErrorCell` storage branch (`164b3c6`), timezone anchoring and `&= ~flags` (`c43e702`), row storage keyed by tile (`f002f4d`), the row header cell count (`68666de`), the `bottom_right_row` range fix (`7158166`), the ColumnRowUIDMap ordering (`772b101`), and the DataStore list claims (174 tables in `tests/data`; only `issue-18.numbers` lacks the required lists; 13 tables lack `multipleChoiceListFormatTable`, 12 plus `issue-18`).

| # | Finding | Origin | Confirmed | Fix | Tests |
| --- | --- | --- | --- | --- | --- |
| 2.1 | Deleting the last row or column dropped the table's outer bottom or right border, and the sidecar kept its old `row_count` and `column_count`. The result depended on whether a border had been read earlier. | `a01ff9d` | Reproduced | `12fda19` | `test_deleting_the_last_row_keeps_the_outer_border`, `test_deleting_the_last_column_keeps_the_outer_border`, `test_deleting_without_borders_updates_the_sidecar_size`, `test_deleting_the_first_row_keeps_the_outer_border` (already passed; kept as a guard) |
| 2.2 | Inserting a row at a horizontal border drew two lines: `add_row(1, start_row=1)` with a border between rows 0 and 1 gave tops `['-','top','top','-','-']`. | `897350e` | Reproduced | `12fda19` | `test_inserting_a_row_at_a_horizontal_border_keeps_one_line`, `test_inserting_a_column_at_a_vertical_border_keeps_one_line` |
| 2.3 | `(A÷B)%` rendered as `A÷B%`, which reads as `A÷(B%)`, 10,000 times larger. `A×(B÷C)` followed by `%` was wrong the same way. | `c43e702`, `2c8affd` | Reproduced | `cc556a4` | `test_percent_needs_parens_around_division`, `test_percent_needs_parens_when_division_ends_a_product`, `test_percent_needs_parens_around_negated_division`, `test_percent_of_division_as_percent_operand_stays_unbracketed` |
| 2.4 | Editing one reopened cell's style changed every cell sharing its paragraph style and the named style: three cells at 11 pt, one edited to 30, all three saved at 30. | `a01ff9d` | Reproduced | `6dc407d` | `test_editing_one_cell_of_a_shared_style_changes_only_that_cell` |
| 2.5 | Never-styled header, header-column and footer cells reported the body cell style's defaults: on the default table a header cell reported no fill while the header row style is grey. | `a01ff9d` | Reproduced | `5a8d3b5` | `test_unstyled_header_cells_report_header_styles` |
| 2.6 | The insert-side stroke sidecar shift had no observable effect, because `add_row()` and `add_column()` load strokes first and save then rebuilds the sidecar. The unreachable `_shift_stroke_runs_on_insert()` also grew a run that only touched the insert point. | `a01ff9d` | Reproduced: with the shifts replaced by no-ops, all 24 border tests still passed | `12fda19`: about 210 lines of sidecar shifting removed | Existing border tests |
| 2.7 | `register_font()` made the first face registered the family default, and silently replaced built-in font entries for the whole process. | `24964d9` | Read | `1604c7a` | `test_register_font_prefers_the_regular_face_as_family_default`, `test_register_font_rejects_built_in_fonts`, `test_register_font_argument_types` |
| 2.8 | `calculate_table_uuid_map()` built the extra table owner index after the early return for documents with no haunted owners, so it was never set (hidden by `getattr(..., {})`) and never reset. | `248ad88` | Read; confirmed on `issue-18.numbers`, where the attribute was unset | `0cbc2e0` | `test_extra_owner_uuid_map_is_built_without_haunted_owners` |
| 2.9 | Comments and docstrings cited files not in the repository (`fresh_eyes_findings_2026-08-28.md`, `row_storage_desync_bug_report.md`) and a symbol that does not exist (`_SCALING_OPS`). | Several | Read | `a6c9e25`, `cc556a4` | n/a |
| 2.10 | `FORK_CHANGES.md` claimed `a01ff9d` stops an unknown font name raising on save; that code (`_paragraph_style_font_name()`) was dropped in the upstream 4.20.0 merge (`1aecbeb`). It also said `959ffa8` points cell format ids at a new list; it creates the list the ids already refer to. | `FORK_CHANGES.md` | Read; `git log -S` located the removal | `7b8c29e` | n/a |

### How the border fix works (`12fda19`)

Every insert and delete now loads borders onto the cells and marks the table, so save rebuilds the stroke sidecar from the cells at the table's new size. A line between two rows is held on the lower row's top and mirrored on the upper row's bottom. `prepare_borders_for_insert()` clears the upper row's mirror so the line stays only above the row that moves. `prepare_borders_for_delete()` joins the neighbours of a deleted block with the line below it, and keeps the table's outer edge when deleting through the last row or from the first. Columns follow the same rules.

### How the style edit fix works (`6dc407d`)

When a reopened cell's text style is edited and its paragraph style is a named style or is used by another cell, the edit goes into a new unnamed variation, stored the way Numbers stores a cell override: `is_variation` set, the named style as parent, listed in the stylesheet's `styles` and `parent_to_children_style_map`. The shape was taken from the 96 overrides Numbers wrote in the files in `tests/data`. A never-styled cell gets a variation of its position's default text style, and a style used by that cell alone is edited in place. An edited cell still reports its named style's name.

## Findings made while fixing

These were not in either review's findings list; they came up while fixing them.

| Finding | Origin | Confirmed | Fix | Tests |
| --- | --- | --- | --- | --- |
| `Style.__post_init__` replaced the font details read from the file with the family default face, so saving an edited Roboto Light cell wrote Roboto Regular, and a font the library does not know was saved as the default font. | `a01ff9d` with upstream 4.20.0 | Test failed: stored `Roboto-Regular` instead of `Roboto-Light` | `6dc407d` | `test_editing_a_reopened_style_keeps_its_font_face`, `test_editing_a_reopened_style_keeps_an_unknown_font` |
| Setting `font_name` on an existing style did not change the saved font. | `cell.py` `Style.__setattr__` | Test failed: read back `Helvetica Neue` instead of `Arial` | `6dc407d` | `test_setting_font_name_on_a_reopened_style_changes_the_font` |
| Text style edits to a never-styled reopened cell were dropped, because the cell has no paragraph style of its own. | `a01ff9d` | Test failed: 10.0 instead of 20.0 | `6dc407d` | `test_editing_an_unshared_cell_style_reuses_it` |
| The three `test_unstyled_cell_inherits_table_default_*` tests set the body style but checked cell (0,0), a header cell. The vertical alignment test compared an enum with the string `"top"`, and the fill test checked only `is not None`, so none could fail. | `a01ff9d` | Read; with the fallback disabled, all four tests now fail | `5a8d3b5`: use body cell (1,1) with exact values | The tests themselves |
| `test_register_font` left Roboto registered for later tests in the same process. | `24964d9` | Read | `1604c7a`: `restore_font_maps` fixture in `tests/conftest.py` | n/a |
| `test_debug` in `tests/test_unpack_numbers.py` left the `numbers_parser` logger at `DEBUG`, and pytest kept about 1,600 `LogRecord` objects per load in `test_memory_leaks` on the same worker. | Upstream test order dependency | Reproduced: running `test_unpack_numbers.py` then `test_memory_leaks.py` in one process failed on the fork and 2 of 2 times on upstream `main` | `bcd4bca` | `test_memory_leaks` |

## CI investigation: `test_memory_leaks` on Python 3.12

After `ade2064`, "Run Tests (3.12)" failed twice with exactly one extra object (`177221 - 177220`, then `177235 - 177234`) while Python 3.10, 3.11, 3.13 and 3.14 passed on the same commits.

Ruled out locally, on the runner's interpreter (Ubuntu 24.04 `/usr/bin/python3.12`, 3.12.3): the full suite with 4 workers, with and without coverage (7 of 7 passed); each test file paired with the leak test; the whole suite in one process with the leak test last; 25 loads of `tests/data/test-1.numbers` (delta 0 every time, on 4.20.3 and after); background threads (an xdist worker has only `MainThread`); warnings on load (none). One leaked `Document` holds about 5,450 objects.

Likely cause: the test kept the previous iteration's pympler summary alive while it counted again, and each summary row is a list, so a change of one in the number of object types alive in the worker showed up as one extra object. `f555bfb` writes the summary to a file in `tmp_path`, deletes it before the next count, and lists the grown types in the failure message; the assertions are unchanged. The 3.12 job has passed on every run since. This is the likely cause rather than a proven one: the growth never reproduced locally. `b972272` records this in the test.

## Effect on the fjcstudio skills

The fjcstudio-numbers and fjcstudio-programme examples (each skill's documented workflow) were run against 4.20.3, the version both skills pin (`0b4b578`), and against the fixed code, and every cell was compared:

| Compared | Entries | Result |
| --- | --- | --- |
| Borders and stroke sidecar sizes | 1,457 | Identical |
| Reported cell styles | 1,450 | Identical except the fill of never-styled header-row cells: `None` instead of white. The branded template's header row style has no fill, so `None` is what Numbers shows. The saved files have the same tables, table styles and number of cells with cell styles (25 and 436). |

The fjcstudio-numbers example also saved with the branded fonts stored as `fjcstudioBold` and `fjcstudioLight` after the `register_font()` change. The skills keep their current pin until they are repinned to a commit with these fixes.

## Still open

1. **Resolved: edited cell styles were full copies, not variations of the table style.** Fixed in `abda991`, with the edge cases (gradient fills, removed fills, image fills, dangling style keys) in `5dcae99` and `a34a86d`: a cell-level edit (fill, inset, wrap, vertical alignment) is now an unnamed variation of the table style holding only the changed values. See `FORK_CHANGES.md`. Saved files opened in Numbers.app confirmed the edited cells, the removed header fill and the following of table style changes. The image fill changes in `a34a86d` are covered by unit tests only.
2. **Not checked in Numbers.app.** No change from either review was confirmed by opening files in Numbers.app. The `duplicate_image()` copying of titles, captions and masks is covered by unit tests only, because images created by this library have none of those objects.
3. **`PLR0917` on `add_image()`** (model and `Sheet`) is left, since fixing it means keyword-only arguments.
4. **Repository settings.** The Code coverage job fails at the Codecov upload ("Token required - not valid tokenless upload") because there is no `CODECOV_TOKEN` secret, and the Sphinx deploy job fails because GitHub Pages is not enabled. The tests in the coverage job pass.

## Pull requests and commits

| PR | Branch | Content | Merged |
| --- | --- | --- | --- |
| #3 | `claude/awesome-bell-39k1nn` | Review 1 fixes, formatting of older code, `FORK_CHANGES.md` coverage, the `test_memory_leaks` change | `3774dd3` into `main` |
| #4 | `claude/older-review-fixes` | Review 2 fixes | `218837c` into #3's branch, then `main` with #3 |

| Commit | Purpose |
| --- | --- |
| `46da2bb` | `uv.lock` for 4.20.4 |
| `ade2064` | Review 1 findings 1.1 to 1.8 |
| `50bdce0` | `ruff format` on older fork code |
| `7b8c29e` | `FORK_CHANGES.md`: every commit covered, two claims corrected |
| `f555bfb`, `b972272` | `test_memory_leaks` summary handling and its explanation |
| `cc556a4` | Finding 2.3 |
| `1604c7a` | Finding 2.7 |
| `0cbc2e0` | Finding 2.8 |
| `5a8d3b5` | Finding 2.5 and the three unstyled cell tests |
| `6dc407d` | Finding 2.4 and the font and never-styled cell findings |
| `12fda19` | Findings 2.1, 2.2 and 2.6 |
| `a6c9e25` | Finding 2.9 |
| `57683c5` | `ruff format` on the fork's own test files |
| `4174317` | `FORK_CHANGES.md`: Review 2 fixes recorded |
