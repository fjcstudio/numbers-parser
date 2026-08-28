from uuid import UUID, uuid1

from numbers_parser.exceptions import UnsupportedError
from numbers_parser.generated import TSPMessages_pb2 as TSPMessages


class NumbersUUID(UUID):
    def __init__(self, uuid=None) -> None:
        if uuid is None:
            super().__init__(int=uuid1().int)
        elif isinstance(uuid, int):
            super().__init__(int=uuid)
        elif isinstance(uuid, str):
            super().__init__(hex=uuid)
        elif isinstance(uuid, TSPMessages.UUID):
            uuid_int = uuid.upper << 64 | uuid.lower
            super().__init__(int=uuid_int)
        elif isinstance(uuid, TSPMessages.CFUUIDArchive):
            uuid_int = (
                (uuid.uuid_w3 << 96) | (uuid.uuid_w2 << 64) | (uuid.uuid_w1 << 32) | uuid.uuid_w0
            )
            super().__init__(int=uuid_int)
        elif isinstance(uuid, dict):
            if "uuid_w0" in uuid and "uuid_w1" in uuid:
                uuid_int = (
                    (int(uuid["uuid_w3"]) << 96)
                    | (int(uuid["uuid_w2"]) << 64)
                    | (int(uuid["uuid_w1"]) << 32)
                    | int(uuid["uuid_w0"])
                )
                super().__init__(int=uuid_int)
            elif "upper" in uuid and "lower" in uuid:
                uuid_int = int(uuid["upper"]) << 64 | int(uuid["lower"])
                super().__init__(int=uuid_int)
            else:
                msg = "Unsupported UUID dict structure"
                raise UnsupportedError(msg)
        else:
            msg = f"Unsupported UUID init type {type(uuid).__name__}"
            raise UnsupportedError(msg)

    @property
    def dict2(self) -> dict:
        upper = self.int >> 64
        lower = self.int & 0xFFFFFFFFFFFFFFFF
        return {"upper": upper, "lower": lower}

    @property
    def dict4(self) -> object:
        uuid_w3 = self.int >> 96
        uuid_w2 = (self.int >> 64) & 0xFFFFFFFF
        uuid_w1 = (self.int >> 32) & 0xFFFFFFFF
        uuid_w0 = self.int & 0xFFFFFFFF
        return {
            "uuid_w3": uuid_w3,
            "uuid_w2": uuid_w2,
            "uuid_w1": uuid_w1,
            "uuid_w0": uuid_w0,
        }

    @property
    def protobuf2(self) -> object:
        upper = self.int >> 64
        lower = self.int & 0xFFFFFFFFFFFFFFFF
        return TSPMessages.UUID(upper=upper, lower=lower)

    @property
    def protobuf4(self) -> object:
        uuid_w3 = self.int >> 96
        uuid_w2 = (self.int >> 64) & 0xFFFFFFFF
        uuid_w1 = (self.int >> 32) & 0xFFFFFFFF
        uuid_w0 = self.int & 0xFFFFFFFF
        return TSPMessages.CFUUIDArchive(
            uuid_w3=uuid_w3,
            uuid_w2=uuid_w2,
            uuid_w1=uuid_w1,
            uuid_w0=uuid_w0,
        )


def uuid_to_hex(archive: object) -> str:
    """Convert a protobuf UUID to a hex string"""
    uuid = NumbersUUID(archive)
    return uuid.hex


def derive_table_identity_uuid(table_id_uuid: NumbersUUID) -> NumbersUUID:
    """
    Derive the table-identity ("kind-1") owner UUID Numbers.app expects for a
    table, from that table's own ``TableModelArchive.table_id`` value.

    ``table_id`` is a plain ``string`` field holding a textual UUID -- the
    table's real, persistent identity. It is invisible to any scan that only
    walks binary UUID-typed fields (``TSP.UUID`` / ``CFUUIDArchive``), which
    is exactly why it went unnoticed for three investigation sessions before
    being found. Numbers.app parses that string and expects the kind-1
    (``TABLE_MODEL``) formula owner's UUID to hold the *same 16 bytes*, but in
    Apple's own half-order convention: the byte-for-byte reversal of every
    binary form ``NumbersUUID`` emits. On first open, finding a kind-1 owner
    that does not hold this value, Numbers.app silently retires ("adopts")
    it and rebuilds the table's identity -- and every reference into the
    table that named it under the old identity -- from scratch, under the
    value derived here. See
    ``Claude chat handoff notes/session_2026-08-28_artifacts/fresh_eyes_findings_2026-08-28.md``,
    "Result (round 4): the actual mechanism, one line", for the experiment
    that pinned this down.

    ``table_id_uuid`` must be the exact same ``NumbersUUID`` written into
    this table's own ``table_id`` string (``str(table_id_uuid).upper()``) --
    not an independently minted value. There is no arithmetic here beyond
    a byte reversal: given ``table_id_uuid``'s 16 bytes in standard
    big-endian field order, the derived value is those same 16 bytes in
    reverse order. Verified against Numbers' own output for two independent
    tables:

        table_id E2AA6E20-A294-11F1-A2A7-C51FB862AAE7
                -> e7aa62b81fc5a7a2f11194a2206eaae2
        table_id E2AA6E35-A294-11F1-A2A7-C51FB862AAE7
                -> e7aa62b81fc5a7a2f11194a2356eaae2

    An earlier version of this function took an unrelated, independently
    minted "family base" instead of ``table_id_uuid``, and additionally
    decremented that base's low time_low byte before reversing. That rule
    reproduced the correct value only by coincidence: CPython's ``uuid1()``
    fallback path deterministically bumps a same-process, back-to-back
    second call to exactly one clock tick past the first (see cpython's
    ``uuid.py``, the ``_last_timestamp`` global), and the caller minted the
    "family base" immediately after minting ``table_id`` -- so subtracting
    one from the second mint landed back on the first mint's value, i.e.
    ``table_id`` itself, every time it was tried in this sandbox. That is an
    accident of one Python implementation's internals, not a property of
    the file format: on a platform where ``uuid1()`` takes the system
    ``uuid_generate_time_safe`` fast path instead of that fallback (see the
    same stdlib module), or if any other ``uuid1()``-consuming call is ever
    inserted between the two mints, the "minus one" arithmetic stops landing
    on ``table_id`` and the fix silently breaks without changing any test
    result in this repository. Reading ``table_id`` directly removes that
    fragility: there is no second mint whose timing has to stay in step
    with anything.

    The result must be written byte-exactly (raw bytes, not re-derived by
    round-tripping through ``uuid.UUID`` string/hex forms) into every site
    that carries this identity.

    Do not apply this to the auxiliary owner kinds (3, 4, 5, 6, 8, 9, 10, 11,
    12, 35) -- Numbers keeps those under the library's plain ``base+kind``
    values regardless, and file 40/41 of the findings above show mixing in
    the derived scheme there is unnecessary and untested.
    """
    return NumbersUUID(int.from_bytes(bytes(reversed(table_id_uuid.bytes)), "big"))
