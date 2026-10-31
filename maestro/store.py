"""Compatibility entry point for `from maestro.store import Store`.

The store was one SQLite class until the storage interface landed (#34). `Store` here still means
"open a SQLite database", so existing code and scripts keep working. New code should import from
`maestro.storage`: the abstract `Store` to annotate what it needs, `SQLiteStore` to open one.
"""

from maestro.storage.base import ACK_WINDOW, SchemaTooNewError
from maestro.storage.sqlite import MIGRATIONS
from maestro.storage.sqlite import SQLiteStore as Store

__all__ = ["ACK_WINDOW", "MIGRATIONS", "SchemaTooNewError", "Store"]
