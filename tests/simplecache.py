# Test-only stub for script.module.simplecache.
# Real simplecache provides on-disk caching for Kodi addons; our tests just
# need import-time correctness, so all methods are no-ops.
class SimpleCache:
    def __init__(self, *a, **kw):
        pass

    def get(self, *a, **kw):
        return None

    def set(self, *a, **kw):
        pass

    def check_cleanup(self, *a, **kw):
        pass

    def close(self, *a, **kw):
        pass

    def __getattr__(self, name):
        return lambda *a, **kw: None
