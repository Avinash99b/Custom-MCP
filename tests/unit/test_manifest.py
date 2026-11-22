"""Manifest parsing and validation rules."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from custom_mcp.plugins.manifest import Manifest, ManifestError, parse_manifest
from mcp_plugin_sdk import AuthMode


def manifest_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "demo",
        "version": "1.0.0",
        "entry": "plugin:Demo",
        "auth_modes": ["none"],
    }
    data.update(overrides)
    return data


def manifest_bytes(**overrides: Any) -> bytes:
    return json.dumps(manifest_dict(**overrides)).encode("utf-8")


def test_valid_manifest_defaults() -> None:
    manifest = Manifest.from_dict(manifest_dict())
    assert manifest.name == "demo"
    assert manifest.version == "1.0.0"
    assert manifest.entry == "plugin:Demo"
    assert manifest.auth_modes == (AuthMode.NONE,)
    assert manifest.display_name == "demo"
    assert manifest.description == ""
    assert manifest.min_core_version is None
    assert manifest.author is None
    assert manifest.homepage is None


def test_entry_accessors() -> None:
    manifest = Manifest.from_dict(manifest_dict(entry="pkg.sub:MyClass"))
    assert manifest.entry_module == "pkg.sub"
    assert manifest.entry_class == "MyClass"


def test_to_dict_round_trip() -> None:
    manifest = Manifest.from_dict(
        manifest_dict(
            display_name="Demo",
            description="A demo",
            author="A",
            homepage="https://example.com",
            min_core_version="0.1.0",
            auth_modes=["api_key", "oauth"],
        )
    )
    payload = manifest.to_dict()
    assert payload["auth_modes"] == ["api_key", "oauth"]
    assert payload["display_name"] == "Demo"
    assert Manifest.from_dict(payload) == manifest


def test_manifest_is_frozen() -> None:
    manifest = Manifest.from_dict(manifest_dict())
    with pytest.raises(FrozenInstanceError):
        manifest.name = "other"  # type: ignore[misc]


def test_unknown_keys_are_ignored() -> None:
    manifest = Manifest.from_dict(manifest_dict(future_field="ok"))
    assert manifest.name == "demo"


@pytest.mark.parametrize("name", ["a", "abc-123", "0-9", "a" * 40])
def test_valid_slugs(name: str) -> None:
    assert Manifest.from_dict(manifest_dict(name=name)).name == name


@pytest.mark.parametrize(
    "name",
    ["", "A", "Abc", "-abc", "abc_", "a" * 41, "a.b", "a b"],
)
def test_invalid_slugs(name: str) -> None:
    with pytest.raises(ManifestError, match="slug"):
        Manifest.from_dict(manifest_dict(name=name))


def test_missing_name() -> None:
    data = manifest_dict()
    del data["name"]
    with pytest.raises(ManifestError, match="missing required field 'name'"):
        Manifest.from_dict(data)


def test_non_string_name() -> None:
    with pytest.raises(ManifestError, match="'name' must be a string"):
        Manifest.from_dict(manifest_dict(name=123))


@pytest.mark.parametrize("version", ["0.0.0", "1.2.3", "10.20.30"])
def test_valid_versions(version: str) -> None:
    assert Manifest.from_dict(manifest_dict(version=version)).version == version


@pytest.mark.parametrize(
    "version",
    [
        "01.0.0",
        "1.02.0",
        "1.0",
        "1.0.0.0",
        "1.0.0-rc1",
        "v1.0.0",
        "\uff11.0.0",
        "\u0661.0.0",
        "1.0.\u0660",
    ],
)
def test_invalid_versions(version: str) -> None:
    with pytest.raises(ManifestError, match="version"):
        Manifest.from_dict(manifest_dict(version=version))


@pytest.mark.parametrize("entry", ["plugin:Demo", "pkg.sub:MyClass", "_a._b:C9"])
def test_valid_entries(entry: str) -> None:
    assert Manifest.from_dict(manifest_dict(entry=entry)).entry == entry


@pytest.mark.parametrize(
    "entry",
    [
        "plugin",
        "plugin:",
        ":Demo",
        "1bad:Demo",
        "a:b:c",
        "pkg.-bad:Demo",
        "a.b-c:Demo",
        "class:Thing",
        "pkg.class:Thing",
        "pkg:class",
        "import:Thing",
    ],
)
def test_invalid_entries(entry: str) -> None:
    with pytest.raises(ManifestError, match="entry"):
        Manifest.from_dict(manifest_dict(entry=entry))


@pytest.mark.parametrize(
    "modes",
    [["none"], ["api_key"], ["oauth"], ["api_key", "oauth"]],
)
def test_valid_auth_modes(modes: list[str]) -> None:
    manifest = Manifest.from_dict(manifest_dict(auth_modes=modes))
    assert [mode.value for mode in manifest.auth_modes] == modes


@pytest.mark.parametrize(
    "modes",
    [
        [],
        "none",
        [123],
        ["bad"],
        ["none", "oauth"],
        ["api_key", "api_key"],
    ],
)
def test_invalid_auth_modes(modes: object) -> None:
    with pytest.raises(ManifestError, match="auth"):
        Manifest.from_dict(manifest_dict(auth_modes=modes))


def test_min_core_version_supported() -> None:
    assert Manifest.from_dict(manifest_dict(min_core_version="0.1.0")).min_core_version == "0.1.0"


@pytest.mark.parametrize("version", ["0.1.1", "1.0.0", "0.2.0"])
def test_min_core_version_too_new(version: str) -> None:
    with pytest.raises(ManifestError, match="requires core version"):
        Manifest.from_dict(manifest_dict(min_core_version=version), core_version="0.1.0")


def test_min_core_version_bad_format() -> None:
    with pytest.raises(ManifestError, match="min_core_version"):
        Manifest.from_dict(manifest_dict(min_core_version="0.1"))


def test_display_name_and_description_types() -> None:
    with pytest.raises(ManifestError, match="display_name"):
        Manifest.from_dict(manifest_dict(display_name=5))
    with pytest.raises(ManifestError, match="description"):
        Manifest.from_dict(manifest_dict(description=5))


def test_author_homepage_types() -> None:
    with pytest.raises(ManifestError, match="author"):
        Manifest.from_dict(manifest_dict(author=5))
    with pytest.raises(ManifestError, match="homepage"):
        Manifest.from_dict(manifest_dict(homepage=5))


def test_non_object_root_from_dict() -> None:
    with pytest.raises(ManifestError, match="root must be a JSON object"):
        Manifest.from_dict([1, 2, 3])  # type: ignore[arg-type]


def test_parse_manifest_valid() -> None:
    manifest = parse_manifest(manifest_bytes())
    assert manifest.name == "demo"


def test_parse_manifest_non_object_root() -> None:
    with pytest.raises(ManifestError, match="root must be a JSON object"):
        parse_manifest(b'["nope"]')


def test_parse_manifest_invalid_json() -> None:
    with pytest.raises(ManifestError, match="not valid JSON"):
        parse_manifest(b"{not json")


def test_parse_manifest_invalid_utf8() -> None:
    with pytest.raises(ManifestError, match="not valid UTF-8"):
        parse_manifest(b"\xff\xfe")


def test_parse_manifest_duplicate_keys() -> None:
    data = b'{"name": "a", "name": "b", "version": "1.0.0", "entry": "p:D", "auth_modes": ["none"]}'
    with pytest.raises(ManifestError, match="duplicate manifest key"):
        parse_manifest(data)


def test_parse_manifest_requires_bytes() -> None:
    with pytest.raises(ManifestError, match="must be bytes"):
        parse_manifest("not-bytes")  # type: ignore[arg-type]


def test_huge_version_component_accepted() -> None:
    huge = "9" * 5000
    manifest = Manifest.from_dict(manifest_dict(version=f"{huge}.0.0"))
    assert manifest.version == f"{huge}.0.0"


def test_huge_min_core_version_rejected() -> None:
    huge = "9" * 5000
    with pytest.raises(ManifestError, match="requires core version"):
        Manifest.from_dict(
            manifest_dict(min_core_version=f"{huge}.0.0"), core_version="0.1.0"
        )


def test_deeply_nested_manifest_rejected() -> None:
    nested = ("[" * 100_000) + "]" * 100_000
    with pytest.raises(ManifestError, match="too deeply nested"):
        parse_manifest(nested.encode("utf-8"))
