import pytest

from numbers_parser import UnsupportedError
from numbers_parser.numbers_uuid import NumbersUUID, derive_table_identity_uuid


def test_uuid():
    uuid = NumbersUUID()
    assert str(uuid).count("-") == 4

    uuid = NumbersUUID(0xFF00FF00EE00EE00DD00DD00CC00BB00)
    assert uuid.hex == "ff00ff00ee00ee00dd00dd00cc00bb00"
    uuid = NumbersUUID("12345678000000001234567811111111")
    assert uuid.int == 0x12345678000000001234567811111111

    assert NumbersUUID(uuid.protobuf4).int == uuid.int
    assert NumbersUUID(uuid.protobuf4).protobuf2.lower == 0x1234567811111111
    assert NumbersUUID(uuid.protobuf2).int == uuid.int
    assert NumbersUUID(uuid.protobuf2).protobuf4.uuid_w0 == 0x11111111

    ref = {"uuid_w0": 0x1234, "uuid_w1": 0xFFFF, "uuid_w2": 0, "uuid_w3": 0x1111}
    uuid = NumbersUUID(ref)
    assert uuid.hex == "00001111000000000000ffff00001234"
    assert uuid.dict4 == ref

    ref = {"upper": 0x1234, "lower": 0xFFFF}
    uuid = NumbersUUID(ref)
    assert uuid.hex == "0000000000001234000000000000ffff"
    assert uuid.dict2 == ref

    with pytest.raises(UnsupportedError) as e:
        _ = NumbersUUID({"a": 1, "b": 2})
    assert str(e.value) == "Unsupported UUID dict structure"

    with pytest.raises(UnsupportedError) as e:
        _ = NumbersUUID(3.14)
    assert str(e.value) == "Unsupported UUID init type float"


# -- Table-identity UUID derivation ("adoption" fix) --------------------------
#
# Numbers.app does not trust a freshly minted uuid1 as a table's kind-1
# (TABLE_MODEL) formula-owner identity: on first open it silently retires
# ("adopts") any kind-1 owner that isn't registered under one specific
# derived value, severing the table's formula dependency edges. That value
# is the byte-reversal of the table's own TableModelArchive.table_id (a
# string field) -- not an arithmetic derivation from anything else. These
# two table_id/derived pairs are recorded, byte for byte, in
# `Claude chat handoff notes/session_2026-08-28_artifacts/
# fresh_eyes_findings_2026-08-28.md`, "Result (round 4)", from round-trip
# experiments against real Numbers.app output (tables 3 and 4 of the "29"
# test document pair).


def test_derive_table_identity_uuid_matches_recorded_numbers_output():
    # Table 3: table_id string -> Numbers' own kind-1 owner UUID.
    table_id_uuid = NumbersUUID("e2aa6e20a29411f1a2a7c51fb862aae7")
    derived = derive_table_identity_uuid(table_id_uuid)
    assert derived.hex == "e7aa62b81fc5a7a2f11194a2206eaae2"

    # Table 4: same rule, different table_id -- only the low byte of
    # time_low (and therefore, after reversal, the low byte of the derived
    # value) differs.
    table_id_uuid = NumbersUUID("e2aa6e35a29411f1a2a7c51fb862aae7")
    derived = derive_table_identity_uuid(table_id_uuid)
    assert derived.hex == "e7aa62b81fc5a7a2f11194a2356eaae2"


def test_derive_table_identity_uuid_is_byte_exact_reversal():
    # Independent re-implementation of the rule (reverse all 16 bytes, no
    # arithmetic), so this doesn't just assert the production function
    # agrees with itself.
    table_id_uuid = NumbersUUID()
    expected = bytes(reversed(table_id_uuid.bytes))

    derived = derive_table_identity_uuid(table_id_uuid)
    assert derived.bytes == expected

    # The transform must never be re-derived by round-tripping through a
    # uuid.UUID hex/string form -- assert the dict2 split matches a direct
    # byte slice of the same raw value.
    assert derived.dict2 == {
        "upper": int.from_bytes(expected[:8], "big"),
        "lower": int.from_bytes(expected[8:], "big"),
    }


def test_derive_table_identity_uuid_does_not_mutate_or_apply_any_arithmetic():
    # Regression guard for the superseded "decrement time_low's low byte"
    # rule: a table_id whose low time_low byte is 0x00 must reverse to a
    # derived value with 0x00 in the corresponding (last) position, not
    # wrap to 0xFF the way the old, coincidentally-correct rule did.
    table_id_uuid = NumbersUUID("00000000a29411f1a2a7c51fb862aae7")
    derived = derive_table_identity_uuid(table_id_uuid)
    assert derived.bytes[-1] == 0x00
    assert derived.hex == "e7aa62b81fc5a7a2f11194a200000000"

    # table_id_uuid itself must be untouched (no in-place byte mutation).
    assert table_id_uuid.hex == "00000000a29411f1a2a7c51fb862aae7"
