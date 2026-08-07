"""Typed exceptions raised by vmkit.

Library code raises these; callers (CLI, REST API, bulk provisioner) map them to
exit codes / HTTP statuses / per-row failure reasons. The library never calls
sys.exit, print, or getpass.

Two classes carry a second base for backward compatibility. ``TaskFailedError``
and ``NotFoundError`` also subclass ``RuntimeError`` because that is what the
corresponding failures used to raise — a caller with ``except RuntimeError``
around a workflow keeps working, while new callers can catch the specific type.
Do not remove the dual inheritance without a major version bump.
"""


class VmkitError(Exception):
    """Base class for all vmkit errors."""


class ValidationError(VmkitError, ValueError):
    """An input value failed validation."""


class AuthenticationError(VmkitError):
    """ESXi/vCenter login failed (bad username or password)."""


class ConnectionFailedError(VmkitError):
    """Could not connect to the ESXi/vCenter host."""


class HostUnreachableError(ConnectionFailedError):
    """The host did not answer: DNS failure, refused connection, or timeout.

    Distinguished from its parent so a bulk caller can tell "this target is down"
    (retry, or fail the whole batch early) from "this target rejected us".
    """


class TlsError(ConnectionFailedError):
    """The TLS handshake failed — certificate rejected, or a protocol mismatch.

    Usually means ``verify_tls=True`` against a host with a self-signed
    certificate.
    """


class PermissionDeniedError(ConnectionFailedError):
    """The account authenticated but lacks the privilege for the operation.

    Raised where ESXi reports ``vim.fault.NoPermission``. Consumers previously had
    to walk ``__cause__``/``__context__`` looking for that fault by hand.
    """


class NotFoundError(VmkitError, RuntimeError):
    """A named inventory object does not exist."""


class VmNotFoundError(NotFoundError):
    """The requested VM does not exist in inventory."""


class DatastoreNotFoundError(NotFoundError):
    """The requested datastore does not exist on the host."""


class DatacenterNotFoundError(NotFoundError):
    """The host has no datacenter."""


class VmExistsError(VmkitError):
    """A VM with the requested name already exists."""


class InsufficientSpaceError(VmkitError):
    """The operation would exceed the datastore free-space limit."""


class TaskFailedError(VmkitError, RuntimeError):
    """A vSphere task finished in a non-success state.

    This is the most common real failure — a disk copy, a register, a power-on —
    and it used to escape the typed hierarchy entirely as a bare ``RuntimeError``.

    ``label`` names the step ("VMDK copy"), and ``fault`` carries the raw
    ``vim.fault`` object when the task reported one, so a caller can classify
    further without re-parsing the message.
    """

    def __init__(self, message: str, *, label: str | None = None, fault=None):
        super().__init__(message)
        self.label = label
        self.fault = fault


class TaskTimeoutError(TaskFailedError):
    """A vSphere task exceeded the caller's timeout and was asked to cancel.

    Distinct from a task that failed on its own: the task may still be running on
    the host. Cancellation is best-effort — ESXi refuses it for tasks that are not
    cancelable, in which case the task runs to completion unobserved.
    """


class TaskCancelledError(VmkitError):
    """A vSphere task was cancelled because the caller's ``should_cancel`` said so.

    Not a failure of the operation — the operator asked to stop.
    """
