"""Tests for the post-architect trait extractor."""

from __future__ import annotations

from alchemist.architect.schemas import (
    CrateArchitecture,
    CrateSpec,
    TraitSpec,
)
from alchemist.architect.trait_extractor import (
    _Shape,
    _shape_for,
    extract_traits,
)
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
)


def _checksum(name: str) -> AlgorithmSpec:
    return AlgorithmSpec(
        name=name, display_name=name, category="checksum",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="u32",
    )


def _hash(name: str) -> AlgorithmSpec:
    return AlgorithmSpec(
        name=name, display_name=name, category="hash",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="Vec<u8>",
    )


def _arch_with_crate(crate_name: str, module_names: list[str]) -> CrateArchitecture:
    return CrateArchitecture(
        workspace_name="test",
        description="",
        crates=[CrateSpec(
            name=crate_name, description="", modules=module_names,
        )],
    )


def test_shape_for_returns_none_on_no_inputs():
    alg = AlgorithmSpec(
        name="f", display_name="", category="checksum",
        description="", inputs=[], return_type="u32",
    )
    assert _shape_for(alg) is None


def test_shape_groups_identical_signatures():
    a = _checksum("adler32")
    b = _checksum("crc32")
    assert _shape_for(a) == _shape_for(b)


def test_shape_distinguishes_categories():
    a = _checksum("adler32")
    h = _hash("sha256")
    # Return type differs → different shape
    assert _shape_for(a) != _shape_for(h)


def test_shape_distinguishes_return_types():
    a = AlgorithmSpec(
        name="a", display_name="", category="checksum",
        description="",
        inputs=[Parameter(name="i", rust_type="&[u8]", description="")],
        return_type="u32",
    )
    b = AlgorithmSpec(
        name="b", display_name="", category="checksum",
        description="",
        inputs=[Parameter(name="i", rust_type="&[u8]", description="")],
        return_type="u64",
    )
    assert _shape_for(a) != _shape_for(b)


def test_extract_emits_checksum_trait():
    mod = ModuleSpec(
        name="checksums", display_name="", description="",
        algorithms=[_checksum("adler32"), _checksum("crc32")],
    )
    arch = _arch_with_crate("zlib-checksum", ["checksums"])
    traits = extract_traits([mod], arch)
    assert len(traits) == 1
    t = traits[0]
    assert t.name == "Checksum"
    assert t.crate == "zlib-checksum"
    assert set(t.implementors) == {"adler32", "crc32"}


def test_extract_skips_solo_family():
    mod = ModuleSpec(
        name="m", display_name="", description="",
        algorithms=[_checksum("adler32")],  # only one
    )
    arch = _arch_with_crate("zlib-checksum", ["m"])
    traits = extract_traits([mod], arch, min_implementors=2)
    assert traits == []


def test_extract_skips_existing_traits():
    mod = ModuleSpec(
        name="m", display_name="", description="",
        algorithms=[_checksum("adler32"), _checksum("crc32")],
    )
    arch = CrateArchitecture(
        workspace_name="t", description="",
        crates=[CrateSpec(name="zlib-checksum", description="", modules=["m"])],
        traits=[TraitSpec(
            name="Checksum",
            description="already declared",
            methods=[],
            crate="zlib-checksum",
        )],
    )
    traits = extract_traits([mod], arch)
    assert traits == []


def test_extract_uses_hasher_name_for_hash_category():
    mod = ModuleSpec(
        name="m", display_name="", description="",
        algorithms=[_hash("sha256"), _hash("md5")],
    )
    arch = _arch_with_crate("zlib-hash", ["m"])
    traits = extract_traits([mod], arch)
    assert len(traits) == 1
    assert traits[0].name == "Hasher"


def test_extract_groups_across_modules():
    m1 = ModuleSpec(
        name="adler", display_name="", description="",
        algorithms=[_checksum("adler32")],
    )
    m2 = ModuleSpec(
        name="crc", display_name="", description="",
        algorithms=[_checksum("crc32")],
    )
    arch = _arch_with_crate("zlib-checksum", ["adler", "crc"])
    traits = extract_traits([m1, m2], arch)
    assert len(traits) == 1
    assert set(traits[0].implementors) == {"adler32", "crc32"}
