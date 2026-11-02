"""Alias module.

The canonical implementation lives in ``src.search.query``. This file exists so the
``services.search.query`` import path keeps working; both paths resolve to the *same*
module object, so there is exactly one copy of the code to maintain.
"""
import sys as _sys

from src.search import query as _impl

_sys.modules[__name__] = _impl
