"""Gaffer — a Fantasy Premier League squad engine.

The engine runs to completion and writes two files: `data/latest.json`, which is
the contract everything downstream reads, and `data/report.html`, the same run
as a standalone page. It is not a server and holds no state between runs beyond
the SQLite history it accumulates.
"""
__version__ = "0.1.0"
