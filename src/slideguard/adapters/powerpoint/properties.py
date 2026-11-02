"""Optional COM-property reads that keep unknown values distinct from measured ones."""

import math
from collections.abc import Callable

_UNAVAILABLE_HRESULTS = {0x80004001, 0x80020003, 0x80020006}


def read_optional(read: Callable, label: str, notes: list[str]):
    try:
        return read()
    except (AttributeError, NotImplementedError):
        pass
    except Exception as error:
        hresult = getattr(error, "hresult", None)
        if type(hresult) is not int or hresult & 0xFFFFFFFF not in _UNAVAILABLE_HRESULTS:
            raise
    notes.append(f"{label} is unavailable; no value was inferred")
    return None


def number(value, label: str, notes: list[str], *, minimum: float | None = None) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and (minimum is None or value >= minimum):
        return float(value)
    notes.append(f"{label} is unknown or invalid")
    return None


def tristate(value, label: str, notes: list[str]) -> bool | None:
    if type(value) is int and value in (-1, 0):
        return value == -1
    notes.append(f"{label} is mixed or unknown")
    return None
