"""Errors are deliberately distinct from firmware business assertions."""


class LisemError(RuntimeError):
    """Base SDK error."""


class CompatibilityError(LisemError):
    """The installed application or worker lacks this native contract."""


class ProtocolError(LisemError):
    """Malformed or oversized native RPC response."""


class Rejected(LisemError):
    """The backend explicitly rejected the operation; no automatic retry."""


class StaleRun(Rejected):
    """The handle belongs to a different boot/reset round."""


class RunEnded(LisemError):
    """The run ended before the expected observation or input completion."""


class OperationTimeout(LisemError, TimeoutError):
    """A bounded observation did not meet its condition."""


class ObservationTimeout(OperationTimeout):
    """The total observation deadline expired; inspect observation and cause."""


class DeliveryUnknown(LisemError):
    """A mutation may have reached the backend; never blindly replay it."""


class HistoryLost(LisemError):
    """A required history interval is missing."""


class CleanupError(LisemError):
    """Owned resources could not be confirmed stopped or evidence saved."""


def preserve_exception(original, cleanup):
    if original is None:
        raise cleanup
    # Python 3.10 has no add_note; keep the error inspectable there as well.
    original.lisem_cleanup_error = cleanup
    if hasattr(original, "add_note"):
        original.add_note(f"Lisem cleanup: {cleanup}")
    else:
        import warnings
        warnings.warn(f"Lisem cleanup: {cleanup}", RuntimeWarning)
