"""Alias module.

The canonical implementation lives in ``src.memory_vector``. This file exists so the
``services.memory.memory_vector`` import path keeps working; both paths resolve to the *same*
module object, so there is exactly one copy of the code to maintain.
"""
import sys as _sys

from src import memory_vector as _impl

_sys.modules[__name__] = _impl
