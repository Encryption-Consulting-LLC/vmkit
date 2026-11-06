"""vmkit — ESXi/vCenter VM automation library.

High-level entry points (params in, exceptions out, no TTY/printing):

    from vmkit import open_connection, clone_workflow, update_workflow, destroy_workflow

Every entry point takes an explicit ``Connection``, so one process can drive many
ESXi targets. In a long-lived process use ``Connection`` as a context manager (or
``close_connection``) and pass ``register_atexit=False``, or sessions accumulate
until the process exits.

Low-level building blocks live in vmkit.esxi / vmkit.datastore / vmkit.vmx.
"""

from vmkit.errors import (
    AuthenticationError,
    ConnectionFailedError,
    DatacenterNotFoundError,
    DatastoreFileNotFoundError,
    DatastoreNotFoundError,
    HostUnreachableError,
    InsufficientSpaceError,
    NotFoundError,
    PermissionDeniedError,
    TaskCancelledError,
    TaskFailedError,
    TaskTimeoutError,
    TlsError,
    ValidationError,
    VmExistsError,
    VmkitError,
    VmNotFoundError,
)
from vmkit.esxi import (
    DatastoreInfo,
    HostCapacity,
    NetworkInfo,
    VmInfo,
    disconnect,
    get_host_capacity,
    list_base_vms,
    list_datastores,
    list_networks,
    list_vm_names,
    list_vms,
)
from vmkit.progress import (
    Phase,
    ProgressCallback,
    ProgressEvent,
)
from vmkit.vmx import DEFAULT_GUEST_OS, DEFAULT_NETWORK, random_mac
from vmkit.workflows import (
    CapacityPlan,
    CleanupResult,
    CloneResult,
    Connection,
    CreateResult,
    DestroyResult,
    UpdateResult,
    VmSpec,
    cleanup_partial_clone,
    clone_workflow,
    close_connection,
    create_workflow,
    destroy_workflow,
    open_connection,
    plan_capacity,
    update_workflow,
)

__all__ = [
    # Connection
    "open_connection",
    "close_connection",
    "disconnect",
    "Connection",
    # Workflows
    "clone_workflow",
    "create_workflow",
    "update_workflow",
    "destroy_workflow",
    "cleanup_partial_clone",
    "CloneResult",
    "CreateResult",
    "UpdateResult",
    "DestroyResult",
    "CleanupResult",
    # Capacity
    "plan_capacity",
    "CapacityPlan",
    "VmSpec",
    # Discovery
    "list_datastores",
    "list_networks",
    "list_vms",
    "list_base_vms",
    "list_vm_names",
    "get_host_capacity",
    "DatastoreInfo",
    "NetworkInfo",
    "VmInfo",
    "HostCapacity",
    # Progress
    "ProgressEvent",
    "ProgressCallback",
    "Phase",
    # VMX
    "DEFAULT_GUEST_OS",
    "DEFAULT_NETWORK",
    "random_mac",
    # Errors
    "VmkitError",
    "ValidationError",
    "AuthenticationError",
    "ConnectionFailedError",
    "HostUnreachableError",
    "TlsError",
    "PermissionDeniedError",
    "NotFoundError",
    "VmExistsError",
    "VmNotFoundError",
    "DatastoreNotFoundError",
    "DatacenterNotFoundError",
    "DatastoreFileNotFoundError",
    "InsufficientSpaceError",
    "TaskFailedError",
    "TaskTimeoutError",
    "TaskCancelledError",
]
