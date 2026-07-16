"""Sparkedge ERCOT -- implied heat rate & spark spread monitor for the ERCOT (Texas) power market."""

from .config import HUBS, UNIT_CLASSES, SETTINGS, Settings
from .storage import Storage
from .data import DataService
from .compute import Analytics

__version__ = "0.1.0"

__all__ = [
    "HUBS",
    "UNIT_CLASSES",
    "SETTINGS",
    "Settings",
    "Storage",
    "DataService",
    "Analytics",
    "__version__",
]
