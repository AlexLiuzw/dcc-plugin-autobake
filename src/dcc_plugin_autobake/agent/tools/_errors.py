"""Structured tool errors — stdlib only.

The sidecar rebuilds these into the northbound error envelope, so the
``code`` values are part of this plugin's public contract: callers switch on
them instead of parsing prose. Keep them stable; add new codes rather than
renaming existing ones.
"""

from __future__ import annotations


class ToolError(Exception):
    """A tool refused to run, or the host refused the operation.

    ``code`` is the machine-readable reason; ``context`` carries the details
    an operator needs to act on it (object name, path, upstream operator).
    """

    code = "tool_error"

    def __init__(self, message, code=None, context=None):
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        self.context = dict(context or {})

    def as_payload(self):
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


class InvalidArgs(ToolError):
    """The caller's arguments do not describe a usable request."""

    code = "invalid_args"


class HostNotAvailable(ToolError):
    """The tool is running outside a Blender host (no ``bpy``)."""

    code = "host_not_available"


__all__ = ["HostNotAvailable", "InvalidArgs", "ToolError"]
