"""Picking a solver without pinning ourselves to a deprecated one.

PuLP ships CBC under two names: the bundled `PULP_CBC_CMD`, which is deprecated
and goes away in PuLP 4.0, and `COIN_CMD`, which wraps a CBC installed
alongside. Preferring the latter when it exists means the engine keeps working
through that removal instead of breaking on an upgrade nobody was watching for.
"""
from __future__ import annotations

import functools

import pulp

PREFERRED = ("COIN_CMD", "PULP_CBC_CMD")


@functools.lru_cache(maxsize=1)
def _solver_name() -> str:
    available = set(pulp.listSolvers(onlyAvailable=True))
    for name in PREFERRED:
        if name in available:
            return name
    raise RuntimeError(
        "No mixed-integer solver available. Install one with `pip install pulp[cbc]`."
    )


def build(time_limit: int):
    """A configured solver instance, quiet and bounded by a time limit."""
    return pulp.getSolver(_solver_name(), msg=False, timeLimit=time_limit)
