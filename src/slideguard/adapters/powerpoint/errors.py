"""Plain Python failures at the PowerPoint read-session boundary."""


class PowerPointSessionError(RuntimeError):
    def __init__(self, operation: str, detail: str, *, hresult: int | None = None, attempts: int = 1):
        self.operation = operation
        self.hresult = hresult
        self.attempts = attempts
        self.cleanup_errors: tuple[PowerPointSessionError, ...] = ()
        code = f" (HRESULT 0x{hresult:08X})" if hresult is not None else ""
        super().__init__(f"PowerPoint {operation} failed{code}: {detail}")


class PowerPointUnavailableError(PowerPointSessionError):
    """The platform or pywin32 runtime is unavailable."""


class PowerPointNotInstalledError(PowerPointUnavailableError):
    """PowerPoint's COM class is not registered; Office may need installation or repair."""


class PowerPointConnectionError(PowerPointSessionError):
    """The calling thread could not initialize COM or connect to PowerPoint."""


class PowerPointOpenError(PowerPointSessionError):
    """The supplied temporary PPTX copy could not be opened."""


class PowerPointExtractionError(PowerPointSessionError):
    """Extraction failed or returned data outside the snapshot contract."""


class PowerPointBusyError(PowerPointSessionError):
    """A known transient COM failure exhausted the attempt limit."""


class PowerPointCleanupError(PowerPointSessionError):
    """The owned presentation or the thread's COM initialization could not be released."""
