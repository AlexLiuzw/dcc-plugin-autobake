"""Vendored third-party add-ons, byte-identical to their pinned archives.

Each child package carries a ``VENDOR-MANIFEST.json`` recording the upstream
commit, archive hash and the sha256 of every copied file, plus the licence
it was taken under. Nothing in this tree may be edited: the adapter layer
(``agent/tools``, ``agent/_addon.py``) is where changes belong, and
``tests/test_vendor_integrity.py`` re-hashes the tree against the manifest.
"""

from __future__ import annotations

__all__ = []
