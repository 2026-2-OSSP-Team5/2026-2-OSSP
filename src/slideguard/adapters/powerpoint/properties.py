"""Optional COM-property reads that keep unknown values distinct from measured ones."""

import math
from collections.abc import Callable

_UNAVAILABLE_HRESULTS = {0x80004001, 0x80020003, 0x80020006}


def com_hresult(error: Exception) -> int | None:
    """Office often wraps the useful SCODE in DISP_E_EXCEPTION's EXCEPINFO."""
    value = getattr(error, "hresult", None)
    if type(value) is not int:
        return None
    value &= 0xFFFFFFFF
    info = getattr(error, "excepinfo", None)
    if value == 0x80020009 and isinstance(info, tuple) and len(info) > 5 and type(info[5]) is int:
        return info[5] & 0xFFFFFFFF
    return value


def indexed_property(obj, name: str, *indices):
    """Read Office's indexed PROPERTYGET without calling its returned object.

    Late-bound TextRange2.Runs is a property, unlike PowerPoint TextRange.Runs.
    The regular Python-call path remains available for plain test implementations.
    """
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return getattr(obj, name)(*indices)
    from win32com.client import Dispatch

    result = ole.Invoke(ole.GetIDsOfNames(name), 0, 2, True, *indices)
    return Dispatch(result) if hasattr(result, "Invoke") else result


def read_optional(read: Callable, label: str, notes: list[str], *, unavailable: tuple[int, ...] = ()):
    try:
        return read()
    except (AttributeError, NotImplementedError):
        pass
    except Exception as error:
        if com_hresult(error) not in _UNAVAILABLE_HRESULTS and com_hresult(error) not in unavailable:
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
