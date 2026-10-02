import gc
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
def test_memory_leaks():
    """Memory leak test (see issue-67)."""
    if version_info < (3, 11):
        return

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

        if ii > 1:
            assert num_objects - last_num_objects <= 0
            assert num_bytes - last_num_bytes <= 0

        last_num_objects = num_objects
        last_num_bytes = num_bytes
