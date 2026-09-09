"""goat-code deterministic script layer.

Every module here is stdlib-only and runs identically on Windows, macOS and
Linux. The agents call this package through ``scripts/goatcode.py``.
"""

#: Kept in step with .claude-plugin/plugin.json; a test pins them together.
__version__ = "0.3.0"
