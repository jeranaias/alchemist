"""Tests for the build-utility function classifier."""

from __future__ import annotations

import pytest

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    FunctionSpec,
    ModuleSpec,
    Parameter,
)
from alchemist.extractor.function_classifier import (
    classify_function,
    filter_build_utilities,
)


def _make_fnspec(name: str = "foo", purpose: str = "") -> FunctionSpec:
    return FunctionSpec(name=name, purpose=purpose, category="utility")


def _make_alg(name: str, description: str = "", category: str = "checksum") -> AlgorithmSpec:
    return AlgorithmSpec(
        name=name,
        display_name=name,
        category=category,
        description=description,
    )


# ---------- classify_function ----------

def test_main_is_classified_as_main():
    assert classify_function("main", _make_fnspec("main")) == "main"


def test_write_table_header_is_build_utility():
    spec = _make_fnspec("write_table_header", purpose="Writes CRC table to a header file")
    assert classify_function("write_table_header", spec) == "build_utility"


def test_gen_header_is_build_utility():
    spec = _make_fnspec("gen_crc_header", purpose="Generates header")
    assert classify_function("gen_crc_header", spec) == "build_utility"


def test_test_function_is_test_harness():
    spec = _make_fnspec("test_deflate")
    assert classify_function("test_deflate", spec) == "test_harness"


def test_example_function_is_test_harness():
    spec = _make_fnspec("example_compress")
    assert classify_function("example_compress", spec) == "test_harness"


def test_codegen_purpose_is_build_utility():
    spec = _make_fnspec("make_trees", purpose="Generates C source for static Huffman trees")
    assert classify_function("make_trees", spec) == "build_utility"


def test_writes_to_file_purpose_is_build_utility():
    spec = _make_fnspec("dump_table", purpose="Writes to file the CRC lookup table")
    assert classify_function("dump_table", spec) == "build_utility"


def test_normal_algorithm_is_algorithm():
    spec = _make_fnspec("adler32", purpose="Computes the Adler-32 checksum of a byte buffer")
    assert classify_function("adler32", spec) == "algorithm"


def test_gen_without_header_is_algorithm():
    """gen_ prefix alone is not enough — must also contain 'header'."""
    spec = _make_fnspec("gen_bitlen", purpose="Generate bit lengths for Huffman codes")
    assert classify_function("gen_bitlen", spec) == "algorithm"


# ---------- filter_build_utilities ----------

def test_filter_removes_build_and_test_keeps_algorithm():
    module = ModuleSpec(
        name="crc",
        display_name="CRC",
        description="CRC module",
        algorithms=[
            _make_alg("crc32", "Compute CRC-32 checksum"),
            _make_alg("main"),
            _make_alg("test_crc"),
            _make_alg("make_crc_table", description="Generates C source for CRC table"),
        ],
    )
    filtered = filter_build_utilities([module])
    assert len(filtered) == 1
    names = [a.name for a in filtered[0].algorithms]
    assert "crc32" in names
    assert "main" not in names
    assert "test_crc" not in names
    assert "make_crc_table" not in names


def test_filter_drops_empty_modules():
    module = ModuleSpec(
        name="tools",
        display_name="Tools",
        description="Build tools",
        algorithms=[_make_alg("main")],
    )
    filtered = filter_build_utilities([module])
    assert len(filtered) == 0
