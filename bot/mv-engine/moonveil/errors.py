"""Public errors shared by runtime discovery and recovery stages."""


class MoonVeilError(RuntimeError):
    """Raised when an input cannot be processed safely."""


class MoonVeilTimeoutError(MoonVeilError):
    """A Luau subprocess exceeded its allotted time."""
