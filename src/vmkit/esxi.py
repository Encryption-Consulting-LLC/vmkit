"""ESXi/vCenter session and inventory operations.

Every function takes an explicit ``content`` or ``conn``, so nothing here is tied
to a single target — one process can talk to as many hosts as it likes.

Known limitation (correct on standalone ESXi, **nondeterministic against
vCenter**): :func:`get_datacenter` returns the first datacenter it finds, and
:func:`register_vm` picks the first host in inventory and that host's resource
pool. A standalone ESXi host has exactly one of each, so the choice is forced.
Against vCenter it is arbitrary. If a caller ever needs to target vCenter, both
need an explicit datacenter/host/pool parameter.
"""

import atexit
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl

from vmkit._http import suppress_insecure_warnings
from vmkit.errors import (
    AuthenticationError,
    ConnectionFailedError,
    DatacenterNotFoundError,
    DatastoreNotFoundError,
    HostUnreachableError,
    PermissionDeniedError,
    TaskCancelledError,
    TaskFailedError,
    TaskTimeoutError,
    TlsError,
)
from vmkit.progress import ProgressCallback, Reporter

log = logging.getLogger(__name__)

# atexit hooks registered by connect(), keyed by id(ServiceInstance), so
# disconnect() can remove exactly its own hook. atexit.unregister() matches by
# equality and would otherwise strip every session's hook at once.
_atexit_hooks: dict[int, Callable[[], None]] = {}
_atexit_lock = threading.Lock()


def _disconnect_quietly(si: vim.ServiceInstance) -> None:
    try:
        Disconnect(si)
    except Exception:
        log.debug("Disconnect failed during cleanup", exc_info=True)


def _causes(exc: BaseException):
    """Walk the __cause__/__context__ chain, so a fault wrapped by pyVmomi's own
    error handling is still findable."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _classify_connect_failure(host: str, exc: Exception) -> ConnectionFailedError:
    """Map a connection exception onto the typed hierarchy.

    Everything used to collapse into a bare ``ConnectionFailedError`` because the
    except clause was ``except Exception``, which is why consumers walked the
    cause chain by hand looking for ``vim.fault.NoPermission``.
    """
    for cause in _causes(exc):
        if isinstance(cause, vim.fault.NoPermission):
            return PermissionDeniedError(
                f"Connected to {host} but the account lacks the required privilege: {cause}"
            )
        if isinstance(cause, ssl.SSLError):
            return TlsError(f"TLS handshake with {host} failed: {cause}")
        if isinstance(cause, (ConnectionError, TimeoutError, OSError)):
            # socket.gaierror and ConnectionRefusedError are both OSError.
            return HostUnreachableError(f"Could not reach {host}: {cause}")
    return ConnectionFailedError(f"Could not connect to {host}: {exc}")


def connect(
    host: str,
    user: str,
    password: str,
    port: int,
    *,
    register_atexit: bool = True,
    verify_tls: bool = False,
) -> vim.ServiceInstance:
    """Connect to ESXi/vCenter, return the ServiceInstance.

    Raises AuthenticationError on bad credentials, and a ConnectionFailedError
    subclass (HostUnreachableError / TlsError / PermissionDeniedError) otherwise.

    ``register_atexit`` registers a process-exit hook that closes the session.
    That is right for a short-lived CLI and wrong for a server: the hook holds a
    reference to every session ever opened, so a long-lived process opening one
    connection per batch leaks sessions (against the host's per-host session cap)
    and credentials in memory. Pass ``False`` and call :func:`disconnect`, or use
    ``Connection`` as a context manager.

    ``verify_tls`` defaults to ``False``, preserving the historical behaviour of
    accepting any certificate.
    """
    log.info("Connecting to %s as %s ...", host, user)
    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        suppress_insecure_warnings()
    try:
        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)
    except vim.fault.InvalidLogin:
        raise AuthenticationError(
            f"Login failed: invalid username or password for {user}@{host}."
        )
    except Exception as exc:
        raise _classify_connect_failure(host, exc) from exc

    if register_atexit:
        # A per-session closure, not a shared function: atexit.unregister()
        # matches by equality, so one shared callable would make closing any
        # session strip every other session's hook.
        def hook(si=si):
            _disconnect_quietly(si)

        atexit.register(hook)
        with _atexit_lock:
            _atexit_hooks[id(si)] = hook

    log.info("Connected. API version: %s", si.content.about.fullName)
    return si


def disconnect(si: vim.ServiceInstance) -> None:
    """Close a session and drop its atexit hook. Safe to call more than once."""
    with _atexit_lock:
        hook = _atexit_hooks.pop(id(si), None)
    if hook is not None:
        atexit.unregister(hook)
    _disconnect_quietly(si)


def get_datacenter(content: vim.ServiceInstanceContent) -> vim.Datacenter:
    """Return the first (only, on standalone ESXi) datacenter."""
    for child in content.rootFolder.childEntity:
        if isinstance(child, vim.Datacenter):
            return child
    raise DatacenterNotFoundError("No datacenter found on host.")


def _vm_name(vm: vim.VirtualMachine) -> str | None:
    """Read a VM's name, returning None if the VM was deleted between the
    container-view snapshot and this property read (e.g. a concurrent
    Destroy_Task) — a mid-deletion VM should simply not be listed."""
    try:
        return vm.name
    except vmodl.fault.ManagedObjectNotFound:
        return None


def _view(content: vim.ServiceInstanceContent, kind):
    view_mgr = content.viewManager
    assert view_mgr is not None
    return view_mgr.CreateContainerView(content.rootFolder, [kind], True)


def list_vm_names(content: vim.ServiceInstanceContent) -> set[str]:
    """Return a set of all VM names currently in inventory."""
    view = _view(content, vim.VirtualMachine)
    try:
        return {name for vm in view.view if (name := _vm_name(vm)) is not None}
    finally:
        view.Destroy()


def get_datastore(content: vim.ServiceInstanceContent, name: str) -> vim.Datastore:
    """Return the datastore object matching ``name``."""
    view = _view(content, vim.Datastore)
    try:
        for ds in view.view:
            if ds.name == name:
                return ds
    finally:
        view.Destroy()
    raise DatastoreNotFoundError(f"Datastore '{name}' not found on host.")


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatastoreInfo:
    """A datastore as offered to an operator choosing where to deploy."""

    name: str
    capacity: int
    free: int
    type: str
    accessible: bool

    @property
    def used(self) -> int:
        return self.capacity - self.free

    @property
    def pct_used(self) -> float:
        return (self.used / self.capacity * 100) if self.capacity else 0.0


@dataclass(frozen=True)
class NetworkInfo:
    """A port group a VM's NIC can be attached to."""

    name: str
    accessible: bool


@dataclass(frozen=True)
class VmInfo:
    """A VM in inventory, with enough detail to pick a clone base."""

    name: str
    power_state: str
    guest_os: str | None
    cpus: int | None
    mem_mb: int | None
    is_template: bool


@dataclass(frozen=True)
class HostCapacity:
    """A host's physical capacity and current usage.

    ``cpu_mhz_*`` figures are aggregate clock, which is how ESXi reports and
    schedules CPU; core counts are there for a sanity check against per-VM vCPU
    totals.
    """

    name: str
    cpu_cores: int
    cpu_threads: int
    cpu_mhz_total: int
    cpu_mhz_used: int
    memory_mb_total: int
    memory_mb_used: int

    @property
    def cpu_mhz_free(self) -> int:
        return max(0, self.cpu_mhz_total - self.cpu_mhz_used)

    @property
    def memory_mb_free(self) -> int:
        return max(0, self.memory_mb_total - self.memory_mb_used)


def list_datastores(content: vim.ServiceInstanceContent) -> list[DatastoreInfo]:
    """Enumerate datastores. Nothing could be enumerated before 0.4.0 — a caller
    could only fetch one by exact name, so a typo became a mid-clone failure."""
    view = _view(content, vim.Datastore)
    out: list[DatastoreInfo] = []
    try:
        for ds in view.view:
            try:
                summary = ds.summary
                out.append(
                    DatastoreInfo(
                        name=summary.name,
                        capacity=summary.capacity or 0,
                        free=summary.freeSpace or 0,
                        type=summary.type or "",
                        accessible=bool(summary.accessible),
                    )
                )
            except vmodl.fault.ManagedObjectNotFound:
                continue
    finally:
        view.Destroy()
    return sorted(out, key=lambda d: d.name)


def list_networks(content: vim.ServiceInstanceContent) -> list[NetworkInfo]:
    """Enumerate port groups, for validating a VM's ``network`` before cloning.

    Attaching a VM to a port group that does not exist on the target produces a
    registered VM with a dead NIC — silent, and only noticed when first boot never
    phones home.
    """
    view = _view(content, vim.Network)
    out: list[NetworkInfo] = []
    try:
        for net in view.view:
            try:
                out.append(
                    NetworkInfo(name=net.name, accessible=bool(net.summary.accessible))
                )
            except vmodl.fault.ManagedObjectNotFound:
                continue
    finally:
        view.Destroy()
    return sorted(out, key=lambda n: n.name)


def _vm_info(vm: vim.VirtualMachine) -> VmInfo | None:
    try:
        summary = vm.summary
        config = summary.config
        return VmInfo(
            name=config.name,
            power_state=str(summary.runtime.powerState),
            guest_os=config.guestId,
            cpus=config.numCpu,
            mem_mb=config.memorySizeMB,
            is_template=bool(config.template),
        )
    except vmodl.fault.ManagedObjectNotFound:
        return None


def list_vms(content: vim.ServiceInstanceContent) -> list[VmInfo]:
    """Enumerate every VM with its hardware summary."""
    view = _view(content, vim.VirtualMachine)
    try:
        out = [info for vm in view.view if (info := _vm_info(vm)) is not None]
    finally:
        view.Destroy()
    return sorted(out, key=lambda v: v.name)


def list_base_vms(content: vim.ServiceInstanceContent) -> list[VmInfo]:
    """Enumerate VMs usable as a clone base: templates and powered-off VMs.

    Cloning copies the base's VMDK server-side without snapshotting it, so a
    powered-on base would be copied while its disk is being written to.
    """
    return [
        vm
        for vm in list_vms(content)
        if vm.is_template or vm.power_state == str(vim.VirtualMachinePowerState.poweredOff)
    ]


def get_host_capacity(content: vim.ServiceInstanceContent) -> HostCapacity:
    """Return the first host's CPU/RAM capacity and current usage.

    There was no host CPU/RAM read at all before 0.4.0, so overcommit was
    completely unchecked — a batch could ask for more vCPU and RAM than the host
    physically has and nothing would notice until the VMs contended.
    """
    view = _view(content, vim.HostSystem)
    try:
        if not view.view:
            raise DatacenterNotFoundError("No host found on this connection.")
        host = view.view[0]
        hardware = host.summary.hardware
        stats = host.summary.quickStats
        return HostCapacity(
            name=host.name,
            cpu_cores=hardware.numCpuCores or 0,
            cpu_threads=hardware.numCpuThreads or 0,
            cpu_mhz_total=(hardware.cpuMhz or 0) * (hardware.numCpuCores or 0),
            cpu_mhz_used=stats.overallCpuUsage or 0,
            memory_mb_total=int((hardware.memorySize or 0) / (1024 * 1024)),
            memory_mb_used=stats.overallMemoryUsage or 0,
        )
    finally:
        view.Destroy()


# --------------------------------------------------------------------------- #
# VM operations                                                               #
# --------------------------------------------------------------------------- #
def register_vm(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    name: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Register the VM from its .vmx file."""
    vmx_ds_path = f"[{datastore}] {name}/{name}.vmx"
    log.info("Registering VM from: %s", vmx_ds_path)

    host_view = _view(content, vim.HostSystem)
    try:
        esxi_host = host_view.view[0]
    finally:
        host_view.Destroy()
    resource_pool = esxi_host.parent.resourcePool

    task = dc.vmFolder.RegisterVM_Task(
        path=vmx_ds_path,
        name=name,
        asTemplate=False,
        pool=resource_pool,
        host=esxi_host,
    )
    wait_for_task(
        task,
        "Register VM",
        progress,
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )


def get_vm_by_name(
    content: vim.ServiceInstanceContent, name: str
) -> vim.VirtualMachine | None:
    """Return the VM object matching ``name``, or None if not found."""
    view = _view(content, vim.VirtualMachine)
    try:
        return next((vm for vm in view.view if _vm_name(vm) == name), None)
    finally:
        view.Destroy()


def power_on_vm(
    content: vim.ServiceInstanceContent,
    name: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Power on the named VM."""
    target = get_vm_by_name(content, name)
    if not target:
        log.warning("Could not find VM '%s' to power on.", name)
        return
    log.info("Powering on VM: %s", name)
    task = target.PowerOnVM_Task()
    wait_for_task(
        task,
        "Power on",
        progress,
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )


def power_off_vm(
    content: vim.ServiceInstanceContent,
    name: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Power off the named VM."""
    target = get_vm_by_name(content, name)
    if not target:
        log.warning("Could not find VM '%s' to power off.", name)
        return
    log.info("Powering off VM: %s", name)
    task = target.PowerOffVM_Task()
    wait_for_task(
        task,
        "Power off",
        progress,
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )


def _task_key(label: str) -> str:
    """Stable per-operation key for progress consumers (one bar/channel each)."""
    return "task:" + label.lower().replace(" ", "-")


def _try_cancel(task: vim.Task, label: str) -> None:
    """Ask ESXi to cancel a task. Best-effort: not every task is cancelable."""
    try:
        task.CancelTask()
        log.info("  %s: cancellation requested", label)
    except Exception:
        log.warning(
            "  %s: could not be cancelled; it may run to completion on the host",
            label,
            exc_info=True,
        )


def wait_for_task(
    task: vim.Task,
    label: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    poll_interval_s: float = 1.0,
) -> object:
    """Block until a vSphere task completes, emitting 0-100% progress events.

    ``timeout_s`` bounds the wait; ``should_cancel`` is polled each iteration so a
    caller can stop a batch. Both issue ``CancelTask`` and then raise
    (``TaskTimeoutError`` / ``TaskCancelledError``). Without them the loop is
    unbounded, and a hung ``CopyVirtualDisk_Task`` holds a worker slot forever
    with no way to intervene.

    Raises ``TaskFailedError`` if the task ends in a non-success state.
    """
    rep = Reporter(
        progress,
        key=_task_key(label),
        label=label,
        total=100,
        unit="%",
        context=progress_context,
    )
    rep.start()
    deadline = (time.monotonic() + timeout_s) if timeout_s is not None else None

    while True:
        # One property read per iteration: `task.info` is a live SOAP fetch, so
        # reading .state and .progress separately doubles the round trips and can
        # observe two different snapshots of the same task.
        info = task.info
        if info.state not in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
            break
        if should_cancel is not None and should_cancel():
            _try_cancel(task, label)
            rep.fail("cancelled")
            log.info("  %s: cancelled by caller", label)
            raise TaskCancelledError(f"{label} was cancelled.")
        if deadline is not None and time.monotonic() > deadline:
            _try_cancel(task, label)
            msg = f"{label} timed out after {timeout_s:g}s."
            rep.fail(msg)
            log.error("  %s", msg)
            raise TaskTimeoutError(msg, label=label)
        rep.to(info.progress or 0)
        time.sleep(poll_interval_s)

    if info.state == vim.TaskInfo.State.success:
        rep.finish()
        log.info("  %s: done", label)
        return info.result

    err = info.error
    msg = err.msg if err else "unknown error"
    rep.fail(msg)
    log.error("  %s FAILED: %s", label, msg)
    raise TaskFailedError(f"{label} failed: {msg}", label=label, fault=err)
