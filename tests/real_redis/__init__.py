"""Isolated real-Redis integration tests.

Each test starts a dedicated ``redis-server`` on a private unix socket in an
owned temp directory; the shared or pre-existing Redis on ``localhost:6379`` is
never contacted, imported from, or flushed.
"""
