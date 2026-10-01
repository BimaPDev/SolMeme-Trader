"""Per-platform features (Axiom, fomo.family, j7tracker, ...).

Each module here registers features with `@feature(...)` from solmeme.features.registry,
reading `state.ext[<source name>]` (filled from that source's Enrichment / signal data).
Modules are auto-imported, so strategies can reference them by name.
"""

import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_m.name}")
