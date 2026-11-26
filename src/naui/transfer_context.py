"""Per-worker transfer cancellation context for metadata/authentication I/O."""
from contextvars import ContextVar

CURRENT_TRANSFER = ContextVar('naui_transfer_control', default=None)
