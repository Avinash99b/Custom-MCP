"""Self-contained malicious-archive security fixtures for package validation.

This package deliberately depends only on the Python standard library so it can
be imported while the ``custom_mcp`` / ``mcp_plugin_sdk`` implementation is still
in flux.  See :mod:`tests.security_cases.archive_cases` for the fixture API.
"""
