"""Storage backends.

`Store` (in `base`) is the interface the rest of Maestro talks to; `SQLiteStore` is the default
implementation and the only one today. A PostgreSQL backend (#34) will be a second subclass, so
anything backend-independent belongs in `base` or `logic`, not in a backend module.
"""

from maestro.storage.base import ACK_WINDOW, SchemaTooNewError, Store
from maestro.storage.factory import (
    StoreConfigError,
    Target,
    driver_errors,
    open_store,
    parse_url,
)
from maestro.storage.sqlite import MIGRATIONS, SQLiteStore

__all__ = ["ACK_WINDOW", "MIGRATIONS", "SQLiteStore", "SchemaTooNewError", "Store", "StoreConfigError",
           "Target", "driver_errors", "open_store", "parse_url"]
