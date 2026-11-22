"""Scoped plugin storage and core reserved-secret access."""

from __future__ import annotations

from typing import Any, cast

import pytest

from custom_mcp.storage.manager import (
    MAX_VALUE_BYTES,
    InvalidKeyError,
    InvalidPrefixError,
    InvalidSlugError,
    InvalidTtlError,
    InvalidValueError,
    ScopedStorage,
    StorageDecodeError,
    StorageManager,
    decode_value,
    encode_value,
    validate_key,
    validate_prefix,
    validate_slug,
    validate_ttl,
)
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis


@pytest.fixture
def manager(redis_sync: SyncRedis, redis_async: AsyncRedis) -> StorageManager:
    return StorageManager(redis_sync, redis_async)


# --- key / prefix / slug / ttl validation -----------------------------------


@pytest.mark.parametrize("key", ["a", "A1", "a.b-c_d:e", "core.api_key", "x" * 128])
def test_valid_keys(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize("key", ["", "x" * 129, "a*", "a b", "a/b", "a?b"])
def test_invalid_keys(key: str) -> None:
    with pytest.raises(InvalidKeyError):
        validate_key(key)


def test_non_string_key_rejected() -> None:
    with pytest.raises(InvalidKeyError):
        validate_key(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("prefix", ["", "core.", "a.b-c_d:e", "x" * 128])
def test_valid_prefixes(prefix: str) -> None:
    assert validate_prefix(prefix) == prefix


@pytest.mark.parametrize("prefix", ["x" * 129, "a*", "a?", "a["])
def test_invalid_prefixes(prefix: str) -> None:
    with pytest.raises(InvalidPrefixError):
        validate_prefix(prefix)


@pytest.mark.parametrize("slug", ["demo", "a", "hello-world", "x" * 40])
def test_valid_slugs(slug: str) -> None:
    assert validate_slug(slug) == slug


@pytest.mark.parametrize("slug", ["", "Demo", "hello world", "x" * 41, "-bad"])
def test_invalid_slugs(slug: str) -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug(slug)


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5, "60"])
def test_invalid_ttl(ttl: object) -> None:
    with pytest.raises(InvalidTtlError):
        validate_ttl(ttl)  # type: ignore[arg-type]


def test_valid_ttl() -> None:
    assert validate_ttl(None) is None
    assert validate_ttl(60) == 60


# --- value encoding ---------------------------------------------------------


def test_value_round_trip(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("k", {"a": [1, 2, None], "b": "x"})
    assert storage.get("k") == {"a": [1, 2, None], "b": "x"}


def test_missing_vs_stored_null(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("stored-null", None)
    assert storage.get("stored-null", "fallback") is None
    assert storage.get("absent", "fallback") == "fallback"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected(manager: StorageManager, bad: float) -> None:
    with pytest.raises(InvalidValueError):
        manager.scoped("demo").set("k", bad)


def test_non_serializable_value_rejected(manager: StorageManager) -> None:
    with pytest.raises(InvalidValueError):
        manager.scoped("demo").set("k", object())


def test_value_size_limit(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("ok", "a" * (MAX_VALUE_BYTES - 10))
    with pytest.raises(InvalidValueError):
        storage.set("too-big", "a" * MAX_VALUE_BYTES)


def test_encode_decode_helpers() -> None:
    assert decode_value(encode_value({"a": 1})) == {"a": 1}
    with pytest.raises(StorageDecodeError):
        decode_value("{not json")


def test_lone_surrogate_value_rejected(manager: StorageManager) -> None:
    with pytest.raises(InvalidValueError):
        manager.scoped("demo").set("surrogate", "\ud800")


def test_deeply_nested_value_rejected(manager: StorageManager) -> None:
    value: Any = []
    cursor = value
    for _ in range(100_000):
        nested: list[Any] = []
        cursor.append(nested)
        cursor = nested
    with pytest.raises(InvalidValueError):
        manager.scoped("demo").set("deep", value)


def test_decode_rejects_non_finite_and_deep() -> None:
    with pytest.raises(StorageDecodeError):
        decode_value("NaN")
    with pytest.raises(StorageDecodeError):
        decode_value("[" * 100_000 + "]" * 100_000)


def test_list_keys_filters_unexpected_scan_keys() -> None:
    class _StubRedis:
        def scan_iter(self, match: str | None = None) -> object:
            return iter(
                ["plugin:demo:data:a", "plugin:other:data:b", "plugin:demo:data:a"]
            )

    storage = ScopedStorage(cast("SyncRedis", _StubRedis()), "demo")
    assert storage.list_keys() == ["a"]


def test_bad_stored_json_raises(manager: StorageManager, redis_sync: SyncRedis) -> None:
    redis_sync.set("plugin:demo:data:corrupt", "not-json")
    with pytest.raises(StorageDecodeError):
        manager.scoped("demo").get("corrupt")


# --- reserved keys ----------------------------------------------------------


def test_reserved_read_allowed_write_denied(manager: StorageManager) -> None:
    storage = manager.scoped("demo", secret=True)
    assert storage.get("core.api_key", "absent") == "absent"
    with pytest.raises(PermissionError):
        storage.set("core.api_key", "x")
    with pytest.raises(PermissionError):
        storage.delete("core.api_key")


def test_allow_reserved_bypass(manager: StorageManager) -> None:
    storage = manager.scoped("demo", secret=True, allow_reserved=True)
    storage.set("core.custom", "v")
    assert storage.get("core.custom") == "v"
    assert storage.delete("core.custom") is True


async def test_core_secret_round_trip(manager: StorageManager) -> None:
    await manager.set_core_secret("demo", "core.api_key", "sekret")
    assert await manager.get_core_secret("demo", "core.api_key") == "sekret"
    assert await manager.secret_names("demo") == ["core.api_key"]
    assert manager.scoped("demo", secret=True).get("core.api_key") == "sekret"
    assert await manager.delete_core_secret("demo", "core.api_key") is True
    assert await manager.get_core_secret("demo", "core.api_key") is None


async def test_set_core_secret_requires_core_prefix(manager: StorageManager) -> None:
    with pytest.raises(InvalidKeyError):
        await manager.set_core_secret("demo", "api_key", "x")


# --- delete / ttl -----------------------------------------------------------


def test_delete_reports_existence(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("k", 1)
    assert storage.delete("k") is True
    assert storage.delete("k") is False


def test_ttl_applied(manager: StorageManager, redis_sync: SyncRedis) -> None:
    storage = manager.scoped("demo")
    storage.set("k", "v", ttl=60)
    ttl = redis_sync.ttl("plugin:demo:data:k")
    assert 0 < ttl <= 60


# --- listing / namespaces ---------------------------------------------------


def test_list_keys_sorted_and_prefixed(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("b", 1)
    storage.set("a", 2)
    storage.set("sub.x", 3)
    manager.scoped("demo", secret=True, allow_reserved=True).set("core.k", 4)
    assert storage.list_keys() == ["a", "b", "sub.x"]
    assert storage.list_keys("sub.") == ["sub.x"]


def test_namespace_isolation_between_slugs(manager: StorageManager) -> None:
    manager.scoped("foo").set("k", "foo-value")
    manager.scoped("foobar").set("k", "foobar-value")
    assert manager.scoped("foo").get("k") == "foo-value"
    assert manager.scoped("foobar").get("k") == "foobar-value"
    assert manager.scoped("foo").list_keys() == ["k"]
    assert manager.scoped("foobar").list_keys() == ["k"]


def test_data_and_secret_namespaces_separate(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    secrets = manager.scoped("demo", secret=True)
    storage.set("k", "data")
    assert secrets.get("k", "absent") == "absent"
    secrets.set("k", "secret")
    assert storage.get("k") == "data"


def test_invalid_slug_rejected(manager: StorageManager) -> None:
    with pytest.raises(InvalidSlugError):
        manager.scoped("Bad Slug")


def test_unknown_namespace_rejected(redis_sync: SyncRedis) -> None:
    with pytest.raises(ValueError):
        ScopedStorage(redis_sync, "demo", namespace="other")


# --- context / snapshot -----------------------------------------------------


def test_context_builds_plugin_context(manager: StorageManager) -> None:
    context = manager.context("demo", callback_url="https://x/mcp/demo/", log=lambda _l, _m: None)
    assert context.slug == "demo"
    assert context.callback_url == "https://x/mcp/demo/"
    assert isinstance(context.storage, ScopedStorage)
    assert isinstance(context.secrets, ScopedStorage)
    context.storage.set("k", 1)
    assert context.secrets.get("k", "absent") == "absent"


async def test_snapshot_excludes_ttl_keys(manager: StorageManager) -> None:
    storage = manager.scoped("demo")
    storage.set("persistent", {"a": 1})
    storage.set("transient", {"b": 2}, ttl=60)
    secrets = manager.scoped("demo", secret=True)
    secrets.set("token", "keep")
    await manager.set_core_secret("demo", "core.api_key", "cred")

    snapshot = await manager.snapshot("demo")
    assert snapshot.data == {"persistent": {"a": 1}}
    assert snapshot.secrets == {"token": "keep", "core.api_key": "cred"}
    assert await manager.data_names("demo") == ["persistent", "transient"]
