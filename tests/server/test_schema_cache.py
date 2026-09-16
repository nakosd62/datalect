"""
schema_cache.py is a small, dependency-free in-memory cache with NO TTL/
expiry concept at all - an entry set via set() stays exactly as it was
until invalidate()/clear() drops it, or the process restarts. No app
import needed, just exercise it directly.
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


def _fresh_module():
    for mod_name in ("schema_cache",):
        sys.modules.pop(mod_name, None)
    import schema_cache
    schema_cache.clear()
    return schema_cache


def test_get_missing_key_returns_none():
    cache = _fresh_module()
    assert cache.get("nope") is None


def test_set_then_get_returns_value():
    cache = _fresh_module()
    cache.set("k1", "some schema text")
    assert cache.get("k1") == "some schema text"


def test_entry_never_expires_on_its_own():
    # No TTL to wait out - a cached entry is returned unchanged no matter
    # how much (real or simulated) time passes; only invalidate()/clear()
    # ever removes it.
    cache = _fresh_module()
    cache.set("k1", "schema text")
    for _ in range(3):
        assert cache.get("k1") == "schema text"


def test_set_again_replaces_the_cached_value():
    cache = _fresh_module()
    cache.set("k1", "old text")
    cache.set("k1", "new text")
    assert cache.get("k1") == "new text"


def test_invalidate_drops_entry():
    cache = _fresh_module()
    cache.set("k1", "schema text")
    cache.invalidate("k1")
    assert cache.get("k1") is None


def test_invalidate_missing_key_is_a_no_op():
    cache = _fresh_module()
    cache.invalidate("never-set")  # must not raise


def test_clear_drops_everything():
    cache = _fresh_module()
    cache.set("k1", "a")
    cache.set("k2", "b")
    cache.clear()
    assert cache.get("k1") is None
    assert cache.get("k2") is None
