"""Tests for alchemist.architect.global_state."""

from alchemist.architect.global_state import (
    GlobalStrategy,
    classify_global,
    classify_globals,
)


def test_const_array_becomes_const_table():
    r = classify_global("crc_table", "uint32_t [256]", is_const=True)
    assert r.strategy == GlobalStrategy.const_table
    assert "[u32; 256]" in r.rust_type


def test_init_function_lazy_init():
    r = classify_global(
        "crc_table", "uint32_t [256]",
        has_init_function=True, is_mutated_after_init=False,
    )
    assert r.strategy == GlobalStrategy.lazy_init
    assert "LazyLock" in r.rust_type


def test_mutable_counter_becomes_atomic():
    r = classify_global(
        "call_count", "int",
        is_mutated_after_init=True,
    )
    assert r.strategy == GlobalStrategy.atomic
    assert "Atomic" in r.rust_type


def test_mutable_array_becomes_arc_mutex():
    r = classify_global(
        "shared_buf", "uint8_t [1024]",
        is_mutated_after_init=True,
    )
    assert r.strategy == GlobalStrategy.arc_mutex
    assert "Mutex" in r.rust_type


def test_extern_becomes_opaque():
    r = classify_global("handle", "void *", is_extern=True)
    assert r.strategy == GlobalStrategy.opaque_extern
    assert "c_void" in r.rust_type


def test_simple_const_scalar():
    r = classify_global("BASE", "uint32_t", is_const=True)
    assert r.strategy == GlobalStrategy.const_table


def test_classify_globals_batch():
    globals_list = [
        {"name": "table", "type": "uint32_t [256]", "is_const": True},
        {"name": "count", "type": "int", "is_mutated_after_init": True},
    ]
    results = classify_globals(globals_list)
    assert len(results) == 2
    assert results[0].strategy == GlobalStrategy.const_table
    assert results[1].strategy == GlobalStrategy.atomic


def test_ownership_decision_dict():
    r = classify_global("crc_table", "uint32_t [256]", is_const=True)
    d = r.as_ownership_decision()
    assert "c_pattern" in d
    assert "rust_pattern" in d
    assert "rationale" in d
