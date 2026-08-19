"""
schema_cache.py is a small, dependency-free TTL cache - no app import
needed, just exercise it directly. Uses monkeypatched time.monotonic() so
TTL expiry is deterministic instead of sleeping in real time.
"""

import sys
import types

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


@pytest.fixture
def cache_module(monkeypatch):
    for mod_name in ("schema_cache",):
        sys.modules.pop(mod_name, None)
    monkeypatch.delenv("SCHEMA_CACHE_TTL_SECONDS", raising=False)
    import schema_cache
    schema_cache.clear()
    yield schema_cache
    schema_cache.clear()


def test_get_missing_key_returns_none(cache_module):
    assert cache_module.get("nope") is None


def test_set_then_get_returns_value(cache_module):
    cache_module.set("k1", "some schema text")
    assert cache_module.get("k1") == "some schema text"


def test_entry_expires_after_ttl(cache_module, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: fake_now[0])

    cache_module.set("k1", "schema text", ttl_seconds=10)
    assert cache_module.get("k1") == "schema text"

    fake_now[0] += 11
    assert cache_module.get("k1") is None


def test_entry_still_valid_just_before_ttl(cache_module, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: fake_now[0])

    cache_module.set("k1", "schema text", ttl_seconds=10)
    fake_now[0] += 9
    assert cache_module.get("k1") == "schema text"


def test_invalidate_drops_entry(cache_module):
    cache_module.set("k1", "schema text")
    cache_module.invalidate("k1")
    assert cache_module.get("k1") is None


def test_invalidate_missing_key_is_a_no_op(cache_module):
    cache_module.invalidate("never-set")  # must not raise


def test_clear_drops_everything(cache_module):
    cache_module.set("k1", "a")
    cache_module.set("k2", "b")
    cache_module.clear()
    assert cache_module.get("k1") is None
    assert cache_module.get("k2") is None
