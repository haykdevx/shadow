"""Alias module.

The canonical implementation lives in ``src.memory``. This file exists so the
``services.memory.memory`` import path keeps working; both paths resolve to the *same*
module object, so there is exactly one copy of the code to maintain.
"""
import sys as _sys

from src import memory as _impl

_sys.modules[__name__] = _impl
