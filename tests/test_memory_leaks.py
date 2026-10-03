import gc
import json
import logging
from sys import version_info

import pytest
from pympler import muppy, summary

from numbers_parser import Document


@pytest.fixture(name="quiet_logger")
def quiet_logger_fixture():
    # Tests that run the CLIs in-process with --debug leave the numbers_parser
    # logger at DEBUG. pytest then keeps every debug record emitted while this
    # test loads documents, which looks like a leak, so pin the level here.
    logger = logging.getLogger("numbers_parser")
    level = logger.level
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(level)


@pytest.mark.usefixtures("quiet_logger")
def test_memory_leaks(tmp_path):
    """Memory leak test (see issue-67)."""
    if version_info < (3, 11):
        return

    # Each iteration's summary is written to a file rather than kept in memory,
    # so the snapshot used to explain a failure is not itself counted.
    snapshot = tmp_path / "summary.json"
    last_num_objects = None
    last_num_bytes = None
    iterations = 10
    for ii in range(iterations):
        doc = Document("tests/data/test-1.numbers")
        del doc
        gc.collect()

        interim_summary = summary.summarize(muppy.get_objects())
        num_objects = sum(r[1] for r in interim_summary)
        num_bytes = sum(r[2] for r in interim_summary)

        if ii > 1 and (num_objects > last_num_objects or num_bytes > last_num_bytes):
            grown = _grown_types(json.loads(snapshot.read_text()), interim_summary)
            assert num_objects - last_num_objects <= 0, f"iteration {ii}: {grown}"
            assert num_bytes - last_num_bytes <= 0, f"iteration {ii}: {grown}"

        snapshot.write_text(json.dumps(interim_summary))
        del interim_summary
        last_num_objects = num_objects
        last_num_bytes = num_bytes


def _grown_types(before: list, after: list) -> list:
    """Return (type, objects, bytes) for each type that grew between two summaries."""
    diff = summary.get_diff(before, after)
    return [(row[0], row[1], row[2]) for row in diff if row[1] > 0 or row[2] > 0]
