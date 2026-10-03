from __future__ import annotations

import logging
import re
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha1
from itertools import chain
from math import floor
from pathlib import Path
from struct import pack
from typing import ClassVar
from warnings import warn

from numbers_parser.bullets import (
    BULLET_CONVERSION,
    BULLET_PREFIXES,
    BULLET_SUFFIXES,
)
from numbers_parser.cell import (
    RGB,
    Alignment,
    Border,
    BorderType,
    Cell,
    CustomFormatting,
    Formatting,
    FormattingType,
    HorizontalJustification,
    MergeAnchor,
    MergedCell,
    MergeReference,
    PaddingType,
    Style,
    VerticalJustification,
    _decode_date_format,
)
from numbers_parser.constants import (
    ALLOWED_FORMATTING_PARAMETERS,
    COLON_TRACT_NODE,
    CUSTOM_FORMAT_TYPE_MAP,
    CUSTOM_TEXT_PLACEHOLDER,
    DEFAULT_COLUMN_WIDTH,
    DEFAULT_DOCUMENT,
    DEFAULT_FONT,
    DEFAULT_PRE_BNC_BYTES,
    DEFAULT_ROW_HEIGHT,
    DEFAULT_TABLE_OFFSET,
    DEFAULT_TEXT_INSET,
    DEFAULT_TEXT_WRAP,
    DEFAULT_TILE_SIZE,
    DOCUMENT_ID,
    EPOCH,
    FONT_FAMILY_DEFAULT,
    FONT_MAP,
    FORMAT_TYPE_MAP,
    MAX_TILE_SIZE,
    PACKAGE_ID,
    CellInteractionType,
    CellValueType,
    FormatType,
    OwnerKind,
)
from numbers_parser.containers import ObjectStore
from numbers_parser.exceptions import UnsupportedError, UnsupportedWarning
from numbers_parser.formula import TableFormulas
from numbers_parser.generated import TNArchives_pb2 as TNArchives
from numbers_parser.generated import TSAArchives_pb2 as TSAArchives
from numbers_parser.generated import TSCEArchives_pb2 as TSCEArchives
from numbers_parser.generated import TSDArchives_pb2 as TSDArchives
from numbers_parser.generated import TSKArchives_pb2 as TSKArchives
from numbers_parser.generated import TSPArchiveMessages_pb2 as TSPArchiveMessages
from numbers_parser.generated import TSPMessages_pb2 as TSPMessages
from numbers_parser.generated import TSSArchives_pb2 as TSSArchives
from numbers_parser.generated import TSTArchives_pb2 as TSTArchives
from numbers_parser.generated import TSWPArchives_pb2 as TSWPArchives
from numbers_parser.generated.TSDArchives_pb2 import (
    StrokePatternArchive as StrokePattern,
)
from numbers_parser.generated.TSWPArchives_pb2 import (
    CharacterStylePropertiesArchive as CharacterStyle,
)
from numbers_parser.iwafile import find_extension
from numbers_parser.numbers_cache import Cacheable, cache
from numbers_parser.numbers_uuid import NumbersUUID, derive_table_identity_uuid, uuid_to_hex
from numbers_parser.xrefs import CellRange, ScopedNameRefCache

logger = logging.getLogger(__name__)
debug = logger.debug

# UTF-16 code unit of U+FFFC, the placeholder an attached object such as a
# page number field occupies in a text storage.
_PLACEHOLDER_UNIT = "\ufffc".encode("utf-16-le")


class MergeCells:
    def __init__(self) -> None:
        self._references = defaultdict(lambda: False)

    def add_reference(self, row: int, col: int, rect: tuple) -> None:
        self._references[(row, col)] = MergeReference(*rect)

    def add_anchor(self, row: int, col: int, size: tuple) -> None:
        self._references[(row, col)] = MergeAnchor(size)

    def is_merge_reference(self, row_col: tuple) -> bool:
        # defaultdict will default this to False for missing entries
        return isinstance(self._references[row_col], MergeReference)

    def is_merge_anchor(self, row_col: tuple) -> bool:
        # defaultdict will default this to False for missing entries
        return isinstance(self._references[row_col], MergeAnchor)

    def get(self, row_col: tuple) -> MergeAnchor | MergeReference:
        return self._references[row_col]

    def size(self, row_col: tuple) -> tuple:
        return self._references[row_col].size

    def rect(self, row_col: tuple) -> tuple:
        return self._references[row_col].rect

    def merge_cells(self):
        return [k for k, v in self._references.items() if self.is_merge_anchor(k)]


class DataLists(Cacheable):
    """Model for TST.DataList with caching and key generation for new values."""

    def __init__(
        self,
        model: object,
        datalist_name: str,
        value_attr: str | None = None,
        fallback_datalist_name: str | None = None,
    ) -> None:
        self._model = model
        self._datalists = {}
        self._value_attr = value_attr
        self._datalist_name = datalist_name
        # Older documents may only populate a legacy DataStore field (e.g. the
        # required format_table_pre_bnc) and leave its newer optional sibling
        # (format_table) unset, so dereferencing the unset reference's default
        # identifier of 0 raises KeyError. fallback_datalist_name names that
        # legacy field; add_table() seeds a real datalist from it rather than
        # reading it in place, because version 5 cell storage resolves its
        # format ids against format_table only (see _create_datalist_from).
        self._fallback_datalist_name = fallback_datalist_name

    def _create_datalist_from(self, base_data_store: object, fallback_id: int) -> int:
        """
        Populate a missing datalist field, seeded from its legacy sibling.

        Cell storage version 5 resolves its format ids against format_table, not
        against format_table_pre_bnc: across this suite's fixtures there are 109
        tables where the two lists hold different keys and the cells always track
        format_table, and none that track the legacy list. Reading the legacy list
        in place would therefore make new entries unresolvable for Numbers, so
        create the field the cells actually index and copy the legacy entries into
        it, preserving their keys so existing references stay valid.
        """
        datalist_id, datalist = self._model.objects.create_object_from_dict(
            "Index/Tables/TableDataList-{}",
            {"listType": TSTArchives.TableDataList.ListType.FORMAT, "nextListID": 1},
            TSTArchives.TableDataList,
        )
        self._model.add_component_metadata(
            datalist_id,
            "CalculationEngine",
            "Tables/TableDataList-{}",
        )
        fallback = self._model.objects[fallback_id]
        datalist.CopyFrom(fallback)
        datalist.listType = TSTArchives.TableDataList.ListType.FORMAT
        self._model.set_reference(getattr(base_data_store, self._datalist_name), datalist_id)
        return datalist_id

    @cache()
    def add_table(self, table_id: int) -> None:
        """Cache a new datalist for a table if not already seen."""
        base_data_store = self._model.objects[table_id].base_data_store
        if (
            not base_data_store.HasField(self._datalist_name)
            and self._fallback_datalist_name is not None
            and base_data_store.HasField(self._fallback_datalist_name)
        ):
            datalist_id = self._create_datalist_from(
                base_data_store,
                getattr(base_data_store, self._fallback_datalist_name).identifier,
            )
        else:
            datalist_id = getattr(base_data_store, self._datalist_name).identifier
        datalist = self._model.objects[datalist_id]

        max_key = 0
        self._datalists[table_id] = {}
        self._datalists[table_id]["by_key"] = {}
        self._datalists[table_id]["by_value"] = {}
        self._datalists[table_id]["key_index"] = {}
        self._datalists[table_id]["datalist"] = datalist
        self._datalists[table_id]["id"] = datalist_id
        for i, entry in enumerate(sorted(datalist.entries, key=lambda x: x.key)):
            if entry.key > max_key:
                max_key = entry.key
                self._datalists[table_id]["by_key"][entry.key] = entry
                self._datalists[table_id]["key_index"][entry.key] = i
                value_key = self.value_key(getattr(entry, self._value_attr))
                self._datalists[table_id]["by_value"][value_key] = entry.key
        self._datalists[table_id]["next_key"] = max_key + 1

    def id(self, table_id: int) -> int:
        self.add_table(table_id)
        return self._datalists[table_id]["id"]

    def lookup_value(self, table_id: int, key: int):
        """Return the an entry in a table's datalist matching a key."""
        self.add_table(table_id)
        return self._datalists[table_id]["by_key"][key]

    def value_key(self, value):
        if hasattr(value, "DESCRIPTOR"):
            return repr(value)
        return value

    def init(self, table_id: int) -> None:
        """Remove all entries from a datalist."""
        self.add_table(table_id)
        self._datalists[table_id]["by_key"] = {}
        self._datalists[table_id]["by_value"] = {}
        self._datalists[table_id]["key_index"] = {}
        self._datalists[table_id]["next_key"] = 1
        self._datalists[table_id]["datalist"].nextListID = 1
        clear_field_container(self._datalists[table_id]["datalist"].entries)

    def lookup_key(self, table_id: int, value) -> int:
        """
        Return the key associated with a value for a particular table entry.
        If the value is not in the datalist, allocate a new entry with the
        next available key.
        """
        self.add_table(table_id)
        value_key = self.value_key(value)
        if value_key not in self._datalists[table_id]["by_value"]:
            key = self._datalists[table_id]["next_key"]
            self._datalists[table_id]["next_key"] += 1
            self._datalists[table_id]["datalist"].nextListID += 1
            attrs = {"key": key, self._value_attr: value, "refcount": 1}
            entry = TSTArchives.TableDataList.ListEntry(**attrs)
            self._datalists[table_id]["datalist"].entries.append(entry)
            self._datalists[table_id]["by_key"][key] = entry
            self._datalists[table_id]["key_index"][key] = (
                len(self._datalists[table_id]["datalist"].entries) - 1
            )
            self._datalists[table_id]["by_value"][value_key] = key
        else:
            value_key = self.value_key(value)
            key = self._datalists[table_id]["by_value"][value_key]
            index = self._datalists[table_id]["key_index"][key]
            self._datalists[table_id]["datalist"].entries[index].refcount += 1

        return key


class _NumbersModel(Cacheable):
    """
    Loads all objects from Numbers document and provides decoding
    methods for other classes in the module to abstract away the
    internal structures of Numbers document data structures.

    Not to be used in application code.
    """

    def __init__(self, filepath: Path, password: str | None) -> None:
        if filepath is None:
            filepath = Path(DEFAULT_DOCUMENT)
        self.objects = ObjectStore(filepath, password)
        self._merge_cells = defaultdict(MergeCells)
        self._row_heights = {}
        self._col_widths = {}
        self._table_formats = DataLists(
            self,
            "format_table",
            "format",
            fallback_datalist_name="format_table_pre_bnc",
        )
        self._table_styles = DataLists(self, "styleTable", "reference")
        self._table_strings = DataLists(self, "stringTable", "string")
        self._control_specs = DataLists(self, "control_cell_spec_table", "cell_spec")
        self._formulas = DataLists(self, "formula_table", "formula")
        self._table_data = {}
        self._table_categories_data = {}
        self._table_categories_row_mapper = {}
        self._styles = None
        self._update_strokes = {}
        self._images = {}
        self._custom_formats = None
        self._custom_format_archives = None
        self._custom_format_ids = None
        self.name_ref_cache = ScopedNameRefCache(self)
        self.missing_fonts = {}
        self.calculate_table_uuid_map()

    def save(self, filepath: Path, package: bool, password: str, hint: str) -> None:
        self.objects.save(filepath, package, password, hint)

    def find_refs(self, ref: str) -> list:
        return self.objects.find_refs(ref)

    def sheet_ids(self):
        return [
            o.identifier
            for o in self.objects[DOCUMENT_ID].sheets
            if isinstance(self.objects[o.identifier], TNArchives.SheetArchive)
        ]

    def sheet_name(self, sheet_id, value=None):
        if value is None:
            if sheet_id not in self.objects:
                return None
            return self.objects[sheet_id].name
        self.objects[sheet_id].name = value
        return None

    def ruler_guides(self, sheet_id: int) -> list:
        """Return the sheet's ruler guides as (axis, position) tuples, in points."""
        sheet_obj = self.objects[sheet_id]
        if not sheet_obj.HasField("userDefinedGuideStorage"):
            return []
        storage_id = sheet_obj.userDefinedGuideStorage.identifier
        storage_obj = self.objects[storage_id]
        return [
            ("vertical" if g.type == 1 else "horizontal", g.position)
            for g in storage_obj.userDefinedGuides
        ]

    def add_ruler_guide(self, sheet_id: int, axis: str, position: float) -> None:
        """
        Add a ruler guide ("horizontal" or "vertical") at position, in points.

        The sheet's GuideStorageArchive is created on the first guide.
        """
        if axis not in ("horizontal", "vertical"):
            msg = "axis must be 'horizontal' or 'vertical'"
            raise ValueError(msg)
        guide_type = 1 if axis == "vertical" else 0

        sheet_obj = self.objects[sheet_id]
        if not sheet_obj.HasField("userDefinedGuideStorage"):
            storage_id, _ = self.objects.create_object_from_dict(
                "Document",
                {"userDefinedGuides": []},
                TSDArchives.GuideStorageArchive,
            )
            sheet_obj.userDefinedGuideStorage.identifier = storage_id
        else:
            storage_id = sheet_obj.userDefinedGuideStorage.identifier

        storage_obj = self.objects[storage_id]
        guide = storage_obj.userDefinedGuides.add()
        guide.type = guide_type
        guide.position = position

    def add_image(
        self,
        sheet_id: int,
        data: bytes,
        filename: str,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> int:
        """
        Add a free-standing image to a sheet at (x, y), sized (width, height),
        all in points. Returns the new image's object ID.

        The file is registered in the package the same way a cell background
        image is (SHA1 deduplication, Data/ file, DataInfo entry). The drawable
        reuses the geometry built for a new table.
        """
        digest = sha1(data).digest()  # noqa: S324
        if digest in self._images:
            image_id = self._images[digest]
        else:
            # Store the file first: it raises if the name is taken, and must
            # do so before the DataInfo and digest are recorded.
            self.store_image(data, filename)
            image_id = self.next_image_identifier()
            self.objects[PACKAGE_ID].datas.append(
                TSPArchiveMessages.DataInfo(
                    identifier=image_id,
                    digest=digest,
                    preferred_file_name=filename,
                    file_name=filename,
                    materialized_length=len(data),
                ),
            )
            self._images[digest] = image_id

        image_info_id, image_info = self.objects.create_object_from_dict(
            "Document",
            {"data": {"identifier": image_id}},
            TSDArchives.ImageArchive,
        )
        image_info.super.MergeFrom(self.create_drawable(sheet_id, x, y, height=height, width=width))

        self.objects[sheet_id].drawable_infos.append(
            TSPMessages.Reference(identifier=image_info_id),
        )
        return image_info_id

    def duplicate_image(self, sheet_id: int, image_id: int, x: float, y: float) -> int:
        """
        Copy an image to (x, y) and return the copy's object ID.

        The copy shares the source's package file and style, and sits directly
        behind the source. Its title, caption and mask are new objects, since
        each image must own them: a title or caption also gets its own text
        storage and placement, and each owned copy's parent is the new image.
        """
        drawables = self.objects[sheet_id].drawable_infos
        position = next((i for i, ref in enumerate(drawables) if ref.identifier == image_id), None)
        if position is None:
            msg = f"no image with id {image_id} on this sheet"
            raise IndexError(msg)

        source = self.objects[image_id]
        copy_id, copy = self.objects.create_object_from_dict("Document", {}, type(source))
        copy.CopyFrom(source)
        for field in ("title", "caption"):
            if copy.super.HasField(field):
                info_ref = getattr(copy.super, field)
                info_ref.identifier, info = self._copy_object(info_ref.identifier)
                _set_drawable_parent(info, copy_id)
                for path in (("super", "owned_storage"), ("placement",)):
                    ref = _set_message_field(info, path)
                    if ref is not None:
                        ref.identifier, _ = self._copy_object(ref.identifier)
        if copy.HasField("mask"):
            copy.mask.identifier, mask = self._copy_object(copy.mask.identifier)
            _set_drawable_parent(mask, copy_id)
        copy.super.geometry.position.x = x
        copy.super.geometry.position.y = y
        drawables.insert(position, TSPMessages.Reference(identifier=copy_id))
        return copy_id

    def _copy_object(self, object_id: int) -> tuple[int, object]:
        """Copy an object into the Document archive and return (new id, new object)."""
        source = self.objects[object_id]
        new_id, new = self.objects.create_object_from_dict("Document", {}, type(source))
        new.CopyFrom(source)
        return new_id, new

    def _header_footer_storage(self, sheet_id: int, kind: str, zone: int):
        refs = getattr(self.objects[sheet_id], kind)
        if not 0 <= zone < len(refs):
            msg = f"{kind[:-1]} zone {zone} does not exist (sheet has {len(refs)})"
            raise IndexError(msg)
        return self.objects[refs[zone].identifier]

    def header_footer_text(self, sheet_id: int, kind: str, zone: int) -> str:
        """Return the text of a page header or footer zone ("headers" or "footers")."""
        storage = self._header_footer_storage(sheet_id, kind, zone)
        return "".join(storage.text)

    def header_footer_char_runs(self, sheet_id: int, kind: str, zone: int) -> list:
        """Return the character style runs of a zone as (character_index, object_id or None)."""
        storage = self._header_footer_storage(sheet_id, kind, zone)
        return [
            (e.character_index, e.object.identifier if e.HasField("object") else None)
            for e in storage.table_char_style.entries
        ]

    def set_header_footer_text(
        self,
        sheet_id: int,
        kind: str,
        zone: int,
        text: str,
        runs: list | None = None,
    ) -> None:
        r"""
        Replace the text of a page header or footer zone.

        Style runs are kept for the part of the text they still cover and
        dropped past the end of the new text, so a shorter text leaves no
        run pointing beyond it. Text after the last kept run takes that run's style.
        If ``runs`` is given, it replaces the character style runs: a list of
        (character_index, object_id or None) as returned by
        :meth:`header_footer_char_runs`, with indices increasing and inside
        the new text. ``runs`` is checked before anything changes.

        An attached object such as a page number field is kept only where the
        new text still has its U+FFFC placeholder at the same index, so
        ``"\ufffc of 3"`` keeps a field at index 0 and ``"Draft"`` drops it.
        """
        storage = self._header_footer_storage(sheet_id, kind, zone)
        units = text.encode("utf-16-le")
        length = len(units) // 2
        if runs is not None:
            _check_char_runs(runs, length)
        del storage.text[:]
        storage.text.append(text)
        for name in (
            "table_para_style",
            "table_para_data",
            "table_list_style",
            "table_char_style",
            "table_para_starts",
            "table_language",
            "table_para_bidi",
        ):
            entries = getattr(storage, name).entries
            keep = [e for e in entries if e.character_index == 0 or e.character_index < length]
            if len(keep) != len(entries):
                kept = [type(e).FromString(e.SerializeToString()) for e in keep]
                del entries[:]
                entries.extend(kept)
        for name in ("table_attachment", "table_footnote"):
            entries = getattr(storage, name).entries
            keep = [
                e
                for e in entries
                if e.character_index < length
                and units[2 * e.character_index : 2 * e.character_index + 2] == _PLACEHOLDER_UNIT
            ]
            if len(keep) != len(entries):
                kept = [type(e).FromString(e.SerializeToString()) for e in keep]
                del entries[:]
                entries.extend(kept)
        if runs is not None:
            entries = storage.table_char_style.entries
            del entries[:]
            for index, object_id in runs:
                entry = entries.add()
                entry.character_index = index
                if object_id is not None:
                    entry.object.identifier = object_id

    def image_ids(self, sheet_id: int) -> list[int]:
        """Return the object IDs of the free-standing images on a sheet, back to front."""
        return [
            ref.identifier
            for ref in self.objects[sheet_id].drawable_infos
            if isinstance(self.objects[ref.identifier], TSDArchives.ImageArchive)
        ]

    def image_geometry(self, image_id: int):
        """Return the GeometryArchive of an image."""
        return self.objects[image_id].super.geometry

    def image_data(self, image_id: int) -> tuple[bytes, str]:
        """Return (data, filename) for an image, or (None, None) if its file is missing."""
        data_id = self.objects[image_id].data.identifier
        datas = self.objects[PACKAGE_ID].datas
        info = next((x for x in datas if x.identifier == data_id), None)
        if info is None:
            return None, None
        data = self.objects.file_store.get(f"Data/{info.file_name}")
        return data, info.preferred_file_name or info.file_name

    def remove_image(self, sheet_id: int, image_id: int) -> None:
        """
        Remove an image from a sheet.

        The package file and its DataInfo stay, since another image can share
        them through SHA1 deduplication.
        """
        drawables = self.objects[sheet_id].drawable_infos
        for i, ref in enumerate(drawables):
            if ref.identifier == image_id:
                del drawables[i]
                return
        msg = f"no image with id {image_id} on this sheet"
        raise IndexError(msg)

    def set_table_data(self, table_id: int, data: list) -> None:
        self._table_data[table_id] = data

    # Don't cache: new tables can be added at runtime
    def table_ids(self, sheet_id: int | None = None) -> list:
        """
        Return a list of table IDs for a given sheet ID or all table
        IDs id the sheet ID is None
        """
        table_info_ids = self.find_refs("TableInfoArchive")
        return [
            self.objects[t_id].tableModel.identifier
            for t_id in table_info_ids
            if (sheet_id is None or self.objects[t_id].super.parent.identifier == sheet_id)
        ]

    # Don't cache: new tables can be added at runtime
    def paper_size(self) -> tuple[str, float, float]:
        """Return (paper id, portrait width, portrait height) in points."""
        doc = self.objects[DOCUMENT_ID]
        return doc.paper_id, doc.page_size.width, doc.page_size.height

    def set_paper_size(self, paper_id: str, width: float, height: float) -> None:
        doc = self.objects[DOCUMENT_ID]
        doc.paper_id = paper_id
        doc.page_size.width = width
        doc.page_size.height = height

    def sheet_is_portrait(self, sheet_id: int) -> bool:
        return self.objects[sheet_id].in_portrait_page_orientation

    def set_sheet_portrait(self, sheet_id: int, portrait: bool) -> None:
        self.objects[sheet_id].in_portrait_page_orientation = portrait

    def table_locked(self, table_id: int) -> bool:
        """Return True if the table is locked against moving, resizing and editing in Numbers."""
        return self.objects[self.table_info_id(table_id)].super.locked

    def set_table_locked(self, table_id: int, locked: bool) -> None:
        self.objects[self.table_info_id(table_id)].super.locked = locked

    def table_info_id(self, table_id: int) -> int:
        """Return the TableInfoArchive ID for a given table ID."""
        ids = [
            x
            for x in self.objects.find_refs("TableInfoArchive")
            if self.objects[x].tableModel.identifier == table_id
        ]
        return ids[0]

    def number_of_rows(self, table_id, num_rows=None):
        if num_rows is not None:
            self.objects[table_id].number_of_rows = num_rows
        return self.objects[table_id].number_of_rows

    def number_of_columns(self, table_id, num_cols=None):
        if num_cols is not None:
            self.objects[table_id].number_of_columns = num_cols
        return self.objects[table_id].number_of_columns

    def table_name(self, table_id, value=None):
        if value is None:
            return self.objects[table_id].table_name
        self.objects[table_id].table_name = value
        return None

    def table_names(self):
        return list(
            chain.from_iterable(
                [[self.table_name(tid) for tid in self.table_ids(sid)] for sid in self.sheet_ids()],
            ),
        )

    def table_name_enabled(self, table_id: int, enabled: bool | None = None):
        if enabled is not None:
            self.objects[table_id].table_name_enabled = enabled
            return None
        return self.objects[table_id].table_name_enabled

    def clear_ruler_guides(self, sheet_id: int) -> None:
        """Remove every ruler guide from the sheet."""
        sheet_obj = self.objects[sheet_id]
        if sheet_obj.HasField("userDefinedGuideStorage"):
            storage_obj = self.objects[sheet_obj.userDefinedGuideStorage.identifier]
            del storage_obj.userDefinedGuides[:]

    def table_name_height(self, table_id: int) -> float:
        """Return the table's name banner height in points (0.0 if never rendered)."""
        return self.objects[table_id].table_name_height

    def caption_enabled(self, table_id: int, enabled: bool | None = None) -> bool:
        table_info = self.objects[self.table_info_id(table_id)]
        if enabled is not None:
            table_info.super.caption_hidden = not enabled
            return None
        caption_info_id = table_info.super.caption.identifier
        caption_archive = self.objects[caption_info_id]
        if caption_archive.DESCRIPTOR.name == "StandinCaptionArchive":
            return False
        return not table_info.super.caption_hidden

    def find_style_id(self, style_substr: str):
        stylesheet = self.objects[self.stylesheet_id()]
        caption_styles = [
            x for x in stylesheet.identifier_to_style_map if style_substr in x.identifier.lower()
        ]
        return caption_styles[0].style.identifier

    def caption_paragraph_style_id(self):
        style_map = {
            x: self.objects[x]
            for x in self.find_refs("ParagraphStyleArchive")
            if "Caption" in self.objects[x].super.name
        }
        return next(iter(style_map.keys()))

    @cache(num_args=0)
    def stylesheet_id(self):
        return self.find_refs("StylesheetArchive")[0]

    def set_reference(self, obj: object, ref_id: int) -> None:
        obj.MergeFrom(TSPMessages.Reference(identifier=ref_id))

    def create_path_source_archive(self, table_id):
        box_size = 100.0
        return TSDArchives.PathSourceArchive(
            horizontalFlip=False,
            verticalFlip=False,
            bezier_path_source=TSDArchives.BezierPathSourceArchive(
                naturalSize=TSPMessages.Size(width=self.table_width(table_id), height=0.0),
                path=TSPMessages.Path(
                    elements=[
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.moveTo,
                            points=[TSPMessages.Point(x=0.0, y=0.0)],
                        ),
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.lineTo,
                            points=[TSPMessages.Point(x=box_size, y=0.0)],
                        ),
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.lineTo,
                            points=[TSPMessages.Point(x=box_size, y=box_size)],
                        ),
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.lineTo,
                            points=[TSPMessages.Point(x=0.0, y=box_size)],
                        ),
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.closeSubpath,
                        ),
                        TSPMessages.Path.Element(
                            type=TSPMessages.Path.ElementType.moveTo,
                            points=[TSPMessages.Point(x=0.0, y=0.0)],
                        ),
                    ],
                ),
            ),
        )

    def create_caption_archive(self, table_id) -> None:
        table_info_id = self.table_info_id(table_id)
        table_info = self.objects[table_info_id]
        caption_placement_id, _ = self.objects.create_object_from_dict(
            "CalculationEngine",
            {
                "caption_anchor_location": 1,  # UPPER_LEFT
                "drawable_anchor_location": 7,  # LOWER_LEFT
            },
            TSAArchives.CaptionPlacementArchive,
        )
        caption_info_id, caption_info = self.objects.create_object_from_dict(
            "CalculationEngine",
            {"childInfoKind": "Caption", "placement": {"identifier": caption_placement_id}},
            TSAArchives.CaptionInfoArchive,
        )
        storage_id, _ = self.objects.create_object_from_dict(
            "CalculationEngine",
            {
                "text": ["Caption"],
                "in_document": True,
                "style_sheet": {"identifier": self.stylesheet_id()},
                "table_para_style": {
                    "entries": [
                        {
                            "character_index": 0,
                            "object": {"identifier": self.caption_paragraph_style_id()},
                        },
                    ],
                },
                "table_list_style": {
                    "entries": [
                        {
                            "character_index": 0,
                            "object": {"identifier": self.find_style_id("liststyle")},
                        },
                    ],
                },
                "table_para_starts": {"entries": [{"character_index": 0, "first": 0, "second": 0}]},
                "table_para_bidi": {"entries": [{"character_index": 0, "first": 0, "second": 0}]},
                "table_drop_cap_style": {"entries": [{"character_index": 0}]},
            },
            TSWPArchives.StorageArchive,
        )
        for object_id in [
            storage_id,
            self.find_style_id("caption"),
            self.find_style_id("liststyle"),
            self.caption_paragraph_style_id(),
        ]:
            self.add_component_reference(
                object_id,
                location="CalculationEngine",
                component_id=self.stylesheet_id(),
            )
        caption_info.super.MergeFrom(
            TSWPArchives.ShapeInfoArchive(
                is_text_box=True,
                owned_storage=TSPMessages.Reference(identifier=storage_id),
                deprecated_storage=TSPMessages.Reference(identifier=storage_id),
                super=TSDArchives.ShapeArchive(
                    super=self.create_drawable(table_info_id, 0, 500, flags=1),
                    style={"identifier": self.find_style_id("caption")},
                    strokePatternOffsetDistance=0.0,
                    pathsource=self.create_path_source_archive(table_id),
                ),
            ),
        )

        self.set_reference(table_info.super.caption, caption_info_id)
        component = self.metadata_component(self.calc_engine_id())
        component.object_uuid_map_entries.append(
            TSPArchiveMessages.ObjectUUIDMapEntry(
                identifier=caption_info_id,
                uuid=NumbersUUID().protobuf2,
            ),
        )

    def caption_text(self, table_id: int, caption: str | None = None) -> str:
        table_info = self.objects[self.table_info_id(table_id)]
        caption_info_id = table_info.super.caption.identifier
        caption_archive = self.objects[caption_info_id]

        if caption_archive.DESCRIPTOR.name == "StandinCaptionArchive":
            if caption is None:
                return "Caption"
            self.create_caption_archive(table_id)
            caption_info_id = table_info.super.caption.identifier
            caption_archive = self.objects[caption_info_id]

        caption_storage_id = caption_archive.super.owned_storage.identifier
        if caption is not None:
            clear_field_container(self.objects[caption_storage_id].text)
            self.objects[caption_storage_id].text.append(caption)
            return None
        if len(self.objects[caption_storage_id].text) == 0:
            return "Caption"
        return self.objects[caption_storage_id].text[0]

    @cache()
    def table_tiles(self, table_id):
        bds = self.objects[table_id].base_data_store
        return [self.objects[t.tile.identifier] for t in bds.tiles.tiles]

    def custom_format_list_archive(self) -> object:
        """
        Return the document's TSK.CustomFormatListArchive, creating an empty one
        if the document predates the field. Older documents leave
        document.super.custom_format_list unset, so reading its identifier gives
        the proto default of 0 and self.objects[0] raises KeyError. New documents
        store this archive alongside the document in Index/Document.iwa.
        """
        document_super = self.objects[DOCUMENT_ID].super
        if not document_super.HasField("custom_format_list"):
            list_id, _ = self.objects.create_object_from_dict(
                "Index/Document",
                {},
                TSKArchives.CustomFormatListArchive,
            )
            self.set_reference(document_super.custom_format_list, list_id)
        return self.objects[document_super.custom_format_list.identifier]

    @cache(num_args=0)
    def custom_format_map(self):
        custom_format_list = self.custom_format_list_archive()
        return {
            NumbersUUID(u).hex: custom_format_list.custom_formats[i]
            for i, u in enumerate(custom_format_list.uuids)
        }

    @cache(num_args=2)
    def table_format(self, table_id: int, key: int) -> str:
        """Return the format associated with a format ID for a particular table."""
        return self._table_formats.lookup_value(table_id, key).format

    @cache(num_args=3)
    def format_archive(self, table_id: int, format_type: FormattingType, formatting: Formatting):
        """Create a table format from a Formatting spec and return the table format ID."""
        attrs = {x: getattr(formatting, x) for x in ALLOWED_FORMATTING_PARAMETERS[format_type]}
        attrs["format_type"] = FORMAT_TYPE_MAP[format_type]

        format_archive = TSKArchives.FormatStructArchive(**attrs)
        return self._table_formats.lookup_key(table_id, format_archive)

    def cell_popup_model(self, parent_id: int, formatting: Formatting):
        tsce_items = [{"cell_value_type": "NIL_TYPE"}]
        for item in formatting.popup_values:
            if isinstance(item, str):
                tsce_items.append(
                    {
                        "cell_value_type": "STRING_TYPE",
                        "string_value": {
                            "value": item,
                            "format": {"format_type": FormatType.TEXT},
                        },
                    },
                )
            else:
                tsce_items.append(
                    {
                        "cell_value_type": "NUMBER_TYPE",
                        "number_value": {
                            "value": item,
                            "format": {"format_type": FormatType.DECIMAL},
                        },
                    },
                )
        popup_menu_id, _ = self.objects.create_object_from_dict(
            f"Index/Tables/DataList-{parent_id}",
            {"tsce_item": tsce_items},
            TSTArchives.PopUpMenuModel,
            True,
        )
        return popup_menu_id

    def control_cell_archive(
        self,
        table_id: int,
        format_type: FormattingType,
        formatting: Formatting,
    ):
        """Create control cell archive from a Formatting spec and return the table format ID."""
        if format_type == FormattingType.TICKBOX:
            cell_spec = TSTArchives.CellSpecArchive(interaction_type=CellInteractionType.TOGGLE)
        elif format_type == FormattingType.RATING:
            cell_spec = TSTArchives.CellSpecArchive(
                interaction_type=CellInteractionType.RATING,
                range_control_min=0.0,
                range_control_max=5.0,
                range_control_inc=1.0,
            )
        elif format_type == FormattingType.SLIDER:
            cell_spec = TSTArchives.CellSpecArchive(
                interaction_type=CellInteractionType.SLIDER,
                range_control_min=formatting.minimum,
                range_control_max=formatting.maximum,
                range_control_inc=formatting.increment,
            )
        else:  # POPUP
            popup_id = self.cell_popup_model(self._control_specs.id(table_id), formatting)
            cell_spec = TSTArchives.CellSpecArchive(
                interaction_type=CellInteractionType.POPUP,
                chooser_control_popup_model=TSPMessages.Reference(identifier=popup_id),
                chooser_control_start_w_first=not (formatting.allow_none),
            )
        return self._control_specs.lookup_key(table_id, cell_spec)

    def add_custom_decimal_format_archive(self, formatting: CustomFormatting) -> None:
        """Create a custom format from the format spec."""
        integer_format = formatting.integer_format
        decimal_format = formatting.decimal_format
        num_integers = formatting.num_integers
        num_decimals = formatting.num_decimals
        show_thousands_separator = formatting.show_thousands_separator

        if num_integers == 0:
            format_string = ""
        elif integer_format == PaddingType.NONE:
            format_string = "#" * num_integers
        else:
            format_string = "0" * num_integers
        if num_integers > 6:
            format_string = re.sub(r"(...)(...)$", r",\1,\2", format_string)
        elif num_integers > 3:
            format_string = re.sub(r"(...)$", r",\1", format_string)
        if num_decimals > 0:
            if decimal_format == PaddingType.NONE:
                format_string += "." + "#" * num_decimals
            else:
                format_string += "." + "0" * num_decimals

        min_integer_width = (
            num_integers if num_integers > 0 and integer_format != PaddingType.NONE else 0
        )
        num_nonspace_decimal_digits = num_decimals if decimal_format == PaddingType.ZEROS else 0
        num_nonspace_integer_digits = num_integers if integer_format == PaddingType.ZEROS else 0
        index_from_right_last_integer = num_decimals + 1 if num_integers > 0 else num_decimals
        # Empirically correct:
        if index_from_right_last_integer == 1:
            index_from_right_last_integer = 0
        elif index_from_right_last_integer == 0:
            index_from_right_last_integer = 1
        decimal_width = num_decimals if decimal_format == PaddingType.SPACES else 0
        is_complex = "0" in format_string and (
            min_integer_width > 0 or num_nonspace_decimal_digits == 0
        )

        format_archive = TSKArchives.CustomFormatArchive(
            name=formatting.name,
            format_type_pre_bnc=FormatType.CUSTOM_NUMBER,
            format_type=FormatType.CUSTOM_NUMBER,
            default_format=TSKArchives.FormatStructArchive(
                contains_integer_token=num_integers > 0,
                custom_format_string=format_string,
                decimal_width=decimal_width,
                format_type=FormatType.CUSTOM_NUMBER,
                fraction_accuracy=0xFFFFFFFD,
                index_from_right_last_integer=index_from_right_last_integer,
                is_complex=is_complex,
                min_integer_width=min_integer_width,
                num_hash_decimal_digits=0,
                num_nonspace_decimal_digits=num_nonspace_decimal_digits,
                num_nonspace_integer_digits=num_nonspace_integer_digits,
                requires_fraction_replacement=False,
                scale_factor=1.0,
                show_thousands_separator=show_thousands_separator and num_integers > 0,
                total_num_decimal_digits=decimal_width,
                use_accounting_style=False,
            ),
        )
        self.add_custom_format_archive(formatting, format_archive)

    def add_custom_datetime_format_archive(self, formatting: CustomFormatting) -> None:
        format_archive = TSKArchives.CustomFormatArchive(
            name=formatting.name,
            format_type_pre_bnc=FormatType.CUSTOM_DATE,
            format_type=FormatType.CUSTOM_DATE,
            default_format=TSKArchives.FormatStructArchive(
                custom_format_string=formatting.format,
                format_type=FormatType.CUSTOM_DATE,
            ),
        )
        self.add_custom_format_archive(formatting, format_archive)

    def add_custom_format_archive(
        self,
        formatting: CustomFormatting,
        format_archive: object,
    ) -> None:
        format_uuid = NumbersUUID().protobuf2
        self._custom_formats[formatting.name] = formatting
        self._custom_format_archives[formatting.name] = format_archive
        self._custom_format_uuids[formatting.name] = format_uuid

        custom_format_list = self.custom_format_list_archive()
        custom_format_list.custom_formats.append(format_archive)
        custom_format_list.uuids.append(format_uuid)

    def custom_format_id(self, table_id: int, formatting: CustomFormatting) -> int:
        """Look up the custom format and return the format ID for the table."""
        format_type = CUSTOM_FORMAT_TYPE_MAP[formatting.type]
        format_uuid = self._custom_format_uuids[formatting.name]
        custom_format = TSKArchives.FormatStructArchive(
            format_type=format_type,
            custom_uid=TSPMessages.UUID(lower=format_uuid.lower, upper=format_uuid.upper),
        )
        return self._table_formats.lookup_key(table_id, custom_format)

    def add_custom_text_format_archive(self, formatting: CustomFormatting) -> None:
        format_string = formatting.format.replace("%s", CUSTOM_TEXT_PLACEHOLDER)
        format_archive = TSKArchives.CustomFormatArchive(
            name=formatting.name,
            format_type_pre_bnc=FormatType.CUSTOM_TEXT,
            format_type=FormatType.CUSTOM_TEXT,
            default_format=TSKArchives.FormatStructArchive(
                custom_format_string=format_string,
                format_type=FormatType.CUSTOM_TEXT,
            ),
        )
        self.add_custom_format_archive(formatting, format_archive)

    @cache(num_args=2)
    def table_style(self, table_id: int, key: int) -> str:
        """Return the style associated with a style ID for a particular table."""
        style_entry = self._table_styles.lookup_value(table_id, key)
        return self.objects[style_entry.reference.identifier]

    @cache(num_args=2)
    def table_string(self, table_id: int, key: int) -> str:
        """Return the string associated with a string ID for a particular table."""
        try:
            return self._table_strings.lookup_value(table_id, key).string
        except KeyError:
            return ""

    def init_table_strings(self, table_id: int) -> None:
        """Cache table strings reference and delete all existing keys/values."""
        self._table_strings.init(table_id)

    def table_string_key(self, table_id: int, value: str) -> int:
        """
        Return the key associated with a string for a particular table. If
        the string is not in the strings table, allocate a new entry with the
        next available key.
        """
        return self._table_strings.lookup_key(table_id, value)

    @cache(num_args=0)
    def owner_id_map(self):
        """
        "
        Extracts the mapping table from Owner IDs to UUIDs. Returns a
        dictionary mapping the owner ID int to a 128-bit UUID.
        """
        # The TSCE.CalculationEngineArchive contains a list of mapping entries
        # in dependencyTracker.formulaOwnerDependencies from the root level
        # of the protobuf. Each mapping contains a 32-bit style UUID:
        #
        # "owner_id_map": {
        #     "map_entry": [
        #     {
        #         "internal_ownerId": 33,
        #         "owner_id": 0x3cb03f23_c26dda92_1e4bfcc0_8750e563
        #     },
        #
        #
        calc_engine = self.calc_engine()
        if calc_engine is None:
            return {}

        owner_id_map = {}
        for e in calc_engine.dependency_tracker.owner_id_map.map_entry:
            owner_id_map[e.internal_owner_id] = NumbersUUID(e.owner_id).hex
        return owner_id_map

    def calculate_table_uuid_map(self) -> None:
        # Each Table Model has a UUID which is used in references to the table. See
        # Numbers.md#uuid-mapping for more details.

        self._calculate_extra_owner_uuids()

        # For haunted owner archive types, map formula_owner_uids to their base_owner_uids
        haunted_owner_ids = [
            obj_id
            for obj_id in self.find_refs("FormulaOwnerDependenciesArchive")
            if self.objects[obj_id].owner_kind == OwnerKind.HAUNTED_OWNER
        ]
        if len(haunted_owner_ids) == 0:
            # Some older documents (see issue-18) do not use FormulaOwnerDependenciesArchive
            self._table_id_to_base_id = {}
            return

        formula_owner_to_base_owner_map = {
            uuid_to_hex(self.objects[obj_id].formula_owner_uid): uuid_to_hex(
                self.objects[obj_id].base_owner_uid,
            )
            for obj_id in haunted_owner_ids
        }

        # Map table IDs to the base_owner_uids of the formula owners that match
        # the table model's haunted owner
        self._table_id_to_base_id = {
            table_id: formula_owner_to_base_owner_map.get(
                uuid_to_hex(self.objects[table_id].haunted_owner.owner_uid),
            )
            for table_id in self.table_ids()
        }
        self._table_base_id_to_formula_owner_id = {
            uuid_to_hex(self.objects[obj_id].base_owner_uid): obj_id for obj_id in haunted_owner_ids
        }

    def _calculate_extra_owner_uuids(self) -> None:
        # In addition to the haunted-owner base UUID, some cross-table
        # references (observed empirically against real Numbers.app output)
        # are keyed by a *different*, auxiliary TABLE_MODEL owner's own
        # formula_owner_uid rather than the haunted-owner base UUID. This
        # auxiliary owner shares the same underlying table (via its
        # formula_owner reference to that table's TableInfoArchive) but
        # lives in an unrelated UUID family. Index every such UUID per
        # table so table_uuids_to_id() can resolve either form.
        self._table_id_to_extra_owner_uuids = {}
        table_model_owner_ids = [
            obj_id
            for obj_id in self.find_refs("FormulaOwnerDependenciesArchive")
            if self.objects[obj_id].owner_kind == OwnerKind.TABLE_MODEL
        ]
        for obj_id in table_model_owner_ids:
            fod = self.objects[obj_id]
            if not fod.HasField("formula_owner"):
                continue
            owner_ref_id = fod.formula_owner.identifier
            if owner_ref_id not in self.objects:
                continue
            owner_obj = self.objects[owner_ref_id]
            if not owner_obj.HasField("tableModel"):
                # Only a TableInfoArchive-wrapped owner is a real,
                # observed shape (see add_formula_owner()'s own
                # TABLE_MODEL-kind owner, which always points at a
                # TableInfoArchive) -- nothing else resolves to a table.
                continue
            resolved_table_id = owner_obj.tableModel.identifier
            self._table_id_to_extra_owner_uuids.setdefault(resolved_table_id, set()).add(
                uuid_to_hex(fod.formula_owner_uid),
            )

    @cache()
    def table_base_id(self, table_id: int) -> int:
        """ "Finds the UUID of a table."""
        # Table can be empty if the document does not use FormulaOwnerDependenciesArchive
        return self._table_id_to_base_id.get(table_id)

    @cache(num_args=0)
    def calc_engine_id(self):
        """Return the CalculationEngine ID for the current document."""
        ce_id = self.find_refs("CalculationEngineArchive")
        if len(ce_id) == 0:
            return 0
        return ce_id[0]

    @cache(num_args=0)
    def calc_engine(self):
        """Return the CalculationEngine object for the current document."""
        ce_id = self.calc_engine_id()
        if ce_id == 0:
            return None
        return self.objects[ce_id]

    def add_merge_range(self, table_id, row_start, row_end, col_start, col_end) -> None:
        debug(
            "Add merge: table_id=%d, [%d,%d]->[%d,%d]",
            table_id,
            row_start,
            row_end,
            col_start,
            col_end,
        )
        size = (row_end - row_start + 1, col_end - col_start + 1)
        for row in range(row_start, row_end + 1):
            for col in range(col_start, col_end + 1):
                self._merge_cells[table_id].add_reference(
                    row,
                    col,
                    (row_start, col_start, row_end, col_end),
                )
        self._merge_cells[table_id].add_anchor(row_start, col_start, size)

    @cache()
    def calculate_merges_using_formula_stores(self, table_id) -> int:
        def range_end(archive: object) -> int:
            # range_end is optional
            return archive.range_end if archive.HasField("range_end") else archive.range_begin

        table_model = self.objects[table_id]
        formulas = table_model.merge_owner.formula_store.formulas
        if len(formulas) == 0:
            debug("table=%s: no formula store merges", self.table_name(table_id))
            return 0

        merge_count = 0
        for formula in formulas:
            node = formula.formula.AST_node_array.AST_node[0]
            if node.AST_node_type != COLON_TRACT_NODE:
                continue
            merge_count += 1
            self.add_merge_range(
                table_id,
                node.AST_colon_tract.absolute_row[0].range_begin,
                range_end(node.AST_colon_tract.absolute_row[0]),
                node.AST_colon_tract.absolute_column[0].range_begin,
                range_end(node.AST_colon_tract.absolute_column[0]),
            )
        debug("table=%s: %d formula store merges found", self.table_name(table_id), merge_count)
        return merge_count

    @cache()
    def calculate_merges_using_dependency_archives(self, table_id) -> int:
        """Extract all the merge cell ranges for the Table."""
        # See details in Numbers.md#merge-ranges.
        owner_id_map = self.owner_id_map()
        table_base_id = self.table_base_id(table_id)

        formula_table_ids = self.find_refs("FormulaOwnerDependenciesArchive")
        merge_count = 0
        for formula_id in formula_table_ids:
            dependencies = self.objects[formula_id]
            if dependencies.owner_kind != OwnerKind.MERGE_OWNER:
                continue
            for record in dependencies.range_dependencies.back_dependency:
                to_owner_id = record.internal_range_reference.owner_id
                if owner_id_map[to_owner_id] == table_base_id:
                    record_range = record.internal_range_reference.range
                    merge_count += 1
                    self.add_merge_range(
                        table_id,
                        record_range.top_left_row,
                        record_range.bottom_right_row,
                        record_range.top_left_column,
                        record_range.bottom_right_column,
                    )
        debug(
            "table=%s: %d dependency archive merges found",
            self.table_name(table_id),
            merge_count,
        )
        return merge_count

    @cache()
    def calculate_merges_using_region_map(self, table_id) -> None:
        base_data_store = self.objects[table_id].base_data_store
        if base_data_store.merge_region_map.identifier == 0:
            debug("table=%s: no merge_region_map", self.table_name(table_id))
            return

        merge_count = 0
        cell_ranges = self.objects[base_data_store.merge_region_map.identifier]
        for cell_range in cell_ranges.cell_range:
            (col_start, row_start) = (
                cell_range.origin.packedData >> 16,
                cell_range.origin.packedData & 0xFFFF,
            )
            (num_columns, num_rows) = (
                cell_range.size.packedData >> 16,
                cell_range.size.packedData & 0xFFFF,
            )
            row_end = row_start + num_rows - 1
            col_end = col_start + num_columns - 1
            merge_count += 1
            self.add_merge_range(table_id, row_start, row_end, col_start, col_end)
        debug(
            "table=%s: %d merge_region_map merges found",
            self.table_name(table_id),
            merge_count,
        )

    def merge_cells(self, table_id):
        if self.calculate_merges_using_formula_stores(table_id) > 0:
            return self._merge_cells[table_id]
        if self.calculate_merges_using_dependency_archives(table_id) > 0:
            return self._merge_cells[table_id]
        self.calculate_merges_using_region_map(table_id)
        return self._merge_cells[table_id]

    def table_id_to_sheet_id(self, table_id: int) -> int:
        return next(
            (sheet_id for sheet_id in self.sheet_ids() if table_id in self.table_ids(sheet_id)),
            None,
        )

    @cache()
    def table_uuids_to_id(self, table_uuid) -> int | None:
        return next(
            (
                table_id
                for sheet_id in self.sheet_ids()
                for table_id in self.table_ids(sheet_id)
                if table_uuid == self.table_base_id(table_id)
                or table_uuid in self._table_id_to_extra_owner_uuids.get(table_id, set())
            ),
            None,
        )

    def node_to_ref(self, table_id: int, row: int, col: int, node):
        def resolve_range(is_absolute, absolute_list, relative_list, offset, max_val):
            if is_absolute:
                return absolute_list[0].range_begin
            if not relative_list and absolute_list[0].range_begin == max_val:
                return max_val
            return offset + relative_list[0].range_begin

        def resolve_range_end(is_absolute, absolute_list, relative_list, offset, max_val):
            if is_absolute:
                return range_end(absolute_list[0])
            if not relative_list and range_end(absolute_list[0]) == max_val:
                return max_val
            return offset + range_end(relative_list[0])

        if node.HasField("AST_cross_table_reference_extra_info"):
            table_uuid = NumbersUUID(node.AST_cross_table_reference_extra_info.table_id).hex
            to_table_id = self.table_uuids_to_id(table_uuid)
        else:
            to_table_id = None

        if node.HasField("AST_colon_tract"):
            row_begin = resolve_range(
                node.AST_sticky_bits.begin_row_is_absolute,
                node.AST_colon_tract.absolute_row,
                node.AST_colon_tract.relative_row,
                row,
                0x7FFFFFFF,
            )

            row_end = resolve_range_end(
                node.AST_sticky_bits.end_row_is_absolute,
                node.AST_colon_tract.absolute_row,
                node.AST_colon_tract.relative_row,
                row,
                0x7FFFFFFF,
            )

            col_begin = resolve_range(
                node.AST_sticky_bits.begin_column_is_absolute,
                node.AST_colon_tract.absolute_column,
                node.AST_colon_tract.relative_column,
                col,
                0x7FFF,
            )

            col_end = resolve_range_end(
                node.AST_sticky_bits.end_column_is_absolute,
                node.AST_colon_tract.absolute_column,
                node.AST_colon_tract.relative_column,
                col,
                0x7FFF,
            )

            return CellRange(
                model=self,
                row_start=None if row_begin == 0x7FFFFFFF else row_begin,
                row_end=None if row_end == 0x7FFFFFFF else row_end,
                col_start=None if col_begin == 0x7FFF else col_begin,
                col_end=None if col_end == 0x7FFF else col_end,
                row_start_is_abs=node.AST_sticky_bits.begin_row_is_absolute,
                row_end_is_abs=node.AST_sticky_bits.end_row_is_absolute,
                col_start_is_abs=node.AST_sticky_bits.begin_column_is_absolute,
                col_end_is_abs=node.AST_sticky_bits.end_column_is_absolute,
                from_table_id=table_id,
                to_table_id=to_table_id,
            )

        row = node.AST_row.row if node.AST_row.absolute else row + node.AST_row.row
        col = node.AST_column.column if node.AST_column.absolute else col + node.AST_column.column
        if node.HasField("AST_row") and not node.HasField("AST_column"):
            return CellRange(
                model=self,
                row_start=row,
                row_start_is_abs=node.AST_row.absolute,
                from_table_id=table_id,
                to_table_id=to_table_id,
            )

        if node.HasField("AST_column") and not node.HasField("AST_row"):
            return CellRange(
                model=self,
                col_start=col,
                col_start_is_abs=node.AST_column.absolute,
                from_table_id=table_id,
                to_table_id=to_table_id,
            )

        return CellRange(
            model=self,
            row_start=row,
            col_start=col,
            row_start_is_abs=node.AST_row.absolute,
            col_start_is_abs=node.AST_column.absolute,
            from_table_id=table_id,
            to_table_id=to_table_id,
        )

    @cache()
    def formula_ast(self, table_id: int):
        bds = self.objects[table_id].base_data_store
        formula_table_id = bds.formula_table.identifier
        formula_table = self.objects[formula_table_id]
        formulas = {}
        for formula in formula_table.entries:
            formulas[formula.key] = formula.formula.AST_node_array.AST_node
        return formulas

    @cache()
    def storage_buffers(self, table_id: int) -> dict:
        """
        Return the table's per-row storage buffers, keyed by each row's own
        absolute row index.

        Each row's true position is derived directly from which tile its
        rowInfo lives in (that tile's own ``tileid``) plus the rowInfo's own
        ``tile_row_index`` -- never from ``rowHeaders.buckets``, a separate
        bookkeeping structure this library keeps in lockstep with tile
        storage on every write of its own, but which a real Numbers.app
        resave is free to leave stale: Numbers.app can drop the storage for
        entirely-blank rows (and even an entirely-blank tile) as a space
        optimisation on save without correspondingly updating
        ``rowHeaders.buckets`` to match. Relying on ``rowHeaders.buckets``'s
        own count to work out "which flattened position is row N" then
        silently misattributes every row from that point on to the wrong
        stored content -- confirmed directly against a real Numbers.app
        round trip of a large, sparse table (commit f002f4d);
        ``tests/test_row_storage_desync.py`` reproduces the fault.
        Deriving the row from the rowInfo itself sidesteps the desync
        entirely, since a rowInfo's own tile position can't go stale the
        way a second, separate structure can.

        Reads ``bds.tiles.tiles`` directly rather than via
        ``table_tiles()``, so this stays self-contained instead of coupling
        the live tile-reference list to a separately cached object list
        derived from it.
        """
        bds = self.objects[table_id].base_data_store
        tile_size = bds.tiles.tile_size or DEFAULT_TILE_SIZE
        buffers = {}
        for tile_ref in bds.tiles.tiles:
            tile = self.objects[tile_ref.tile.identifier]
            if not tile.last_saved_in_BNC:
                msg = "Pre-BNC storage is unsupported"
                raise UnsupportedError(msg)
            tile_base_row = tile_ref.tileid * tile_size
            for r in tile.rowInfos:
                row = tile_base_row + r.tile_row_index
                if row in buffers:
                    warn(
                        f"Table {table_id}: row {row} has more than one "
                        "stored rowInfo (corrupt file?); keeping the last "
                        "one found",
                        UnsupportedWarning,
                        stacklevel=2,
                    )
                buffers[row] = get_storage_buffers_for_row(
                    r.cell_storage_buffer,
                    r.cell_offsets,
                    self.number_of_columns(table_id),
                    r.has_wide_offsets,
                )
        return buffers

    @cache(num_args=3)
    def storage_buffer(self, table_id: int, row: int, col: int) -> bytes | None:
        row_buffer = self.storage_buffers(table_id).get(row)
        if row_buffer is None:
            return None
        if col >= len(row_buffer):
            return None
        return row_buffer[col]

    def recalculate_row_headers(self, table_id: int, data: list) -> None:
        base_data_store = self.objects[table_id].base_data_store
        buckets = self.objects[base_data_store.rowHeaders.buckets[0].identifier]
        # Capture the sizes already on disk BEFORE clearing, keyed by row.
        # A saved size of 0.0 is ambiguous on its own -- it's the file
        # format's normal "never customized, use default_row_height" state
        # for most rows, but it's also what Table.row_height(row, 0) writes
        # to mean "auto-fit this row". row_height()'s own getter resolves
        # that ambiguity in favour of the common case (substituting
        # default_row_height) and must keep doing that for in-memory
        # queries like Table.height -- see test_table_size.py::test_row_col_sizes,
        # which depends on exactly that substitution and never calls save().
        # But re-deriving every row's height through that getter here, on
        # every single save, silently overwrites a literal 0.0 that was
        # deliberately saved (by this process or a previous one) with a
        # concrete rounded default -- destroying the row_height(row, 0)
        # auto-fit sentinel the very next time the file is saved again.
        # Preserve any row's existing on-disk value verbatim unless this
        # session explicitly set a new height for it.
        existing_sizes = {h.index: h.size for h in buckets.headers}
        overrides = self._row_heights.get(table_id, {})
        clear_field_container(buckets.headers)

        for row, cells in enumerate(data):
            num_cols = len(cells) - sum([isinstance(x, MergedCell) for x in cells])
            if row in overrides:
                height = overrides[row]
            elif row in existing_sizes:
                height = existing_sizes[row]
            else:
                height = round(self.objects[table_id].default_row_height)
            header = TSTArchives.HeaderStorageBucket.Header(
                index=row,
                numberOfCells=num_cols,
                size=height,
                hidingState=0,
            )
            buckets.headers.append(header)

    def recalculate_column_headers(self, table_id: int, data: list) -> None:
        base_data_store = self.objects[table_id].base_data_store
        buckets = self.objects[base_data_store.columnHeaders.identifier]
        # See recalculate_row_headers() for why existing sizes are captured
        # and preserved verbatim rather than re-derived through col_width()'s
        # getter: the same 0.0-is-ambiguous issue applies to column widths.
        existing_sizes = {h.index: h.size for h in buckets.headers}
        overrides = self._col_widths.get(table_id, {})
        clear_field_container(buckets.headers)
        # Transpose data to get columns
        col_data = [list(x) for x in zip(*data)]

        for col, cells in enumerate(col_data):
            num_rows = len(cells) - sum([isinstance(x, MergedCell) for x in cells])
            if col in overrides:
                width = overrides[col]
            elif col in existing_sizes:
                width = existing_sizes[col]
            else:
                width = round(self.objects[table_id].default_column_width)
            header = TSTArchives.HeaderStorageBucket.Header(
                index=col,
                numberOfCells=num_rows,
                size=width,
                hidingState=0,
            )
            buckets.headers.append(header)

    def recalculate_merged_cells(self, table_id: int) -> None:
        merge_cells = self.merge_cells(table_id)

        merge_map_id, merge_map = self.objects.create_object_from_dict(
            "CalculationEngine",
            {},
            TSTArchives.MergeRegionMapArchive,
        )

        merge_cells = self.merge_cells(table_id)
        for row_col in merge_cells.merge_cells():
            size = merge_cells.size(row_col)
            cell_id = TSTArchives.CellID(packedData=(row_col[1] << 16 | row_col[0]))
            table_size = TSTArchives.TableSize(packedData=(size[1] << 16 | size[0]))
            cell_range = TSTArchives.CellRange(origin=cell_id, size=table_size)
            merge_map.cell_range.append(cell_range)

        base_data_store = self.objects[table_id].base_data_store
        self.set_reference(base_data_store.merge_region_map, merge_map_id)

    def recalculate_row_info(
        self,
        table_id: int,
        data: list,
        tile_row_offset: int,
        row: int,
    ) -> TSTArchives.TileRowInfo:
        row_info = TSTArchives.TileRowInfo()
        row_info.storage_version = 5
        row_info.tile_row_index = row - tile_row_offset
        row_info.cell_count = 0
        cell_storage = b""

        offsets = [-1] * len(data[0])
        current_offset = 0

        for col in range(len(data[row])):
            buffer = data[row][col]._to_buffer()
            if buffer is not None:
                cell_storage += buffer
                # Always use wide offsets
                offsets[col] = current_offset >> 2
                current_offset += len(buffer)

                row_info.cell_count += 1

        row_info.cell_offsets = pack(f"<{len(offsets)}h", *offsets)
        row_info.cell_offsets_pre_bnc = DEFAULT_PRE_BNC_BYTES
        row_info.cell_storage_buffer = cell_storage
        row_info.cell_storage_buffer_pre_bnc = DEFAULT_PRE_BNC_BYTES
        row_info.has_wide_offsets = True
        return row_info

    @cache()
    def metadata_component(self, reference: str | int | None = None) -> int:
        """Return the ID of an object in the document metadata given it's name or ID."""
        component_map = {c.identifier: c for c in self.objects[PACKAGE_ID].components}
        if isinstance(reference, str):
            component_ids = [
                x for x, c in component_map.items() if c.preferred_locator == reference
            ]
        else:
            component_ids = [x for x, c in component_map.items() if c.identifier == reference]
        return component_map[component_ids[0]]

    def add_component_metadata(self, object_id: int, parent: str, locator: str) -> None:
        """Add a new ComponentInfo record to the parent object in the document metadata."""
        locator = locator.format(object_id)
        preferred_locator = re.sub(r"\-\d+.*", "", locator)
        component_info = TSPArchiveMessages.ComponentInfo(
            identifier=object_id,
            locator=locator,
            preferred_locator=preferred_locator,
            is_stored_outside_object_archive=False,
            document_read_version=[2, 0, 0],
            document_write_version=[2, 0, 0],
            save_token=1,
        )
        self.objects[PACKAGE_ID].components.append(component_info)
        self.add_component_reference(object_id, location=parent)

    def add_component_reference(
        self,
        object_id: int,
        location: str | None = None,
        component_id: int | None = None,
        is_weak: bool = False,
    ) -> None:
        """Add an external reference to an object in a metadata component."""
        component = self.metadata_component(location or component_id)
        if component_id is not None:
            params = {"object_identifier": object_id, "component_identifier": component_id}
        else:
            params = {"component_identifier": object_id}
        if is_weak:
            params["is_weak"] = True
        component.external_references.append(
            TSPArchiveMessages.ComponentExternalReference(**params),
        )

    # DataStore lists that every Numbers-written table in tests/data carries.
    # Third-party writers can omit them (tests/data/issue-18.numbers comes from
    # SheetJS and lacks all of these), and Numbers then renders the whole table
    # blank, so save creates any that are missing. multipleChoiceListFormatTable
    # is deliberately not listed: tables saved by older Numbers versions lack it
    # and render normally.
    REQUIRED_DATA_LISTS: ClassVar[dict[str, int]] = {
        "format_table": TSTArchives.TableDataList.ListType.FORMAT,
        "formulaErrorTable": TSTArchives.TableDataList.ListType.FORMULA_ERROR,
        "rich_text_table": TSTArchives.TableDataList.ListType.RICH_TEXT_PAYLOAD,
        "conditionalstyletable": TSTArchives.TableDataList.ListType.CONDITIONAL_STYLE,
        "commentStorageTable": TSTArchives.TableDataList.ListType.COMMENT_STORAGE,
        "importWarningSetTable": TSTArchives.TableDataList.ListType.IMPORT_WARNING,
        "control_cell_spec_table": TSTArchives.TableDataList.ListType.CONTROL_CELL_SPEC,
    }

    def ensure_table_data_lists(self, table_id: int) -> None:
        """
        Create any of REQUIRED_DATA_LISTS a table's DataStore lacks.

        Verified in Numbers 15.3.1: a table missing only format_table renders
        blank, and issue-18.numbers renders correctly once these lists exist.
        format_table is seeded from the legacy format_table_pre_bnc when that is
        present so existing format keys stay valid.
        """
        base_data_store = self.objects[table_id].base_data_store
        for field, list_type in self.REQUIRED_DATA_LISTS.items():
            if base_data_store.HasField(field):
                continue
            if field == "format_table" and base_data_store.HasField("format_table_pre_bnc"):
                self._table_formats.add_table(table_id)
                continue
            list_id, _ = self.objects.create_object_from_dict(
                "Index/Tables/TableDataList-{}",
                {"listType": list_type, "nextListID": 1},
                TSTArchives.TableDataList,
            )
            self.add_component_metadata(list_id, "CalculationEngine", "Tables/TableDataList-{}")
            self.set_reference(getattr(base_data_store, field), list_id)

    def recalculate_table_data(self, table_id: int, data: list) -> None:
        table_model = self.objects[table_id]
        table_model.number_of_rows = len(data)
        table_model.number_of_columns = len(data[0])

        self.ensure_table_data_lists(table_id)
        self.init_table_strings(table_id)
        self.recalculate_row_headers(table_id, data)
        self.recalculate_column_headers(table_id, data)
        self.recalculate_merged_cells(table_id)
        self.update_paragraph_styles(data)
        self.update_cell_styles(table_id, data)
        self.update_cell_borders(table_id, data)

        # Clear the reference first so that the ColumnRowUIDMapArchive it
        # pointed to is already unreferenced when unreferenced objects are
        # removed; the other order left the orphan in the file until the
        # following save.
        table_model.ClearField("base_column_row_uids")
        self.objects.remove_unreferenced_objects()

        tile_idx = 0
        max_tile_idx = len(data) >> 8
        base_data_store = self.objects[table_id].base_data_store
        base_data_store.tiles.ClearField("tiles")
        if len(data[0]) > MAX_TILE_SIZE:
            base_data_store.tiles.should_use_wide_rows = True

        while tile_idx <= max_tile_idx:
            row_start = tile_idx * MAX_TILE_SIZE
            if (len(data) - row_start) > MAX_TILE_SIZE:
                num_rows = MAX_TILE_SIZE
                row_end = row_start + MAX_TILE_SIZE
            else:
                num_rows = len(data) - row_start
                row_end = row_start + num_rows

            tile_dict = {
                "maxColumn": 0,
                "maxRow": 0,
                "numCells": 0,
                "numrows": num_rows,
                "storage_version": 5,
                "rowInfos": [],
                "last_saved_in_BNC": True,
                "should_use_wide_rows": True,
            }
            tile_id, tile = self.objects.create_object_from_dict(
                "Index/Tables/Tile-{}",
                tile_dict,
                TSTArchives.Tile,
            )
            for row in range(row_start, row_end):
                row_info = self.recalculate_row_info(table_id, data, row_start, row)
                tile.rowInfos.append(row_info)

            tile_ref = TSTArchives.TileStorage.Tile()
            tile_ref.tileid = tile_idx
            tile_ref.tile.MergeFrom(TSPMessages.Reference(identifier=tile_id))
            base_data_store.tiles.tiles.append(tile_ref)
            base_data_store.tiles.tile_size = MAX_TILE_SIZE

            self.add_component_metadata(tile_id, "CalculationEngine", "Tables/Tile-{}")

            tile_idx += 1

        self.objects.update_object_file_store()

    def create_string_table(self):
        table_strings_id, table_strings = self.objects.create_object_from_dict(
            "Index/Tables/DataList-{}",
            {"listType": TSTArchives.TableDataList.ListType.STRING, "nextListID": 1},
            TSTArchives.TableDataList,
        )
        self.add_component_metadata(table_strings_id, "CalculationEngine", "Tables/DataList-{}")
        return table_strings_id, table_strings

    def table_height(self, table_id: int) -> int:
        """Return the height of a table in points."""
        height = 0.0
        for row in range(self.number_of_rows(table_id)):
            height += self.row_height(table_id, row)
        return floor(height)

    def row_height(self, table_id: int, row: int, height: int | None = None) -> int:
        if height is not None:
            if table_id not in self._row_heights:
                self._row_heights[table_id] = {}
            self._row_heights[table_id][row] = height
            return height

        if table_id in self._row_heights and row in self._row_heights[table_id]:
            return self._row_heights[table_id][row]

        table_model = self.objects[table_id]
        bds = self.objects[table_id].base_data_store
        bucket_id = bds.rowHeaders.buckets[0].identifier
        buckets = self.objects[bucket_id].headers
        bucket_map = {x.index: x for x in buckets}
        if row in bucket_map and bucket_map[row].size != 0.0:
            height = round(bucket_map[row].size)
        else:
            height = round(table_model.default_row_height)

        return height

    def _shift_sizes(self, overrides: dict, headers, start: int, count: int) -> None:
        """
        Move per-row or per-column sizes when rows or columns are inserted
        (``count > 0``) or deleted (``count < 0``) at ``start``.

        Sizes are stored by position, both in this session's override dict
        and in the file's header buckets, so without this a custom height or
        width stays at its old index while the content moves. Inserted
        rows or columns take the size of the row or column that was at
        ``start`` before the shift (as Numbers does), and nothing if there
        was none. A stored 0.0 is moved verbatim, like any other value.
        """
        if count > 0:

            def shifted(index: int) -> int | None:
                return index + count if index >= start else index

        else:
            removed = -count

            def shifted(index: int) -> int | None:
                if index < start:
                    return index
                if index < start + removed:
                    return None
                return index - removed

        new_overrides = {}
        for index, value in overrides.items():
            new_index = shifted(index)
            if new_index is not None:
                new_overrides[new_index] = value
        if count > 0 and start in overrides:
            for i in range(start, start + count):
                new_overrides[i] = overrides[start]
        overrides.clear()
        overrides.update(new_overrides)

        old_headers = []
        for header in headers:
            copy = TSTArchives.HeaderStorageBucket.Header()
            copy.CopyFrom(header)
            old_headers.append(copy)
        clear_field_container(headers)
        rebuilt = []
        for header in old_headers:
            new_index = shifted(header.index)
            if new_index is not None:
                header.index = new_index
                rebuilt.append(header)
            if count > 0 and header.index == start + count and new_index is not None:
                for i in range(start, start + count):
                    inherited = TSTArchives.HeaderStorageBucket.Header()
                    inherited.CopyFrom(header)
                    inherited.index = i
                    rebuilt.append(inherited)
        for header in sorted(rebuilt, key=lambda h: h.index):
            headers.append(header)

    def shift_row_sizes(self, table_id: int, start: int, count: int) -> None:
        base_data_store = self.objects[table_id].base_data_store
        buckets = self.objects[base_data_store.rowHeaders.buckets[0].identifier]
        overrides = self._row_heights.setdefault(table_id, {})
        self._shift_sizes(overrides, buckets.headers, start, count)

    def shift_col_sizes(self, table_id: int, start: int, count: int) -> None:
        base_data_store = self.objects[table_id].base_data_store
        buckets = self.objects[base_data_store.columnHeaders.identifier]
        overrides = self._col_widths.setdefault(table_id, {})
        self._shift_sizes(overrides, buckets.headers, start, count)

    def table_width(self, table_id: int) -> int:
        """Return the width of a table in points."""
        width = 0.0
        for row in range(self.number_of_columns(table_id)):
            width += self.col_width(table_id, row)
        return round(width)

    def col_width(self, table_id: int, col: int, width: int | None = None) -> int:
        if width is not None:
            if table_id not in self._col_widths:
                self._col_widths[table_id] = {}
            self._col_widths[table_id][col] = width
            return width

        if table_id in self._col_widths and col in self._col_widths[table_id]:
            return self._col_widths[table_id][col]

        table_model = self.objects[table_id]
        bds = self.objects[table_id].base_data_store
        bucket_id = bds.columnHeaders.identifier
        buckets = self.objects[bucket_id].headers
        bucket_map = {x.index: x for x in buckets}
        if col in bucket_map and bucket_map[col].size != 0.0:
            width = round(bucket_map[col].size)
        else:
            width = round(table_model.default_column_width)
        return width

    def num_header_rows(self, table_id: int, num_headers: int | None = None) -> int:
        """Return/set the number of header rows."""
        table_model = self.objects[table_id]
        if num_headers is not None:
            table_model.number_of_header_rows = num_headers
        return table_model.number_of_header_rows

    def num_header_cols(self, table_id: int, num_headers: int | None = None) -> int:
        """Return/set the number of header columns."""
        table_model = self.objects[table_id]
        if num_headers is not None:
            table_model.number_of_header_columns = num_headers
        return table_model.number_of_header_columns

    def table_coordinates(self, table_id: int) -> tuple[float]:
        table_info = self.objects[self.table_info_id(table_id)]
        return (
            table_info.super.geometry.position.x,
            table_info.super.geometry.position.y,
        )

    def is_a_pivot_table(self, table_id: int) -> bool:
        """Table is a pivot table."""
        table_info = self.objects[self.table_info_id(table_id)]
        return table_info.is_a_pivot_table

    def last_table_offset(self, sheet_id):
        """Y offset of the last table in a sheet."""
        table_id = self.table_ids(sheet_id)[-1]
        y_offset = next(
            self.objects[self.table_info_id(x)].super.geometry.position.y
            for x in self.table_ids(sheet_id)
            if x == table_id
        )

        return self.table_height(table_id) + y_offset

    def create_drawable(
        self,
        sheet_id: int,
        x: float,
        y: float,
        flags: int = 3,
        height: float = 231.0,
        width: float = 494.0,
    ) -> object:
        """Create a DrawableArchive for a new table in a sheet."""
        table_x = x if x is not None else 0.0
        table_y = y if y is not None else self.last_table_offset(sheet_id) + DEFAULT_TABLE_OFFSET
        return TSDArchives.DrawableArchive(
            parent=TSPMessages.Reference(identifier=sheet_id),
            geometry=TSDArchives.GeometryArchive(
                angle=0.0,
                flags=flags,
                position=TSPMessages.Point(x=table_x, y=table_y),
                size=TSPMessages.Size(height=height, width=width),
            ),
        )

    def add_table(
        self,
        sheet_id: int,
        table_name: str,
        from_table_id: int,
        x: float,
        y: float,
        num_rows: int,
        num_cols: int,
        number_of_header_rows=1,
        number_of_header_columns=1,
    ) -> int:
        from_table = self.objects[from_table_id]

        table_strings_id, _ = self.create_string_table()

        # Build a minimal table duplicating references from the source table
        from_table_refs = field_references(from_table)
        # This table's own persistent identity. Numbers.app's kind-1
        # (TABLE_MODEL) trust check reads this exact value back (see
        # add_formula_owner()/derive_table_identity_uuid()) -- minted once,
        # here, and threaded through rather than re-derived or re-minted
        # anywhere else.
        table_id_uuid = NumbersUUID()
        table_model_id, table_model = self.objects.create_object_from_dict(
            "CalculationEngine",
            {
                "table_id": str(table_id_uuid).upper(),
                "number_of_rows": num_rows,
                "number_of_columns": num_cols,
                "table_name": table_name,
                "table_name_enabled": True,
                "default_row_height": DEFAULT_ROW_HEIGHT,
                "default_column_width": DEFAULT_COLUMN_WIDTH,
                "number_of_header_rows": number_of_header_rows,
                "number_of_header_columns": number_of_header_columns,
                "header_rows_frozen": True,
                "header_columns_frozen": True,
                **from_table_refs,
            },
            TSTArchives.TableModelArchive,
        )
        # Suppress Numbers assertions for tables sharing the same data
        table_model.category_owner.identifier = 0

        column_headers_id, _ = self.objects.create_object_from_dict(
            "Index/Tables/HeaderStorageBucket-{}",
            {"bucketHashFunction": 1},
            TSTArchives.HeaderStorageBucket,
        )
        self.add_component_metadata(
            column_headers_id,
            "CalculationEngine",
            "Tables/HeaderStorageBucket-{}",
        )

        sidecar_id, _ = self.objects.create_object_from_dict(
            "CalculationEngine",
            {"max_order": 1, "column_count": 0, "row_count": 0},
            TSTArchives.StrokeSidecarArchive,
        )
        self.set_reference(table_model.stroke_sidecar, sidecar_id)

        style_table_id, _ = self.objects.create_object_from_dict(
            "Index/Tables/DataList-{}",
            {"listType": TSTArchives.TableDataList.ListType.STYLE, "nextListID": 1},
            TSTArchives.TableDataList,
        )
        self.add_component_metadata(style_table_id, "CalculationEngine", "Tables/DataList-{}")

        formula_table_id, _ = self.objects.create_object_from_dict(
            "Index/Tables/TableDataList-{}",
            {"listType": TSTArchives.TableDataList.ListType.FORMULA, "nextListID": 1},
            TSTArchives.TableDataList,
        )
        self.add_component_metadata(
            formula_table_id,
            "CalculationEngine",
            "Tables/TableDataList-{}",
        )

        format_table_pre_bnc_id, _ = self.objects.create_object_from_dict(
            "Index/Tables/TableDataList-{}",
            {"listType": TSTArchives.TableDataList.ListType.STYLE, "nextListID": 1},
            TSTArchives.TableDataList,
        )
        self.add_component_metadata(
            format_table_pre_bnc_id,
            "CalculationEngine",
            "Tables/TableDataList-{}",
        )

        data_store_refs = field_references(from_table.base_data_store)
        data_store_refs["stringTable"] = {"identifier": table_strings_id}
        data_store_refs["columnHeaders"] = {"identifier": column_headers_id}
        data_store_refs["styleTable"] = {"identifier": style_table_id}
        data_store_refs["formula_table"] = {"identifier": formula_table_id}
        data_store_refs["format_table_pre_bnc"] = {"identifier": format_table_pre_bnc_id}
        table_model.base_data_store.MergeFrom(
            TSTArchives.DataStore(
                rowHeaders=TSTArchives.HeaderStorage(bucketHashFunction=1),
                nextRowStripID=1,
                nextColumnStripID=0,
                rowTileTree=TSTArchives.TableRBTree(),
                columnTileTree=TSTArchives.TableRBTree(),
                tiles=TSTArchives.TileStorage(
                    tile_size=DEFAULT_TILE_SIZE,
                    should_use_wide_rows=True,
                ),
                **data_store_refs,
            ),
        )

        row_headers_id, _ = self.objects.create_object_from_dict(
            "Index/Tables/HeaderStorageBucket-{}",
            {"bucketHashFunction": 1},
            TSTArchives.HeaderStorageBucket,
        )

        self.add_component_metadata(
            row_headers_id,
            "CalculationEngine",
            "Tables/HeaderStorageBucket-{}",
        )
        table_model.base_data_store.rowHeaders.buckets.append(
            TSPMessages.Reference(identifier=row_headers_id),
        )

        table_info_id, table_info = self.objects.create_object_from_dict(
            "CalculationEngine",
            {},
            TSTArchives.TableInfoArchive,
        )
        table_info.tableModel.MergeFrom(TSPMessages.Reference(identifier=table_model_id))
        table_info.super.MergeFrom(self.create_drawable(sheet_id, x, y))

        haunted_owner_uuid = self.add_formula_owner(
            table_info_id,
            table_id_uuid,
            num_rows,
            num_cols,
            number_of_header_rows,
            number_of_header_columns,
        )
        table_model.haunted_owner.owner_uid.MergeFrom(haunted_owner_uuid.protobuf2)
        self.calculate_table_uuid_map()

        self._table_data[table_model_id] = [
            [Cell._empty_cell(table_model_id, row, col, self) for col in range(num_cols)]
            for row in range(num_rows)
        ]
        self.recalculate_table_data(table_model_id, self._table_data[table_model_id])

        self.add_component_reference(
            table_info_id,
            location="Document",
            component_id=self.calc_engine_id(),
        )
        self.create_caption_archive(table_model_id)
        self.caption_enabled(table_model_id, False)

        self.objects[sheet_id].drawable_infos.append(
            TSPMessages.Reference(identifier=table_info_id),
        )
        self.register_table_in_sidebar(sheet_id, table_info_id)

        self.name_ref_cache.mark_dirty()
        return table_model_id

    def _sidebar_root(self):
        """
        The root TreeNode of DocumentArchive.sidebar_order, or None if this
        document doesn't have one at all.
        """
        document = self.objects[DOCUMENT_ID]
        if not document.HasField("sidebar_order"):
            return None
        return self.objects[document.sidebar_order.identifier]

    def _find_sidebar_node(self, parent_node, object_id: int):
        """
        The direct child of parent_node whose `object` is object_id, if any.

        Skips (rather than raising on) a child reference that doesn't
        resolve to a real object. This investigation found dangling
        references in real Numbers.app-authored documents (a stale
        object_uuid_map entry, unrelated to this fix but proof such
        references exist in the wild) -- a sidebar lookup has no reason to
        be less tolerant of that than the rest of the model already is.
        """
        return next(
            (
                self.objects[ref.identifier]
                for ref in parent_node.children
                if ref.identifier in self.objects
                and self.objects[ref.identifier].HasField("object")
                and self.objects[ref.identifier].object.identifier == object_id
            ),
            None,
        )

    def _append_sidebar_child(self, parent_node, object_id: int) -> None:
        """Append a new leaf TreeNode{object: object_id} under parent_node."""
        node_id, _ = self.objects.create_object_from_dict(
            "Document",
            {"object": {"identifier": object_id}},
            TSKArchives.TreeNode,
        )
        parent_node.children.append(TSPMessages.Reference(identifier=node_id))

    def register_sheet_in_sidebar(self, sheet_id: int) -> None:
        """
        Add this sheet to DocumentArchive.sidebar_order as a top-level
        TreeNode, so it (and anything registered under it, such as its
        tables via register_table_in_sidebar()) appears in Numbers' own
        navigator sidebar.

        add_sheet() previously never touched the sidebar tree at all, so a
        library-added sheet -- and every table on it, since
        register_table_in_sidebar() has nothing to attach to without this --
        was completely absent from the navigator. Same class of gap as
        register_table_in_sidebar(), one level up; see that method's
        docstring for why this is a plain add_sheet() omission and not
        entangled with the kind-1 owner identity fix.
        """
        root = self._sidebar_root()
        if root is None:
            return
        self._append_sidebar_child(root, sheet_id)

    def register_table_in_sidebar(self, sheet_id: int, table_info_id: int) -> None:
        """
        Add this table to DocumentArchive.sidebar_order as a TreeNode, so it
        appears in Numbers' own navigator sidebar.

        add_table() previously only added the new table to the sheet's own
        drawable_infos; the sidebar tree is a separate structure Numbers.app
        reads independently, and nothing wrote to it. This is a plain
        omission in add_table() itself -- it happens on every call
        regardless of whether the table ever gets a formula written to it --
        not something specific to the formula-writing feature.

        This is unrelated to (and unaffected by) the kind-1 owner identity
        fix in derive_table_identity_uuid()/add_formula_owner(): the two
        were previously entangled only because, before that fix, Numbers'
        "adoption" of an untrusted kind-1 owner happened to also mint a
        sidebar TreeNode as a side effect of fully reprocessing the table.
        Now that a library-created table's kind-1 owner is trusted from the
        first open and adoption no longer fires, nothing about opening the
        file would still add a missing TreeNode -- so it needs to be
        written here directly, independent of that fix, or the gap would
        become permanent instead of self-healing on first save.
        """
        root = self._sidebar_root()
        if root is None:
            return
        sheet_node = self._find_sidebar_node(root, sheet_id)
        if sheet_node is None:
            # No existing sidebar entry for this sheet. Shouldn't happen any
            # more for a sheet added via add_sheet() (which now calls
            # register_sheet_in_sidebar() itself), but degrade gracefully
            # rather than raise for any other document shape that reaches
            # here without one.
            return
        self._append_sidebar_child(sheet_node, table_info_id)

    def add_formula_owner(
        self,
        table_info_id: int,
        table_id_uuid: NumbersUUID,
        num_rows: int,
        num_cols: int,
        number_of_header_rows: int,
        number_of_header_columns: int,
    ) -> NumbersUUID:
        """
        Create a FormulaOwnerDependenciesArchive that references a TableInfoArchive
        so that cross-references to cells in this table will work.

        table_id_uuid must be the exact same NumbersUUID the caller wrote into
        this table's own TableModelArchive.table_id (a string field) -- see
        derive_table_identity_uuid() for why the kind-1 owner's identity has
        to be computed from that value specifically, not minted independently.
        """
        # Numbers.app does not trust a freshly minted uuid1 as this table's
        # kind-1 (TABLE_MODEL) identity: on first open, finding the identity
        # information it requires missing, it silently retires ("adopts")
        # the untrusted owner and rebuilds the table's identity -- and every
        # reference into the table that named it under the old identity --
        # from scratch, under a value it derives itself. See derive_table_identity_uuid(),
        # commit 7158166 and tests/test_table_identity_adoption.py.
        # The aux owner family below (HAUNTED_OWNER) is deliberately left
        # untouched: Numbers keeps it regardless of its UUID value.
        table_model_owner_uuid = derive_table_identity_uuid(table_id_uuid)
        calc_engine = self.calc_engine()
        owner_id_map = calc_engine.dependency_tracker.owner_id_map.map_entry
        next_owner_id = max([x.internal_owner_id for x in owner_id_map]) + 1
        volatile_dependencies = {
            "volatile_time_cells": {},
            "volatile_random_cells": {},
            "volatile_locale_cells": {},
            "volatile_sheet_table_name_cells": {},
            "volatile_remote_data_cells": {},
            "volatile_geometry_cell_refs": {},
        }
        total_range_for_table = {
            "top_left_column": 0,
            "top_left_row": 0,
            "bottom_right_column": num_cols - 1,
            "bottom_right_row": num_rows - 1,
        }
        body_range_for_table = {
            "top_left_column": number_of_header_columns,
            "top_left_row": number_of_header_rows,
            "bottom_right_column": num_cols - 1,
            "bottom_right_row": num_rows - 1,
        }

        formula_deps_id, _ = self.objects.create_object_from_dict(
            "CalculationEngine",
            {
                "formula_owner_uid": table_model_owner_uuid.dict2,
                "internal_formula_owner_id": next_owner_id,
                "owner_kind": OwnerKind.TABLE_MODEL,
                "cell_dependencies": {},
                "range_dependencies": {},
                "volatile_dependencies": volatile_dependencies,
                "spanning_column_dependencies": {
                    "total_range_for_table": total_range_for_table,
                    "body_range_for_table": body_range_for_table,
                },
                "spanning_row_dependencies": {
                    "total_range_for_table": total_range_for_table,
                    "body_range_for_table": body_range_for_table,
                },
                "whole_owner_dependencies": {"dependent_cells": {}},
                "cell_errors": {},
                "formula_owner": {"identifier": table_info_id},
                "tiled_cell_dependencies": {},
                "uuid_references": {},
                "tiled_range_dependencies": {},
            },
            TSCEArchives.FormulaOwnerDependenciesArchive,
        )
        calc_engine.dependency_tracker.formula_owner_dependencies.append(
            TSPMessages.Reference(identifier=formula_deps_id),
        )
        owner_id_map.append(
            TSCEArchives.OwnerIDMapArchive.OwnerIDMapArchiveEntry(
                internal_owner_id=next_owner_id,
                owner_id=table_model_owner_uuid.protobuf4,
            ),
        )
        self.register_table_identity_in_header_name_mgr(table_model_owner_uuid)

        # See Numbers.md#uuid-mapping for more details on mapping table model
        # UUID to the formula owner.
        formula_owner_uuid = NumbersUUID()
        base_owner_uuid = NumbersUUID()
        next_owner_id += 1
        null_range_ref = {
            "top_left_column": 0x7FFF,
            "top_left_row": 0x7FFFFFFF,
            "bottom_right_column": 0x7FFF,
            "bottom_right_row": 0x7FFFFFFF,
        }
        spanning_dependencies = {
            "total_range_for_table": null_range_ref,
            "body_range_for_table": null_range_ref,
        }
        formula_deps_id, _ = self.objects.create_object_from_dict(
            "CalculationEngine",
            {
                "formula_owner_uid": formula_owner_uuid.dict2,
                "internal_formula_owner_id": next_owner_id,
                "owner_kind": OwnerKind.HAUNTED_OWNER,
                "cell_dependencies": {},
                "range_dependencies": {},
                "volatile_dependencies": volatile_dependencies,
                "spanning_column_dependencies": spanning_dependencies,
                "spanning_row_dependencies": spanning_dependencies,
                "whole_owner_dependencies": {"dependent_cells": {}},
                "cell_errors": {},
                "base_owner_uid": base_owner_uuid.dict2,
                "tiled_cell_dependencies": {},
                "uuid_references": {},
                "tiled_range_dependencies": {},
            },
            TSCEArchives.FormulaOwnerDependenciesArchive,
        )
        calc_engine.dependency_tracker.formula_owner_dependencies.append(
            TSPMessages.Reference(identifier=formula_deps_id),
        )
        owner_id_map.append(
            TSCEArchives.OwnerIDMapArchive.OwnerIDMapArchiveEntry(
                internal_owner_id=next_owner_id,
                owner_id=formula_owner_uuid.protobuf4,
            ),
        )
        return formula_owner_uuid

    def register_table_identity_in_header_name_mgr(self, table_identity_uuid: NumbersUUID) -> None:
        """
        Append this table's derived kind-1 identity to the document's
        HeaderNameMgrArchive.per_tables list.

        This is one of the three sites (alongside FormulaOwnerDependenciesArchive
        .formula_owner_uid and its owner_id_map entry) that Numbers.app checks
        when deciding whether to keep or "adopt" a table's formula owner -- see
        derive_table_identity_uuid(), commit 7158166 and
        tests/test_table_identity_adoption.py. Some older documents (see issue-18)
        do not carry a HeaderNameMgrArchive at all; in that case there is
        nothing to register and this is a no-op, matching how the rest of the
        model tolerates that document shape.
        """
        header_name_mgr_ids = self.find_refs("HeaderNameMgrArchive")
        if len(header_name_mgr_ids) == 0:
            return
        header_name_mgr = self.objects[header_name_mgr_ids[0]]
        # Idempotency guard: safe against being called twice for the same
        # table (not exercised by the current call graph -- add_formula_owner()
        # calls this exactly once per table -- but cheap to guard against a
        # future caller creating a duplicate per_tables entry).
        target_hex = table_identity_uuid.hex
        if any(uuid_to_hex(pt.table_uid) == target_hex for pt in header_name_mgr.per_tables):
            return
        header_name_mgr.per_tables.append(
            TSTArchives.HeaderNameMgrArchive.PerTableArchive(
                table_uid=table_identity_uuid.protobuf2,
                # per_table_precedent is a required field in this archive;
                # (0, 0) means "no header name precedent set yet", which is
                # correct for a table that has just been created.
                per_table_precedent=TSCEArchives.CellCoordinateArchive(column=0, row=0),
            ),
        )

    def add_sheet(self, sheet_name: str) -> int:
        """Add a new sheet with a copy of a table from another sheet."""
        sheet_id, _ = self.objects.create_object_from_dict(
            "Document",
            {"name": sheet_name},
            TNArchives.SheetArchive,
        )

        self.add_component_reference(
            sheet_id,
            location="CalculationEngine",
            component_id=DOCUMENT_ID,
            is_weak=True,
        )

        self.objects[DOCUMENT_ID].sheets.append(TSPMessages.Reference(identifier=sheet_id))
        self.register_sheet_in_sidebar(sheet_id)

        return sheet_id

    @property
    def styles(self):
        if self._styles is None:
            self._styles = self.available_paragraph_styles()
        return self._styles

    @cache(num_args=0)
    def available_paragraph_styles(self) -> dict[str, Style]:
        theme_id = self.objects[DOCUMENT_ID].theme.identifier
        presets = find_extension(self.objects[theme_id].super, "paragraph_style_presets")
        presets_map = {
            self.objects[x.identifier].super.name: {
                "id": x.identifier,
                "obj": self.objects[x.identifier],
            }
            for x in presets
        }
        styles = {
            k: Style(
                alignment=Alignment(
                    HorizontalJustification(v["obj"].para_properties.alignment),
                    VerticalJustification(0),
                ),
                font_color=self.cell_font_color(v["obj"]),
                font_size=self.cell_font_size(v["obj"]),
                font_name=self.cell_font_family(v["obj"]),
                bold=self.cell_is_bold(v["obj"]),
                italic=self.cell_is_italic(v["obj"]),
                underline=self.cell_is_underline(v["obj"]),
                strikethrough=self.cell_is_strikethrough(v["obj"]),
                name=self.cell_style_name(v["obj"]),
                _text_style_obj_id=v["id"],
                _font_details=self.cell_font_details(v["obj"]),
            )
            for k, v in presets_map.items()
        }
        for key, style in styles.items():
            # Keep the stored face rather than the family default (see from_storage)
            style.__dict__["_font_details"] = self.cell_font_details(presets_map[key]["obj"])
            # Override __setattr__ behavior for builtin styles
            style.__dict__["_update_text_style"] = False
            style.__dict__["_update_cell_style"] = False
        return styles

    def add_paragraph_style(self, style: Style) -> int:
        if style.underline:
            underline = CharacterStyle.UnderlineType.kSingleUnderline
        else:
            underline = CharacterStyle.UnderlineType.kNoUnderline
        if style.strikethrough:
            strikethru = CharacterStyle.StrikethruType.kSingleStrikethru
        else:
            strikethru = CharacterStyle.StrikethruType.kNoStrikethru

        style_id_name = "numbers-parser-" + style.name.lower().replace(" ", "-")
        para_style_id, para_style = self.objects.create_object_from_dict(
            "DocumentStylesheet",
            {
                "super": {
                    "name": style.name,
                    "style_identifier": style_id_name,
                },
                "override_count": 1,
                "char_properties": {
                    "font_color": {
                        "model": "rgb",
                        "r": style.font_color.r / 255,
                        "g": style.font_color.g / 255,
                        "b": style.font_color.b / 255,
                        "a": 1.0,
                        "rgbspace": "srgb",
                    },
                    "bold": style.bold | style._font_details["bold"],
                    "italic": style.italic | style._font_details["italic"],
                    "underline": underline,
                    "strikethru": strikethru,
                    "font_size": style.font_size,
                    "font_name": style._font_details["name"],
                    "tsd_fill": {
                        "color": {
                            "model": "rgb",
                            "r": style.font_color.r / 255,
                            "g": style.font_color.g / 255,
                            "b": style.font_color.b / 255,
                            "a": 1.0,
                            "rgbspace": "srgb",
                        },
                    },
                },
                "para_properties": {
                    "alignment": style.alignment.horizontal,
                    "first_line_indent": style.first_indent,
                    "left_indent": style.left_indent,
                    "right_indent": style.right_indent,
                },
            },
            TSWPArchives.ParagraphStyleArchive,
        )
        if style.baseline_shift is not None:
            para_style.char_properties.baseline_shift = style.baseline_shift
        if style.line_spacing is not None:
            self._set_line_spacing(para_style, style.line_spacing)
        stylesheet_id = self.objects[DOCUMENT_ID].stylesheet.identifier
        para_style.super.stylesheet.MergeFrom(TSPMessages.Reference(identifier=stylesheet_id))
        self.objects[stylesheet_id].styles.append(TSPMessages.Reference(identifier=para_style_id))
        self.objects[stylesheet_id].identifier_to_style_map.append(
            TSSArchives.StylesheetArchive.IdentifiedStyleEntry(
                identifier=style_id_name,
                style=TSPMessages.Reference(identifier=para_style_id),
            ),
        )

        theme_id = self.objects[DOCUMENT_ID].theme.identifier
        presets = find_extension(self.objects[theme_id].super, "paragraph_style_presets")
        presets.append(TSPMessages.Reference(identifier=para_style_id))
        self._styles[style.name] = style
        return para_style_id

    def update_paragraph_style(self, style: Style) -> None:
        if style.underline:
            underline = CharacterStyle.UnderlineType.kSingleUnderline
        else:
            underline = CharacterStyle.UnderlineType.kNoUnderline
        if style.strikethrough:
            strikethru = CharacterStyle.StrikethruType.kSingleStrikethru
        else:
            strikethru = CharacterStyle.StrikethruType.kNoStrikethru
        style_obj = self.objects[style._text_style_obj_id]
        style_obj.char_properties.font_color.r = style.font_color.r / 255
        style_obj.char_properties.font_color.g = style.font_color.g / 255
        style_obj.char_properties.font_color.b = style.font_color.b / 255
        style_obj.char_properties.bold = style.bold
        style_obj.char_properties.italic = style.italic
        style_obj.char_properties.underline = underline
        style_obj.char_properties.strikethru = strikethru
        style_obj.char_properties.font_size = style.font_size
        style_obj.char_properties.font_name = style._font_details["name"]
        style_obj.char_properties.tsd_fill.color.r = style.font_color.r / 255
        style_obj.char_properties.tsd_fill.color.g = style.font_color.g / 255
        style_obj.char_properties.tsd_fill.color.b = style.font_color.b / 255
        style_obj.para_properties.alignment = style.alignment.horizontal
        style_obj.para_properties.first_line_indent = style.first_indent
        style_obj.para_properties.left_indent = style.left_indent
        style_obj.para_properties.right_indent = style.right_indent
        if style.baseline_shift is not None:
            style_obj.char_properties.baseline_shift = style.baseline_shift
        if style.line_spacing is not None:
            self._set_line_spacing(style_obj, style.line_spacing)

    @staticmethod
    def _set_line_spacing(style_obj: object, amount: float) -> None:
        style_obj.para_properties.line_spacing_null = False
        style_obj.para_properties.line_spacing.mode = (
            TSWPArchives.LineSpacingArchive.kRelativeLineSpacing
        )
        style_obj.para_properties.line_spacing.amount = amount

    def update_paragraph_styles(self, data: list = None) -> None:
        """
        Create new paragraph style archives for any new styles that
        have been created for this document, and re-save any existing
        style that's been mutated.

        `self.styles` only holds styles registered via the document's
        own style registry (its theme presets, or add_style()) -- it's
        never updated to include a Style object handed back by
        cell.style, so a mutation made through cell.style is invisible
        to the scan above it. When `data` (the table's own cells) is
        given, scan it directly as well, the same way
        update_cell_styles() already does for cell-level styles.
        """
        new_styles = [x for x in self.styles.values() if x._text_style_obj_id is None]
        updated_styles = [
            x
            for x in self.styles.values()
            if x._text_style_obj_id is not None and x._update_text_style
        ]
        for style in new_styles:
            style._text_style_obj_id = self.add_paragraph_style(style)
            style._update_text_style = True

        processed = {id(x) for x in updated_styles}
        for style in updated_styles:
            self.update_paragraph_style(style)

        if data is not None:
            # Registered styles (theme presets and add_style()) were handled
            # above; this loop is for styles read from cells.
            processed.update(id(x) for x in self.styles.values())
            registered = {x._text_style_obj_id for x in self.styles.values()}
            uses = None
            for cells in data:
                for cell in cells:
                    style = cell._style
                    if style is None or not style._update_text_style or id(style) in processed:
                        continue
                    if style._text_style_obj_id is None:
                        # A never-styled cell: give it a variation of its
                        # position's default text style.
                        style._text_style_obj_id = self._add_paragraph_style_variation(
                            self.default_text_style_id(cell),
                        )
                    else:
                        if uses is None:
                            uses = self._text_style_uses()
                        if (
                            style._text_style_obj_id in registered
                            or uses[style._text_style_obj_id] > 1
                        ):
                            # Shared with other cells or a named style: edit a
                            # variation of it so only this cell changes.
                            uses[style._text_style_obj_id] -= 1
                            style._text_style_obj_id = self._add_paragraph_style_variation(
                                style._text_style_obj_id,
                            )
                            uses[style._text_style_obj_id] = 1
                    self.update_paragraph_style(style)
                    processed.add(id(style))

    def _text_style_uses(self) -> Counter:
        """Count the cells in every table that use each paragraph style object."""
        uses = Counter()
        for table_data in self._table_data.values():
            for cells in table_data:
                for cell in cells:
                    if cell._style is not None and cell._style._text_style_obj_id is not None:
                        uses[cell._style._text_style_obj_id] += 1
                    elif cell._text_style_id is not None:
                        uses[self.text_style_object_id(cell)] += 1
        return uses

    def _add_paragraph_style_variation(self, base_id: int) -> int:
        """
        Copy a paragraph style as an unnamed variation of it, the way Numbers
        stores a cell-level override, and return the new style's id.

        A named style becomes the variation's parent. A copy of an unnamed
        style keeps that style's own parent. The new style is listed in the
        stylesheet's styles and under its parent in parent_to_children_style_map.
        """
        base = self.objects[base_id]
        style_id, style = self.objects.create_object_from_dict(
            "DocumentStylesheet",
            {},
            TSWPArchives.ParagraphStyleArchive,
        )
        style.CopyFrom(base)
        if base.super.name:
            style.super.ClearField("name")
            style.super.ClearField("style_identifier")
            style.super.parent.identifier = base_id
            style.super.is_variation = True
            style.override_count = 1
        stylesheet_id = self.objects[DOCUMENT_ID].stylesheet.identifier
        stylesheet = self.objects[stylesheet_id]
        style.super.stylesheet.identifier = stylesheet_id
        stylesheet.styles.append(TSPMessages.Reference(identifier=style_id))
        if style.super.HasField("parent"):
            parent_id = style.super.parent.identifier
            entry = next(
                (
                    e
                    for e in stylesheet.parent_to_children_style_map
                    if e.parent.identifier == parent_id
                ),
                None,
            )
            if entry is None:
                entry = stylesheet.parent_to_children_style_map.add()
                entry.parent.identifier = parent_id
            entry.children.append(TSPMessages.Reference(identifier=style_id))
        return style_id

    def update_cell_styles(self, table_id: int, data: list) -> None:
        """
        Create new cell style archives for any cells whose styles
        have changes that require a cell style.
        """
        cell_styles = {}
        for _, cells in enumerate(data):
            for _, cell in enumerate(cells):
                if cell._style is not None and cell._style._update_cell_style:
                    fingerprint = (
                        str(cell.style.alignment.vertical)
                        + str(cell.style.first_indent)
                        + str(cell.style.left_indent)
                        + str(cell.style.right_indent)
                        + str(cell.style.text_inset)
                        + str(cell.style.text_wrap)
                    )
                    if cell._style.bg_color is not None:
                        fingerprint = fingerprint + (
                            str(cell.style.bg_color.r)
                            + str(cell.style.bg_color.g)
                            + str(cell.style.bg_color.b)
                        )
                    if cell._style.bg_image is not None:
                        fingerprint += cell._style.bg_image.filename
                    if fingerprint not in cell_styles:
                        cell_styles[fingerprint] = self.add_cell_style(cell._style)
                    cell._style._cell_style_obj_id = cell_styles[fingerprint]

    def add_cell_style(self, style: Style) -> int:
        if style.bg_image is not None:
            digest = sha1(style.bg_image.data).digest()  # noqa: S324
            if digest in self._images:
                image_id = self._images[digest]
            else:
                datas = self.objects[PACKAGE_ID].datas
                image_id = self.next_image_identifier()
                datas.append(
                    TSPArchiveMessages.DataInfo(
                        identifier=image_id,
                        digest=digest,
                        preferred_file_name=style.bg_image.filename,
                        file_name=style.bg_image.filename,
                        materialized_length=len(style.bg_image.data),
                    ),
                )
                self._images[digest] = image_id
            color_attrs = {
                "cell_fill": {
                    "image": {
                        "technique": "ScaleToFill",
                        "imagedata": {"identifier": image_id},
                        "interpretsUntaggedImageDataAsGeneric": False,
                    },
                },
            }
        elif style.bg_color is not None:
            color_attrs = {
                "cell_fill": {
                    "color": {
                        "model": "rgb",
                        "r": style.bg_color.r / 255,
                        "g": style.bg_color.g / 255,
                        "b": style.bg_color.b / 255,
                        "a": 1.0,
                        "rgbspace": "srgb",
                    },
                },
            }
        else:
            color_attrs = {}
        cell_style_id, cell_style = self.objects.create_object_from_dict(
            "DocumentStylesheet",
            {
                "super": {
                    "name": style.name,
                    "style_identifier": "",
                },
                "override_count": 1,
                "cell_properties": {
                    **color_attrs,
                    "padding": {
                        "left": style.text_inset,
                        "top": style.text_inset,
                        "right": style.text_inset,
                        "bottom": style.text_inset,
                    },
                    "text_wrap": style.text_wrap,
                    "vertical_alignment": style.alignment.vertical,
                },
            },
            TSTArchives.CellStyleArchive,
        )
        style_id_name = f"numbers-parser-custom-{cell_style_id}"
        cell_style.super.style_identifier = style_id_name

        stylesheet_id = self.objects[DOCUMENT_ID].stylesheet.identifier
        cell_style.super.stylesheet.MergeFrom(TSPMessages.Reference(identifier=stylesheet_id))
        self.objects[stylesheet_id].styles.append(TSPMessages.Reference(identifier=cell_style_id))
        self.objects[stylesheet_id].identifier_to_style_map.append(
            TSSArchives.StylesheetArchive.IdentifiedStyleEntry(
                identifier=style_id_name,
                style=TSPMessages.Reference(identifier=cell_style_id),
            ),
        )

        return cell_style_id

    def text_style_object_id(self, cell: Cell) -> int:
        if cell._text_style_id is None:
            return None
        entry = self._table_styles.lookup_value(cell._table_id, cell._text_style_id)
        return entry.reference.identifier

    def cell_style_object_id(self, cell: Cell) -> int:
        if cell._cell_style_id is None:
            return None
        entry = self._table_styles.lookup_value(cell._table_id, cell._cell_style_id)
        return entry.reference.identifier

    def custom_style_name(self) -> str:
        """
        Find custom styles in the current document and return the next
        highest numbered style.
        """
        stylesheet_id = self.objects[DOCUMENT_ID].stylesheet.identifier
        current_styles = self.styles.keys()
        custom_styles = [x for x in current_styles if re.fullmatch(r"Custom Style \d+", x)]
        for style_entry in self.objects[stylesheet_id].identifier_to_style_map:
            style_id = style_entry.style.identifier
            style_name = getattr(self.objects[style_id].super, "name", "")
            if re.fullmatch(r"Custom Style \d+", style_name):
                custom_styles.append(style_name)

        if len(custom_styles) > 0:
            offset = len("Custom Style ")
            custom_style_ids = [int(x[offset:]) for x in custom_styles]
            return "Custom Style " + str(custom_style_ids[-1] + 1)
        return "Custom Style 1"

    @property
    def custom_formats(self) -> dict[str, CustomFormatting]:
        if self._custom_formats is None:
            custom_format_list = self.custom_format_list_archive()
            custom_formats = custom_format_list.custom_formats
            custom_format_names = [x.name for x in custom_formats]
            custom_format_uuids = list(custom_format_list.uuids)
            self._custom_formats = {}
            self._custom_format_archives = {}
            self._custom_format_uuids = {}
            for i, format_name in enumerate(custom_format_names):
                self._custom_formats[format_name] = CustomFormatting.from_archive(custom_formats[i])
                self._custom_format_archives[format_name] = custom_formats[i]
                self._custom_format_uuids[format_name] = custom_format_uuids[i]

        return self._custom_formats

    def custom_format_name(self) -> str:
        """
        Find custom formats in the current document and return the next
        highest numbered format.
        """
        current_formats = self.custom_formats.keys()
        if "Custom Format" not in current_formats:
            return "Custom Format"
        current_formats = [
            m.group(1) for x in current_formats if (m := re.fullmatch(r"Custom Format (\d+)", x))
        ]
        if len(current_formats) > 0:
            last_id = int(current_formats[-1])
            return f"Custom Format {last_id + 1}"
        return "Custom Format 1"

    @cache()
    def table_formulas(self, table_id: int):
        return TableFormulas(self, table_id)

    @cache(num_args=2)
    def table_rich_text(self, table_id: int, string_key: int) -> dict:
        """Extract bullets and hyperlinks from a rich text data cell."""
        # The table model base data store contains a richTextTable field
        # which is a reference to a TST.TableDataList. The TableDataList
        # has a list of payloads in a field called entries. This will be
        # empty if there is no rich text, i.e. text contents are plaintext.
        #
        # "entries": [
        #     { "key": 1,
        #       "refcount": 1,
        #       "richTextPayload": { "identifier": "2035264" }
        #     },
        #     ...
        #
        # entries[n].richTextPayload.identifier is a reference to a
        # TST.RichTextPayloadArchive that contains a field called storage
        # that itself is a reference to a TSWP.StorageArchive that contains
        # the actual paragraph data:
        #
        # "tableParaStyle": {
        #     "entries": [
        #         { "characterIndex": 0, "object": { "identifier": "1566948" } },
        #         { "characterIndex": 6 },
        #         { "characterIndex": 12 }
        #     ]
        # },
        # "text": [ "Lorem\nipsum\ndolor" ]
        #
        # The bullet character is stored in a TSWP.ListStyleArchive. Each bullet
        # paragraph can have its own reference to a list style or, if none is
        # defined, the previous bullet character is used. All StorageArchives
        # reference a ListStyleArchive but not all those ListStyleArchives have
        # a string with a new bullet character
        bds = self.objects[table_id].base_data_store
        rich_text_table = self.objects[bds.rich_text_table.identifier]
        for entry in rich_text_table.entries:  # pragma: no branch
            if string_key == entry.key:
                payload = self.objects[entry.rich_text_payload.identifier]
                payload_storage = self.objects[payload.storage.identifier]
                smartfield_entries = payload_storage.table_smartfield.entries
                cell_text = payload_storage.text[0] if len(payload_storage.text) > 0 else ""

                hyperlinks = []
                for i, e in enumerate(smartfield_entries):
                    if e.object.identifier:
                        obj = self.objects[e.object.identifier]
                        if isinstance(obj, TSWPArchives.HyperlinkFieldArchive):
                            start = e.character_index
                            if i < len(smartfield_entries) - 1:
                                end = smartfield_entries[i + 1].character_index
                            else:
                                end = len(cell_text)
                            url_text = cell_text[start:end]
                            hyperlinks.append((url_text, obj.url_ref))

                bullets = []
                bullet_chars = []
                payload_entries = payload_storage.table_para_style.entries
                table_list_styles = payload_storage.table_list_style.entries
                offsets = [e.character_index for e in payload_entries]
                for i, offset in enumerate(offsets):
                    if i == len(offsets) - 1:
                        bullets.append(cell_text[offset:])
                    else:
                        # Remove the last character (always newline)
                        bullets.append(cell_text[offset : offsets[i + 1] - 1])

                    # Re-use last style if there is none defined for this bullet
                    if i < len(table_list_styles):
                        table_list_style = table_list_styles[i]

                    bullet_style = self.objects[table_list_style.object.identifier]
                    if len(bullet_style.strings) > 0:
                        bullet_char = bullet_style.strings[0]
                    elif len(bullet_style.number_types) > 0:
                        number_type = bullet_style.number_types[0]
                        bullet_char = formatted_number(number_type, i)
                    else:
                        bullet_char = None

                    bullet_chars.append(bullet_char)

                return {
                    "text": cell_text,
                    "bulleted": any(c is not None for c in bullet_chars),
                    "bullets": bullets,
                    "bullet_chars": bullet_chars,
                    "hyperlinks": hyperlinks,
                }

    def cell_text_style(self, cell: Cell) -> object:
        """
        Return the text style object for the cell or, if none
        is defined, the default header, footer or body style.
        """
        if cell._text_style_id is not None:
            return self.table_style(cell._table_id, cell._text_style_id)
        return self.objects[self.default_text_style_id(cell)]

    def default_text_style_id(self, cell: Cell) -> int:
        """Return the id of the table's default text style for a cell's position."""
        table_model = self.objects[cell._table_id]
        field = "body_text_style"
        if cell.row in range(table_model.number_of_header_rows):
            field = "header_row_text_style"
        elif cell.col in range(table_model.number_of_header_columns):
            field = "header_column_text_style"
        elif table_model.number_of_footer_rows > 0:
            start_row_num = table_model.number_of_rows - table_model.number_of_footer_rows
            if cell.row in range(start_row_num, table_model.number_of_rows):
                field = "footer_row_text_style"
        return getattr(table_model, field).identifier

    def default_cell_style(self, cell: Cell) -> object | None:
        """
        Return the table's own default cell style for a cell's position, or
        None if the table has none: the header row, header column or footer
        row style, else the body style, chosen the same way as
        :meth:`cell_text_style`. Used as the fallback for a cell with no
        explicit per-cell style, in place of this library's generic constants.
        """
        table_model = self.objects[cell._table_id]
        field = "body_cell_style"
        if cell.row in range(table_model.number_of_header_rows):
            field = "header_row_style"
        elif cell.col in range(table_model.number_of_header_columns):
            field = "header_column_style"
        elif table_model.number_of_footer_rows > 0:
            start_row_num = table_model.number_of_rows - table_model.number_of_footer_rows
            if cell.row in range(start_row_num, table_model.number_of_rows):
                field = "footer_row_style"
        if not table_model.HasField(field):
            return None
        return self.objects[getattr(table_model, field).identifier]

    def cell_alignment(self, cell: Cell) -> Alignment:
        style = self.cell_text_style(cell)
        horizontal = HorizontalJustification(self.para_property(style, "alignment"))

        if cell._cell_style_id is None:
            body_style = self.default_cell_style(cell)
            if body_style is not None:
                vertical = VerticalJustification(
                    self.cell_property(body_style, "vertical_alignment"),
                )
            else:
                vertical = VerticalJustification.TOP
        else:
            style = self.table_style(cell._table_id, cell._cell_style_id)
            vertical = VerticalJustification(self.cell_property(style, "vertical_alignment"))
        return Alignment(horizontal, vertical)

    def cell_bg_color(self, cell: Cell) -> tuple | list[tuple]:
        if cell._cell_style_id is None:
            style = self.default_cell_style(cell)
            if style is None:
                return None
        else:
            style = self.table_style(cell._table_id, cell._cell_style_id)

        cell_properties = style.cell_properties.cell_fill

        if cell_properties.HasField("color"):
            return rgb(cell_properties.color)
        if cell_properties.HasField("gradient"):
            return [(rgb(s.color)) for s in cell_properties.gradient.stops]
        return None

    def char_property(self, style: object, field: str):
        """
        Return a char_property field from a style if present
        in the style, or from the parent if not. A root style (no parent)
        with the field unset falls back to the field's proto default.
        """
        if not style.char_properties.HasField(field) and style.super.HasField("parent"):
            parent = self.objects[style.super.parent.identifier]
            return getattr(parent.char_properties, field)
        return getattr(style.char_properties, field)

    def para_property(self, style: object, field: str) -> float:
        """
        Return a para_property field from a style if present
        in the style, or from the parent if not. A root style (no parent)
        with the field unset falls back to the field's proto default.
        """
        if not style.para_properties.HasField(field) and style.super.HasField("parent"):
            parent = self.objects[style.super.parent.identifier]
            return getattr(parent.para_properties, field)
        return getattr(style.para_properties, field)

    def cell_property(self, style: object, field: str) -> float:
        """
        Return a cell_property field from a style if present
        in the style, or from the parent if not. A root style (no parent)
        with the field unset falls back to the field's proto default.
        """
        if not style.cell_properties.HasField(field) and style.super.HasField("parent"):
            parent = self.objects[style.super.parent.identifier]
            return getattr(parent.cell_properties, field)
        return getattr(style.cell_properties, field)

    def cell_is_bold(self, obj: Cell | object) -> bool:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.char_property(style, "bold")

    def cell_is_italic(self, obj: Cell | object) -> bool:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.char_property(style, "italic")

    def cell_is_underline(self, obj: Cell | object) -> bool:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        underline = self.char_property(style, "underline")
        return underline != CharacterStyle.UnderlineType.kNoUnderline

    def cell_is_strikethrough(self, obj: Cell | object) -> bool:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        strikethru = self.char_property(style, "strikethru")
        return strikethru != CharacterStyle.StrikethruType.kNoStrikethru

    def cell_style_name(self, obj: Cell | object) -> bool:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        if not style.super.name and style.super.is_variation and style.super.HasField("parent"):
            # A cell-level override reports the name of the style it varies
            return self.objects[style.super.parent.identifier].super.name
        return style.super.name

    def cell_font_color(self, obj: Cell | object) -> tuple:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return rgb(self.char_property(style, "font_color"))

    def cell_font_size(self, obj: Cell | object) -> float:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.char_property(style, "font_size")

    def cell_font_family(self, obj: Cell | object) -> str:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        font_name = self.char_property(style, "font_name")
        if not font_name:
            # Unset on a root style with no parent to inherit from
            return DEFAULT_FONT
        if font_name not in FONT_MAP:
            if font_name not in self.missing_fonts:
                warn(
                    f"Custom font '{font_name}' unsupported; falling back to {DEFAULT_FONT}",
                    UnsupportedWarning,
                    stacklevel=2,
                )
                self.missing_fonts[font_name] = True
            return DEFAULT_FONT

        return FONT_MAP[font_name]["family"]

    def cell_font_details(self, obj: Cell | object) -> dict[str, str]:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        font_name = self.char_property(style, "font_name")
        if not font_name:
            return FONT_FAMILY_DEFAULT[DEFAULT_FONT]
        if font_name not in FONT_MAP:
            # Keep the stored PostScript name so saving an edited style does not
            # replace a font this library does not know with the default font.
            details = dict(FONT_FAMILY_DEFAULT[DEFAULT_FONT])
            details["name"] = font_name
            return details
        return FONT_MAP[font_name]

    def cell_first_indent(self, obj: Cell | object) -> float:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.para_property(style, "first_line_indent")

    def cell_baseline_shift(self, obj: Cell | object) -> float:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.char_property(style, "baseline_shift")

    def cell_line_spacing(self, obj: Cell | object) -> float | None:
        """
        Return the relative line spacing amount, or None if the style has none.

        Only relative spacing (a multiple of the line height) is reported. A style
        using exact, minimum, maximum or space-between spacing returns None, since
        its amount is in points and would not fit a single relative value.
        """
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        while style is not None:
            props = style.para_properties
            if props.line_spacing_null:
                return None
            if props.HasField("line_spacing"):
                if props.line_spacing.mode != TSWPArchives.LineSpacingArchive.kRelativeLineSpacing:
                    return None
                return props.line_spacing.amount
            if not style.super.HasField("parent"):
                return None
            style = self.objects[style.super.parent.identifier]
        return None

    def cell_left_indent(self, obj: Cell | object) -> float:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.para_property(style, "left_indent")

    def cell_right_indent(self, obj: Cell | object) -> float:
        style = self.cell_text_style(obj) if isinstance(obj, Cell) else obj
        return self.para_property(style, "right_indent")

    def cell_text_inset(self, cell: Cell) -> float:
        if cell._cell_style_id is None:
            body_style = self.default_cell_style(cell)
            if body_style is not None:
                return self.cell_property(body_style, "padding").left
            return DEFAULT_TEXT_INSET
        style = self.table_style(cell._table_id, cell._cell_style_id)
        padding = self.cell_property(style, "padding")
        # Padding is always identical (only one UI setting)
        return padding.left

    def cell_text_wrap(self, cell: Cell) -> float:
        if cell._cell_style_id is None:
            body_style = self.default_cell_style(cell)
            if body_style is not None:
                return self.cell_property(body_style, "text_wrap")
            return DEFAULT_TEXT_WRAP
        style = self.table_style(cell._table_id, cell._cell_style_id)
        return self.cell_property(style, "text_wrap")

    def stroke_type(self, stroke_run: object) -> str:
        """Return the stroke type for a stroke run."""
        stroke_type = stroke_run.stroke.pattern.type
        if stroke_type == StrokePattern.StrokePatternType.TSDSolidPattern:
            return "solid"
        if stroke_type == StrokePattern.StrokePatternType.TSDPattern:
            if stroke_run.stroke.pattern.pattern[0] < 1.0:
                return "dots"
            return "dashes"
        return "none"

    def cell_for_stroke(self, table_id: int, side: str, row: int, col: int) -> object:
        data = self._table_data[table_id]
        if row < 0 or col < 0:
            return None
        if row >= len(data) or col >= len(data[row]):
            return None
        cell = self._table_data[table_id][row][col]
        if isinstance(cell, MergedCell):
            if (
                (side == "top" and row == cell.row_start)
                or (side == "right" and col == cell.col_end)
                or (side == "bottom" and row == cell.row_end)
                or (side == "left" and col == cell.col_start)
            ):
                return cell
        elif cell.is_merged:
            # No borders for merged edges of anchor cells
            if (
                side in ["top", "left"]
                or (side == "right" and cell.size[1] == 1)
                or (side == "bottom" and cell.size[0] == 1)
            ):
                return cell
        else:
            return cell
        return None

    def set_cell_border(
        self,
        table_id: int,
        row: int,
        col: int,
        side: str,
        border_value: Border,
    ) -> None:
        """Set the 2 borders adjacent to a stroke if within the table range."""
        self._update_strokes[table_id] = True
        if side == "top":
            bottom_cell = self.cell_for_stroke(table_id, "bottom", row - 1, col)
            if (cell := self.cell_for_stroke(table_id, "top", row, col)) is not None:
                cell._border.top = border_value
            if bottom_cell is not None:
                bottom_cell._border.bottom = border_value
        elif side == "right":
            left_cell = self.cell_for_stroke(table_id, "left", row, col + 1)
            if (cell := self.cell_for_stroke(table_id, "right", row, col)) is not None:
                cell._border.right = border_value
            if left_cell is not None:
                left_cell._border.left = border_value
        elif side == "bottom":
            top_cell = self.cell_for_stroke(table_id, "top", row + 1, col)
            if (cell := self.cell_for_stroke(table_id, "bottom", row, col)) is not None:
                cell._border.bottom = border_value
            if top_cell is not None:
                top_cell._border.top = border_value
        else:  # left border
            right_cell = self.cell_for_stroke(table_id, "right", row, col - 1)
            if (cell := self.cell_for_stroke(table_id, "left", row, col)) is not None:
                cell._border.left = border_value
            if right_cell is not None:
                right_cell._border.right = border_value

    def extract_strokes_in_layers(
        self,
        table_id: int,
        layer_ids: list,
        side: str,
    ) -> list[tuple[int, int, int, int, str, Border]]:
        strokes = []
        for layer_id in layer_ids:
            stroke_layer = self.objects[layer_id.identifier]
            for stroke_run in stroke_layer.stroke_runs:
                border_value = Border(
                    width=round(stroke_run.stroke.width, 2),
                    color=rgb(stroke_run.stroke.color),
                    style=self.stroke_type(stroke_run),
                )
                if side in ["top", "bottom"]:
                    start_row = stroke_layer.row_column_index
                    start_column = stroke_run.origin
                    for col in range(start_column, start_column + stroke_run.length):
                        strokes.append(
                            (
                                stroke_run.order,
                                table_id,
                                start_row,
                                col,
                                side,
                                border_value,
                            ),
                        )
                else:
                    start_row = stroke_run.origin
                    start_column = stroke_layer.row_column_index
                    for row in range(start_row, start_row + stroke_run.length):
                        strokes.append(
                            (
                                stroke_run.order,
                                table_id,
                                row,
                                start_column,
                                side,
                                border_value,
                            ),
                        )
        return strokes

    @cache()
    def extract_strokes(self, table_id: int) -> None:
        table_obj = self.objects[table_id]
        stroke_sidecar_id = table_obj.stroke_sidecar.identifier
        if stroke_sidecar_id == 0:  # pragma: no cover
            return
        sidecar_obj = self.objects[stroke_sidecar_id]
        strokes = []
        strokes.extend(
            self.extract_strokes_in_layers(table_id, sidecar_obj.top_row_stroke_layers, "top"),
        )
        strokes.extend(
            self.extract_strokes_in_layers(table_id, sidecar_obj.left_column_stroke_layers, "left"),
        )
        strokes.extend(
            self.extract_strokes_in_layers(
                table_id,
                sidecar_obj.right_column_stroke_layers,
                "right",
            ),
        )
        strokes.extend(
            self.extract_strokes_in_layers(
                table_id,
                sidecar_obj.bottom_row_stroke_layers,
                "bottom",
            ),
        )
        for _, table_id, row, col, side, border_value in sorted(
            strokes,
            key=lambda value: value[0],
        ):
            self.set_cell_border(table_id, row, col, side, border_value)

    def load_borders_for_edit(self, table_id: int) -> None:
        """
        Load every cell's border from the stroke sidecar and mark the table so
        save rebuilds the sidecar from the cells' borders.

        Called before rows or columns are inserted or deleted: once borders are
        held on the cells, they move with the cells, and the rebuilt sidecar
        takes the table's new size.
        """
        self.extract_strokes(table_id)
        self._update_strokes[table_id] = True

    def _border_cell(self, table_id: int, side: str, row: int, col: int):
        cell = self.cell_for_stroke(table_id, side, row, col)
        return cell._border if cell is not None else None

    def prepare_borders_for_insert(self, table_id: int, axis: str, start: int) -> None:
        """
        Before rows or columns are inserted at ``start``, keep the line on the
        boundary there with the row or column that moves.

        A line between two rows is held on the lower row's top and mirrored on
        the upper row's bottom (likewise right and left for columns). The new
        rows go between the two, so the upper row's mirror is cleared and the
        line stays on the moving row's top only; otherwise the save would draw
        it on both sides of the new rows.
        """
        self.load_borders_for_edit(table_id)
        data = self._table_data[table_id]
        before, after = ("bottom", "top") if axis == "row" else ("right", "left")
        size = len(data) if axis == "row" else len(data[0])
        if not 0 < start < size:
            return
        for i in range(len(data[0]) if axis == "row" else len(data)):
            upper = (start - 1, i) if axis == "row" else (i, start - 1)
            lower = (start, i) if axis == "row" else (i, start)
            upper_border = self._border_cell(table_id, before, *upper)
            lower_border = self._border_cell(table_id, after, *lower)
            if upper_border is None:
                continue
            line = getattr(lower_border, after, None) if lower_border is not None else None
            if line is None:
                line = getattr(upper_border, before)
            setattr(upper_border, before, None)
            if lower_border is not None:
                setattr(lower_border, after, line)

    def prepare_borders_for_delete(self, table_id: int, axis: str, start: int, n: int) -> None:
        """
        Before rows or columns ``start`` to ``start + n - 1`` are deleted, set
        the borders of the rows or columns that become neighbours.

        Deleting from the middle joins the rows either side, so the upper row's
        bottom mirror takes the line below the deleted block. Deleting through
        the last row keeps the table's bottom edge on the new last row, and
        deleting from the first row keeps the top edge on the new first row.
        Columns follow the same rules with right and left.
        """
        self.load_borders_for_edit(table_id)
        data = self._table_data[table_id]
        before, after = ("bottom", "top") if axis == "row" else ("right", "left")
        size = len(data) if axis == "row" else len(data[0])
        end = min(start + n, size)
        if start == 0 and end == size:
            return

        def at(index: int, i: int) -> tuple:
            return (index, i) if axis == "row" else (i, index)

        for i in range(len(data[0]) if axis == "row" else len(data)):
            if end == size:
                # The new last row takes the old bottom edge
                edge = self._border_cell(table_id, before, *at(size - 1, i))
                target = self._border_cell(table_id, before, *at(start - 1, i))
                if target is not None:
                    setattr(target, before, getattr(edge, before) if edge is not None else None)
            elif start == 0:
                # The new first row takes the old top edge
                edge = self._border_cell(table_id, after, *at(0, i))
                target = self._border_cell(table_id, after, *at(end, i))
                if target is not None:
                    setattr(target, after, getattr(edge, after) if edge is not None else None)
            else:
                lower = self._border_cell(table_id, after, *at(end, i))
                line = getattr(lower, after) if lower is not None else None
                if line is None:
                    last_deleted = self._border_cell(table_id, before, *at(end - 1, i))
                    line = getattr(last_deleted, before) if last_deleted is not None else None
                upper = self._border_cell(table_id, before, *at(start - 1, i))
                if upper is not None:
                    setattr(upper, before, line)
                if lower is not None:
                    setattr(lower, after, line)

    def propagate_borders_into_inserted_rows(
        self,
        table_id: int,
        data: list,
        start_row: int,
        n_rows: int,
    ) -> None:
        """
        Confirmed directly: the stroke sidecar's own layers (read by
        extract_strokes(), which add_row()/add_column() now call before
        touching self._data at all) are only ever POPULATED by
        update_cell_borders() at SAVE time -- in-memory, before any save,
        they're simply empty, even right after set_cell_border() has been
        called. So there is nothing in the sidecar itself to read back
        from immediately after an insertion that happens in the same
        session a border was set in, before either has ever been saved.
        An earlier version of this fix tried reading the sidecar
        directly for exactly this reason and found it empty every time
        in the single-session case -- see git history for that
        abandoned attempt if useful context.

        The one thing that IS always populated immediately, regardless
        of a prior save, is each Cell's own _border attribute --
        set_cell_border() sets it directly, and it's what
        update_cell_borders()'s own save-time rebuild scans to
        reconstruct runs in the first place (see that method's own
        docstring): consecutive rows with an identical border value on
        a given side. That's the actual, general rule this needs to
        satisfy, not a proxy for the sidecar's own arithmetic -- so this
        checks it directly: if the row immediately before the inserted
        block and the row immediately after it (both real, untouched by
        the insertion) already agree on a border value, extend that
        value across the newly-inserted, otherwise border-less rows too,
        via set_cell_border() itself for the same mirroring/bookkeeping
        reasons given above.
        """
        if start_row == 0 or start_row + n_rows >= len(data):
            return
        before_row = data[start_row - 1]
        after_row = data[start_row + n_rows]
        for col in range(min(len(before_row), len(after_row))):
            for side in ("left", "right"):
                before_val = getattr(before_row[col]._border, side)
                after_val = getattr(after_row[col]._border, side)
                if before_val is not None and before_val == after_val:
                    for r in range(start_row, start_row + n_rows):
                        self.set_cell_border(table_id, r, col, side, before_val)

    def propagate_borders_into_inserted_columns(
        self,
        table_id: int,
        data: list,
        start_col: int,
        n_cols: int,
    ) -> None:
        """
        The column-axis mirror of propagate_borders_into_inserted_rows --
        see that method's own docstring for the full reasoning.
        """
        if start_col == 0 or start_col + n_cols >= len(data[0]):
            return
        after_col = start_col + n_cols
        for row_index, row in enumerate(data):
            for side in ("top", "bottom"):
                before_val = getattr(row[start_col - 1]._border, side)
                after_val = getattr(row[after_col]._border, side)
                if before_val is not None and before_val == after_val:
                    for c in range(start_col, after_col):
                        self.set_cell_border(table_id, row_index, c, side, before_val)

    def create_stroke(self, origin: int, length: int, border_value: Border):
        line_cap = TSDArchives.StrokeArchive.LineCap.ButtCap
        line_join = TSDArchives.LineJoin.MiterJoin
        if border_value.style == BorderType.SOLID:
            pattern = TSDArchives.StrokePatternArchive(
                type=StrokePattern.StrokePatternType.TSDSolidPattern,
                phase=0.0,
                count=0,
                pattern=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            )
        elif border_value.style == BorderType.DASHES:
            pattern = TSDArchives.StrokePatternArchive(
                type=StrokePattern.StrokePatternType.TSDPattern,
                phase=0.0,
                count=2,
                pattern=[2.0, 2.0, 0.0, 0.0, 0.0, 0.0],
            )
        elif border_value.style == BorderType.DOTS:
            pattern = TSDArchives.StrokePatternArchive(
                type=StrokePattern.StrokePatternType.TSDPattern,
                phase=0.0,
                count=2,
                pattern=[0.0001, 2.0, 0.0, 0.0, 0.0, 0.0],
            )
            line_cap = TSDArchives.StrokeArchive.LineCap.RoundCap
            line_join = TSDArchives.LineJoin.RoundJoin
        else:
            pattern = TSDArchives.StrokePatternArchive(
                type=StrokePattern.StrokePatternType.TSDEmptyPattern,
                phase=0.0,
                count=0,
                pattern=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            )

        color = TSPMessages.Color(
            model=TSPMessages.Color.rgb,
            rgbspace=TSPMessages.Color.srgb,
            r=border_value.color.r / 255,
            g=border_value.color.g / 255,
            b=border_value.color.b / 255,
            a=1.0,
        )
        width = border_value.width
        return TSTArchives.StrokeLayerArchive.StrokeRunArchive(
            origin=origin,
            length=length,
            order=1,  # When we re-save, strokes are optimized to be flat
            stroke=TSDArchives.StrokeArchive(
                color=color,
                width=width,
                cap=line_cap,
                join=line_join,
                miter_limit=4.0,
                pattern=pattern,
            ),
        )

    def clip_stroke_runs(self, stroke_layer: object, origin: int, length: int) -> None:
        """Trim or split existing stroke runs so none overlaps a new run's range."""
        end = origin + length
        clipped = []
        for stroke_run in stroke_layer.stroke_runs:
            run_end = stroke_run.origin + stroke_run.length
            if run_end <= origin or stroke_run.origin >= end:
                # No overlap with the new run
                clipped.append(self.copy_stroke(stroke_run))
            else:
                if stroke_run.origin < origin:
                    head = self.copy_stroke(stroke_run)
                    head.length = origin - stroke_run.origin
                    clipped.append(head)
                if run_end > end:
                    tail = self.copy_stroke(stroke_run)
                    tail.origin = end
                    tail.length = run_end - end
                    clipped.append(tail)
        clear_field_container(stroke_layer.stroke_runs)
        stroke_layer.stroke_runs.extend(clipped)

    def copy_stroke(self, stroke_run: object):
        """Return a standalone copy of a stroke run."""
        stroke_copy = TSTArchives.StrokeLayerArchive.StrokeRunArchive()
        stroke_copy.CopyFrom(stroke_run)
        return stroke_copy

    def reconcile_cell_borders(self, table_id: int, data: list) -> None:
        """
        Copy borders onto adjacent cells where set_cell_border() could not.

        A bottom border is stored both on its own cell and mirrored onto the next
        row's top; a right border is mirrored onto the next column's left. The
        mirror cannot happen if the adjacent row or column does not exist when the
        border is set, so repair any missing mirrors before the strokes are rebuilt.
        """
        num_rows = len(data)
        num_cols = len(data[0])

        for row in range(num_rows - 1):
            for col in range(num_cols):
                border = data[row][col]._border.bottom
                if border is None or data[row + 1][col]._border.top is not None:
                    continue
                if (cell := self.cell_for_stroke(table_id, "top", row + 1, col)) is not None:
                    cell._border.top = border

        for col in range(num_cols - 1):
            for row in range(num_rows):
                border = data[row][col]._border.right
                if border is None or data[row][col + 1]._border.left is not None:
                    continue
                if (cell := self.cell_for_stroke(table_id, "left", row, col + 1)) is not None:
                    cell._border.left = border

    def update_cell_borders(self, table_id: int, data: list) -> None:
        """Consolidate identical strokes and then generate stoke archives."""
        if table_id not in self._update_strokes:
            return

        num_rows = self.objects[table_id].number_of_rows
        num_cols = self.objects[table_id].number_of_columns

        # Bottom and right borders are only scanned at the closing edge of the table,
        # so any that never reached the adjacent cell must be mirrored across first
        self.reconcile_cell_borders(table_id, data)

        # Clear existing stroke layers to prepare for rebuilt, optimized strokes
        table_obj = self.objects[table_id]
        if table_obj.stroke_sidecar.identifier != 0:
            sidecar_obj = self.objects[table_obj.stroke_sidecar.identifier]
            # add_stroke() also sets these, but a table may have no strokes left
            sidecar_obj.row_count = num_rows
            sidecar_obj.column_count = num_cols
            clear_field_container(sidecar_obj.top_row_stroke_layers)
            clear_field_container(sidecar_obj.bottom_row_stroke_layers)
            clear_field_container(sidecar_obj.left_column_stroke_layers)
            clear_field_container(sidecar_obj.right_column_stroke_layers)

        # Horizontal strokes (all but bottom of table)
        for row in range(num_rows):
            col = 0
            while col < num_cols:
                border = data[row][col]._border.top
                if border is not None:
                    length = 1
                    while col + length < num_cols and data[row][col + length]._border.top == border:
                        length += 1
                    self.add_stroke(table_id, row, col, "top", border, length)
                    col += length
                else:
                    col += 1

        # Bottom horizontal stroke
        row = num_rows - 1
        col = 0
        while col < num_cols:
            border = data[row][col]._border.bottom
            if border is not None:
                length = 1
                while col + length < num_cols and data[row][col + length]._border.bottom == border:
                    length += 1
                self.add_stroke(table_id, row, col, "bottom", border, length)
                col += length
            else:
                col += 1

        # Vertical strokes (all but right hand side of table)
        for col in range(num_cols):
            row = 0
            while row < num_rows:
                border = data[row][col]._border.left
                if border is not None:
                    length = 1
                    while (
                        row + length < num_rows and data[row + length][col]._border.left == border
                    ):
                        length += 1
                    self.add_stroke(table_id, row, col, "left", border, length)
                    row += length
                else:
                    row += 1

        # Right hand side of table
        col = num_cols - 1
        row = 0
        while row < num_rows:
            border = data[row][col]._border.right
            if border is not None:
                length = 1
                while row + length < num_rows and data[row + length][col]._border.right == border:
                    length += 1
                self.add_stroke(table_id, row, col, "right", border, length)
                row += length
            else:
                row += 1

    def add_stroke(
        self,
        table_id: int,
        row: int,
        col: int,
        side: str,
        border_value: Border,
        length: int,
    ) -> None:
        table_obj = self.objects[table_id]
        sidecar_obj = self.objects[table_obj.stroke_sidecar.identifier]
        sidecar_obj.row_count = table_obj.number_of_rows
        sidecar_obj.column_count = table_obj.number_of_columns
        sidecar_obj.max_order = 2

        if side == "top":
            layer_ids = sidecar_obj.top_row_stroke_layers
            row_column_index = row
            origin = col
        elif side == "right":
            layer_ids = sidecar_obj.right_column_stroke_layers
            row_column_index = col
            origin = row
        elif side == "bottom":
            layer_ids = sidecar_obj.bottom_row_stroke_layers
            row_column_index = row
            origin = col
        else:  # left border
            layer_ids = sidecar_obj.left_column_stroke_layers
            row_column_index = col
            origin = row

        stroke_layer = None
        for layer_id in layer_ids:
            if self.objects[layer_id.identifier].row_column_index == row_column_index:
                stroke_layer = self.objects[layer_id.identifier]
        if stroke_layer is not None:
            self.clip_stroke_runs(stroke_layer, origin, length)
            stroke_layer.stroke_runs.append(self.create_stroke(origin, length, border_value))
            stroke_layer.stroke_runs.sort(key=lambda x: x.origin)
        else:
            stroke_layer_id, stroke_layer = self.objects.create_object_from_dict(
                "CalculationEngine",
                {
                    "row_column_index": row_column_index,
                },
                TSTArchives.StrokeLayerArchive,
            )
            stroke_layer.stroke_runs.append(self.create_stroke(origin, length, border_value))
            layer_ids.append(TSPMessages.Reference(identifier=stroke_layer_id))

    def store_image(self, data: bytes, filename: str) -> None:
        """Store image data in the file store."""
        stored_filename = f"Data/{filename}"
        if stored_filename in self.objects.file_store:
            msg = f"{filename}: image already exists in document"
            raise IndexError(msg)
        self.objects.file_store[stored_filename] = data

    def next_image_identifier(self):
        """Return the next available ID in the list of images in the document."""
        datas = self.objects[PACKAGE_ID].datas
        image_ids = [x.identifier for x in datas]
        # datas never appears to be an empty list (default themes include images)
        return max(image_ids) + 1

    @cache(num_args=0)
    def group_uuid_values(self):
        def cell_value_to_key(
            cell_value: TSCEArchives.CellValueArchive,
        ) -> str | int | bool | datetime:
            """Convert a CellValueArchive to a key."""
            cell_value_type = cell_value.cell_value_type
            if cell_value_type == CellValueType.STRING_TYPE:
                return cell_value.string_value.value
            if cell_value_type == CellValueType.NUMBER_TYPE:
                return cell_value.number_value.value
            if cell_value_type == CellValueType.BOOLEAN_TYPE:
                return cell_value.boolean_value.value
            if cell_value_type == CellValueType.DATE_TYPE:
                return _decode_date_format(
                    cell_value.date_value.format.date_time_format,
                    EPOCH + timedelta(seconds=cell_value.date_value.value),
                )
            return None

        return {
            NumbersUUID(self.objects[_id].group_uid): cell_value_to_key(
                self.objects[_id].group_cell_value,
            )
            for _id in self.find_refs("GroupNodeArchive")
        }

    @cache()
    def calculate_table_categories(self, table_id: int) -> tuple[dict[int, int], dict] | None:
        category_owner_id = self.objects[table_id].category_owner.identifier
        if not category_owner_id:
            self._table_categories_data[table_id] = None
            self._table_categories_row_mapper[table_id] = None
            return

        category_archive_id = self.objects[category_owner_id].group_by[0].identifier
        category_archive = self.objects[category_archive_id]
        if not category_archive.is_enabled:
            self._table_categories_data[table_id] = None
            self._table_categories_row_mapper[table_id] = None
            return

        table_info = self.objects[self.table_info_id(table_id)]
        category_order = self.objects[table_info.category_order.identifier]
        row_uid_map = self.objects[category_order.uid_map.identifier]

        group_uuids = self.group_uuid_values()
        row_uuid_to_offset = {
            NumbersUUID(uuid): row for row, uuid in enumerate(category_archive.row_uid_lookup.uuids)
        }
        row_uid_for_index = [
            NumbersUUID(row_uid_map.sorted_row_uids[i]) for i in row_uid_map.row_uid_for_index
        ]

        def parent_relationships(parent: NumbersUUID, children: list, group_parents: dict):
            for child in children:
                child_uuid = NumbersUUID(child.group_uid)
                group_parents[child_uuid] = parent
                if len(child.child) > 0:
                    parent_relationships(child_uuid, child.child, group_parents)

        group_parents = {}
        parent_relationships(None, category_archive.group_node_root.child, group_parents)

        row = 0
        row_mapper: dict[int, int] = {}
        nodes: dict[NumbersUUID, dict] = {}
        root_children: dict = {}
        stack: list[NumbersUUID | None] = []

        for uuid in row_uid_for_index:
            if uuid in group_uuids:
                parent = group_parents.get(uuid)
                nodes[uuid] = {"key": group_uuids[uuid], "children": {}, "rows": []}

                if parent is None:
                    root_children[nodes[uuid]["key"]] = nodes[uuid]
                else:
                    if parent not in nodes:
                        nodes[parent] = {"key": group_uuids[parent], "children": {}, "rows": []}
                    parent_node = nodes[parent]
                    parent_node["children"][nodes[uuid]["key"]] = nodes[uuid]

                while stack and stack[-1] != parent:
                    stack.pop()
                stack.append(uuid)
            else:
                mapped_row = row_uuid_to_offset[uuid]
                if stack:
                    nodes[stack[-1]]["rows"].append(self._table_data[table_id][mapped_row])

                row_mapper[row] = mapped_row
                row += 1

        def node_to_structure(node: dict):
            if not node["children"]:
                return node["rows"]
            out = {}
            for child_key, child_node in node["children"].items():
                out[child_key] = node_to_structure(child_node)
            return out

        self._table_categories_data[table_id] = {}
        for key, node in root_children.items():
            self._table_categories_data[table_id][key] = node_to_structure(node)

        self._table_categories_row_mapper[table_id] = {
            row: row_uuid_to_offset[uuid]
            for row, uuid in enumerate(
                uuid for uuid in row_uid_for_index if uuid not in group_uuids
            )
        }


def rgb(obj) -> RGB:
    """Convert a TSPArchives.Color into an RGB tuple."""
    return RGB(round(obj.r * 255), round(obj.g * 255), round(obj.b * 255))


def range_end(obj):
    """Select end range for a IndexSetArchive.IndexSetEntry."""
    if obj.HasField("range_end"):
        return obj.range_end
    return obj.range_begin


def formatted_number(number_type, index):
    """Returns the numbered index bullet formatted for different types."""
    bullet_char = BULLET_PREFIXES[number_type]
    bullet_char += BULLET_CONVERSION[number_type](index)
    bullet_char += BULLET_SUFFIXES[number_type]

    return bullet_char


def get_storage_buffers_for_row(
    storage_buffer: bytes,
    offsets: list,
    num_cols: int,
    has_wide_offsets: bool,
) -> list[bytes]:
    """
    Extract storage buffers for each cell in a table row.

    Args:
    ----
        storage_buffer:  cell_storage_buffer or cell_storage_buffer for a table row
        offsets: 16-bit cell offsets for a table row
        num_cols: number of columns in a table row
        has_wide_offsets: use 4-byte offsets rather than 1-byte offset

    Returns:
    -------
         data: list of bytes for each cell in a row, or None if empty

    """
    offsets = array("h", offsets).tolist()
    if has_wide_offsets:
        offsets = [o * 4 for o in offsets]

    data = []
    for col in range(num_cols):
        if col >= len(offsets):
            break

        start = offsets[col]
        if start < 0:
            data.append(None)
            continue

        if col == (len(offsets) - 1):
            end = len(storage_buffer)
        else:
            end = None
            # Find next positive offset
            for i, x in enumerate(offsets[col + 1 :]):
                if x >= 0:
                    end = offsets[col + i + 1]
                    break
            if end is None:
                end = len(storage_buffer)
        data.append(storage_buffer[start:end])

    return data


def clear_field_container(obj) -> None:
    """
    Remove all entries from a protobuf RepeatedCompositeFieldContainer
    in a portable fashion.
    """
    while len(obj) > 0:
        _ = obj.pop()


def field_references(obj: object) -> dict:
    """Return a dict of all fields in an object that are references to other objects."""
    return {
        x[0].name: {"identifier": getattr(obj, x[0].name).identifier}
        for x in obj.ListFields()
        if isinstance(getattr(obj, x[0].name), TSPMessages.Reference)
    }


def _set_message_field(message: object, path: tuple):
    """Return the sub-message at an attribute path if every step is a set field, else None."""
    for name in path:
        if name not in message.DESCRIPTOR.fields_by_name or not message.HasField(name):
            return None
        message = getattr(message, name)
    return message


def _set_drawable_parent(message: object, parent_id: int) -> None:
    """Point a drawable's parent reference, nested at any depth of ``super``, at parent_id."""
    while "parent" not in message.DESCRIPTOR.fields_by_name:
        if "super" not in message.DESCRIPTOR.fields_by_name:
            return
        message = message.super
    message.parent.identifier = parent_id


def _check_char_runs(runs: list, length: int) -> None:
    """Raise ValueError unless runs are (index, object id or None) pairs with increasing indices in the text."""
    last = -1
    for run in runs:
        if not isinstance(run, (tuple, list)) or len(run) != 2:
            msg = f"character style run {run!r} must be (character_index, object_id or None)"
            raise ValueError(msg)
        index, object_id = run
        if not isinstance(index, int) or isinstance(index, bool) or index <= last:
            msg = f"character style run index {index!r} must be an integer above {last}"
            raise ValueError(msg)
        if index >= max(length, 1):
            msg = f"character style run index {index} is past the end of the text ({length})"
            raise ValueError(msg)
        if object_id is not None and (
            not isinstance(object_id, int) or isinstance(object_id, bool)
        ):
            msg = f"character style run object {object_id!r} must be an object id or None"
            raise ValueError(msg)
        last = index
