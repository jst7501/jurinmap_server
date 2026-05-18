"""
Stock routes entrypoint.
Implementation is split into multiple part files under `stocks_parts/`
and loaded in-order into this module namespace.
"""

from .stocks_parts.loader import load_split_parts


load_split_parts(globals())

del load_split_parts
