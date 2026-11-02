"""Alias module.

The canonical implementation lives in ``src.search.analytics``. This file exists so the
``services.search.analytics`` import path keeps working; both paths resolve to the *same*
module object, so there is exactly one copy of the code to maintain.
"""
import sys as _sys

from src.search import analytics as _impl

_sys.modules[__name__] = _impl
