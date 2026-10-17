import typing

try:
    import sqlalchemy.util.typing

    def _patched_make_union_type(*types):
        return typing.Union[types]

    sqlalchemy.util.typing.make_union_type = _patched_make_union_type
except Exception:
    pass
