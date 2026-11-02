"""Alias module.

The canonical implementation lives in ``src.youtube_handler``. This file exists so the
``services.youtube.youtube_handler`` import path keeps working; both paths resolve to the *same*
module object, so there is exactly one copy of the code to maintain.
"""
import sys as _sys

from src import youtube_handler as _impl

_sys.modules[__name__] = _impl
