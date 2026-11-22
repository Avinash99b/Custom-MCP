"""Authentication package: the single place that inspects credentials.

Nothing outside this package may read Authorization headers or session cookies.
"""

from __future__ import annotations
