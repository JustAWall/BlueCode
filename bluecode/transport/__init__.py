"""Transport backends. Only RFCOMM for now; keep the import surface small."""

from .base import Link
from . import rfcomm

__all__ = ["Link", "rfcomm"]
