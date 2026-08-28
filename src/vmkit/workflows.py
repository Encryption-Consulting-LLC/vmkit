"""High-level VM deployment workflows — the reusable core behind the CLIs / API.

Functions take parameters (including credentials, via a ``Connection``) and raise
typed ``vmkit.errors`` exceptions. They do not prompt, print, or call sys.exit.
Diagnostics go through stdlib ``logging``; long-running step progress is emitted
as structured ``vmkit.progress.ProgressEvent``s through an optional ``progress``
callback, so any front-end (CLI bar, websocket, polling) can render it.

Nothing here is bound to one target: every function takes an explicit
``Connection``, so a caller can drive many hosts from one process. Use
``Connection`` as a context manager (or call ``close_connection``) in a
long-lived process — see :func:`open_connection`.
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from pyVmomi import vim

from vmkit.datastore import (
    copy_datastore_file,
    copy_virtual_disk,
    get_base_vmdk_size,
    make_directory,
    read_datastore_file,
    upload_file,
)
from vmkit.errors import (
    InsufficientSpaceError,
    VmExistsError,
    VmkitError,
    VmNotFoundError,
)
from vmkit.esxi import (
    connect,
    disconnect,
    get_datacenter,
    get_datastore,
    get_host_capacity,
    get_vm_by_name,
    list_vm_names,
    power_off_vm,
    power_on_vm,
    register_vm,
    wait_for_task,
)
from vmkit.progress import ProgressCallback, StepReporter, human_bytes
from vmkit.vmx import (
    DEFAULT_GUEST_OS,
    DEFAULT_NETWORK,
    NIC_PCI_SLOT,
    parse_guest_os,
    parse_network,
    parse_nic_pci_slot,
    random_mac,
    render_vmx,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Connection                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Connection:
    """An open ESXi/vCenter session plus the credentials needed for datastore
    HTTP file transfers (which use raw https, not the SOAP ServiceInstance).

    Usable as a context manager, which is the right shape for a server holding
    one pooled connection per batch::

        with open_connection(host, user, pw, register_atexit=False) as conn:
            clone_workflow(conn, ...)
    """

    si: vim.ServiceInstance
    host: str
    user: str
    # Excluded from repr: the default dataclass repr puts the ESXi password into
    # any traceback, logged frame-locals capture, or error-reporting payload.
    password: str = field(repr=False)
    port: int = 443
    verify_tls: bool | str = False

    @property
    def content(self) -> vim.ServiceInstanceContent:
        return self.si.content

    def close(self) -> None:
        """Close the session and drop its atexit hook. Idempotent."""
        disconnect(self.si)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_connection(
    host: str,
    user: str,
    password: str,
    port: int = 443,
    *,
    register_atexit: bool = True,
    verify_tls: bool | str = False,
) -> Connection:
    """Connect to ESXi/vCenter and return a Connection (raises on failure).

    ``register_atexit=True`` (the default, preserving historical behaviour) keeps
    a process-exit hook holding this session. In a long-lived process that opens
    a connection per batch, pass ``False`` and close the connection — otherwise
    every session ever opened is retained until the process exits, which runs into
    the host's per-host session cap and keeps credentials in memory.
    """
    si = connect(
        host, user, password, port, register_atexit=register_atexit, verify_tls=verify_tls
    )
    return Connection(
        si=si, host=host, user=user, password=password, port=port, verify_tls=verify_tls
    )


def close_connection(conn: Connection) -> None:
    """Close a Connection's session. Idempotent; equivalent to ``conn.close()``."""
    conn.close()


# --------------------------------------------------------------------------- #
# Results                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class DiskUsage:
    capacity: int
    used: int
    vmdk_size: int
    used_after: int
    pct_now: float
    pct_after: float


@dataclass
class CloneResult:
    name: str
    mac: str
    guest_os: str
    total_vms: int
    powered_on: bool
    disk: DiskUsage | None = None
    network: str = DEFAULT_NETWORK


@dataclass
class UpdateResult:
    name: str
    cpus: int
    mem_mb: int
    mac: str
    guest_os: str
    iso_action: str  # "uploaded" | "removed" | "kept"
    powered_on: bool
    network: str = DEFAULT_NETWORK


@dataclass
class DestroyResult:
    name: str
    was_powered_on: bool


@dataclass
class CleanupResult:
    name: str
    deleted: bool


# --------------------------------------------------------------------------- #
# Batch capacity planning                                                     #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VmSpec:
    """One VM in a planned batch, for :func:`plan_capacity`."""

    name: str
    base: str
    cpus: int = 1
    mem_mb: int = 1024


@dataclass(frozen=True)
class CapacityPlan:
    """What a whole batch would cost a host, measured once.

    ``fits`` and ``problems`` cover the hard datastore limit. CPU and memory
    figures are reported rather than enforced: ESXi overcommits both by design,
    so the right threshold is a policy decision for the caller.
    """

    datastore: str
    capacity: int
    used: int
    required: int
    used_after: int
    pct_now: float
    pct_after: float
    max_usage_pct: float
    fits: bool
    vm_count: int
    total_cpus: int
    total_mem_mb: int
    host: str | None = None
    host_cpu_cores: int | None = None
    host_memory_mb: int | None = None
    host_memory_mb_free: int | None = None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def raise_if_insufficient(self) -> "CapacityPlan":
        """Raise ``InsufficientSpaceError`` if the batch does not fit; else return self."""
        if not self.fits:
            raise InsufficientSpaceError(" ".join(self.problems))
        return self


def plan_capacity(
    content: vim.ServiceInstanceContent,
    datastore: str,
    specs: list[VmSpec],
    max_usage_pct: float = 80.0,
    *,
    read_host_capacity: bool = True,
) -> CapacityPlan:
    """Cost a whole batch against a datastore in one pass.

    :func:`validate_disk_usage` answers "does *one more* VM fit", which is the
    wrong question for a batch: N concurrent clones each see the same free space,
    each individually fits, and collectively they fill the datastore. This sums
    every VMDK in the batch against free space once, before any of them start.

    Distinct bases are measured once each, so a batch of 50 clones from one base
    costs one datastore-browser search rather than 50.

    Reports CPU and memory totals against the host's physical capacity, which
    nothing read at all before 0.4.0 — overcommit was entirely unchecked.
    """
    ds = get_datastore(content, datastore)
    capacity = ds.summary.capacity or 0
    free = ds.summary.freeSpace or 0
    used = capacity - free

    base_sizes: dict[str, int] = {}
    for spec in specs:
        if spec.base not in base_sizes:
            base_sizes[spec.base] = get_base_vmdk_size(ds, spec.base)
    required = sum(base_sizes[spec.base] for spec in specs)

    used_after = used + required
    pct_now = (used / capacity * 100) if capacity else 0.0
    pct_after = (used_after / capacity * 100) if capacity else 0.0

    total_cpus = sum(spec.cpus for spec in specs)
    total_mem_mb = sum(spec.mem_mb for spec in specs)

    problems: list[str] = []
    warnings: list[str] = []
    if pct_after > max_usage_pct:
        problems.append(
            f"Cloning {len(specs)} VM(s) would need {human_bytes(required)} and leave "
            f"datastore '{datastore}' at {pct_after:.1f}% full, exceeding the "
            f"{max_usage_pct:.1f}% limit."
        )

    host_name = host_cores = host_mem = host_mem_free = None
    if read_host_capacity:
        try:
            host = get_host_capacity(content)
            host_name = host.name
            host_cores = host.cpu_cores
            host_mem = host.memory_mb_total
            host_mem_free = host.memory_mb_free
            if total_mem_mb > host.memory_mb_free:
                warnings.append(
                    f"Batch requests {total_mem_mb} MB RAM but the host has "
                    f"{host.memory_mb_free} MB free; VMs will contend or fail to power on."
                )
            if host.cpu_cores and total_cpus > host.cpu_cores * 4:
                warnings.append(
                    f"Batch requests {total_cpus} vCPUs against {host.cpu_cores} physical "
                    "cores (over 4:1 overcommit)."
                )
        except Exception:
            # Capacity reporting is advisory; a host that will not answer must not
            # block a plan whose datastore arithmetic is sound.
            log.warning("Could not read host capacity; reporting disk only", exc_info=True)

    log.info("Capacity plan for %d VM(s) on '%s':", len(specs), datastore)
    log.info("  capacity      : %s", human_bytes(capacity))
    log.info("  used (now)    : %s (%.1f%%)", human_bytes(used), pct_now)
    log.info("  batch needs   : %s", human_bytes(required))
    log.info("  used (after)  : %s (%.1f%%)", human_bytes(used_after), pct_after)

    return CapacityPlan(
        datastore=datastore,
        capacity=capacity,
        used=used,
        required=required,
        used_after=used_after,
        pct_now=pct_now,
        pct_after=pct_after,
        max_usage_pct=max_usage_pct,
        fits=not problems,
        vm_count=len(specs),
        total_cpus=total_cpus,
        total_mem_mb=total_mem_mb,
        host=host_name,
        host_cpu_cores=host_cores,
        host_memory_mb=host_mem,
        host_memory_mb_free=host_mem_free,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Helpers (also useful on their own)                                          #
# --------------------------------------------------------------------------- #
def assert_vm_unique(content: vim.ServiceInstanceContent, name: str) -> None:
    """Raise VmExistsError if a VM named ``name`` is already in inventory."""
    if name in list_vm_names(content):
        raise VmExistsError(f"A VM named '{name}' already exists.")


def get_vm_config(vm: vim.VirtualMachine) -> dict:
    """Extract current CPU, RAM (MB), and primary MAC from a registered VM."""
    config = vm.config
    mac = None
    for device in config.hardware.device:
        if isinstance(device, vim.VirtualEthernetCard):
            mac = device.macAddress
            break
    return {"cpus": config.hardware.numCPU, "mem_mb": config.hardware.memoryMB, "mac": mac}


def validate_disk_usage(
    content: vim.ServiceInstanceContent,
    datastore: str,
    base: str,
    max_usage_pct: float,
) -> DiskUsage:
    """Raise InsufficientSpaceError if cloning ``base``'s VMDK would push the
    datastore past ``max_usage_pct`` full. Returns the usage figures otherwise.

    Single-VM check. For a batch use :func:`plan_capacity`, which sums every VMDK
    at once — N concurrent calls to this function each see the same free space and
    each individually "fit".
    """
    ds = get_datastore(content, datastore)
    capacity = ds.summary.capacity
    free = ds.summary.freeSpace
    used = capacity - free
    vmdk_size = get_base_vmdk_size(ds, base)
    used_after = used + vmdk_size
    pct_now = (used / capacity * 100) if capacity else 0.0
    pct_after = (used_after / capacity * 100) if capacity else 0.0

    log.info("Datastore '%s' disk usage:", datastore)
    log.info("  capacity      : %s", human_bytes(capacity))
    log.info("  used (now)    : %s (%.1f%%)", human_bytes(used), pct_now)
    log.info("  base VMDK     : %s", human_bytes(vmdk_size))
    log.info("  used (after)  : %s (%.1f%%)", human_bytes(used_after), pct_after)

    usage = DiskUsage(capacity, used, vmdk_size, used_after, pct_now, pct_after)
    if pct_after > max_usage_pct:
        raise InsufficientSpaceError(
            f"Cloning the base VMDK would leave datastore '{datastore}' at "
            f"{pct_after:.1f}% full, exceeding the {max_usage_pct:.1f}% limit."
        )
    return usage


def _read_vmx_text(conn: Connection, datastore: str, dc_name: str, vmx_path: str) -> str | None:
    try:
        return read_datastore_file(
            conn.host,
            conn.user,
            conn.password,
            conn.port,
            datastore,
            dc_name,
            vmx_path,
            verify_tls=conn.verify_tls,
        )
    except Exception as exc:
        log.warning("Could not read %s (%s).", vmx_path, exc)
        return None


def _guest_os_from_vmx(
    vmx: str | None, vmx_path: str, default: str = DEFAULT_GUEST_OS
) -> str:
    """Pick the guestOS id out of already-read VMX text; fall back to ``default``."""
    if vmx is not None:
        detected = parse_guest_os(vmx)
        if detected:
            return detected
        log.warning("No guestOS line in %s; defaulting to %s.", vmx_path, default)
    else:
        log.warning("Defaulting guest OS to %s.", default)
    return default


def resolve_guest_os(
    conn: Connection,
    datastore: str,
    dc_name: str,
    vmx_path: str,
    default: str = DEFAULT_GUEST_OS,
) -> str:
    """Read the guestOS id from the VMX at ``vmx_path``; fall back to ``default``
    (so a Linux base/VM yields a Linux VMX instead of hardcoded Windows)."""
    vmx = _read_vmx_text(conn, datastore, dc_name, vmx_path)
    return _guest_os_from_vmx(vmx, vmx_path, default)


def warn_on_nic_slot_mismatch(vmx: str | None, base: str) -> None:
    """Warn when ``base``'s NIC sits at a different PCI slot than the clone gets.

    Silent when the VMX could not be read or has no ``ethernet0.pciSlotNumber``
    line — an unreadable base is already reported by the read itself, and a
    missing line means ESXi assigns the slot, which we cannot predict.
    """
    if vmx is None:
        return
    slot = parse_nic_pci_slot(vmx)
    if slot is None or slot == NIC_PCI_SLOT:
        return
    log.warning(
        "Base '%s' has ethernet0.pciSlotNumber=%s, but this clone is pinned to %s. "
        "On VMware the guest's interface name derives from that slot, so the clone's "
        "NIC will be named differently from the base image's. Generated per-VM "
        "configs detect the NIC at runtime and are unaffected; anything baked into "
        "the image that names an interface is not.",
        base, slot, NIC_PCI_SLOT,
    )


def _write_temp_vmx(vmx_content: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".vmx", delete=False, encoding="utf-8"
    ) as f:
        f.write(vmx_content)
        return f.name


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def cleanup_partial_clone(
    conn: Connection, name: str, datastore: str
) -> CleanupResult:
    """Delete the datastore directory left behind by a failed clone.

    ``clone_workflow`` uploads the VMDK, NVRAM, ISO and VMX before registering the
    VM. If registration fails, those files — possibly a hundred gigabytes of them
    — are orphaned with no rollback, and every consumer has had to compensate by
    hand.

    Refuses to run if a VM of that name is registered: that is a live VM, and
    deleting its files out from under it is not a cleanup. Use
    ``destroy_workflow`` instead.

    Returns ``CleanupResult(deleted=False)`` when there was nothing to delete, so
    a reconcile sweep can call it unconditionally.
    """
    content = conn.content
    if get_vm_by_name(content, name) is not None:
        raise VmExistsError(
            f"VM '{name}' is registered in inventory; use destroy_workflow to remove it "
            "rather than deleting its datastore files."
        )

    dc = get_datacenter(content)
    path = f"[{datastore}] {name}"
    file_manager = content.fileManager
    assert file_manager is not None

    log.info("Cleaning up partial clone at %s", path)
    try:
        task = file_manager.DeleteDatastoreFile_Task(name=path, datacenter=dc)
    except vim.fault.FileNotFound:
        log.info("  nothing to clean up at %s", path)
        return CleanupResult(name=name, deleted=False)

    try:
        wait_for_task(task, "Delete partial clone", None)
    except Exception as exc:
        if isinstance(getattr(exc, "fault", None), vim.fault.FileNotFound):
            log.info("  nothing to clean up at %s", path)
            return CleanupResult(name=name, deleted=False)
        raise
    return CleanupResult(name=name, deleted=True)


# --------------------------------------------------------------------------- #
# Workflows                                                                   #
# --------------------------------------------------------------------------- #
def clone_workflow(
    conn: Connection,
    *,
    name: str,
    base: str,
    datastore: str,
    cpus: int,
    mem_mb: int,
    mac: str | None = None,
    iso_path: str | None = None,
    guest_os: str | None = None,
    network: str = DEFAULT_NETWORK,
    max_usage_pct: float = 80.0,
    skip_disk_check: bool = False,
    power_on: bool = False,
    progress: ProgressCallback | None = None,
    progress_context: str | None = None,
    used_macs: set[str] | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> CloneResult:
    """Clone the base VM's disk server-side, render+upload a VMX, register the VM.

    Raises VmExistsError, InsufficientSpaceError, TaskFailedError, or VmkitError
    (and lets ESXi errors propagate). The temp VMX is always cleaned up. On a
    failure after the datastore directory is created, call
    :func:`cleanup_partial_clone` to remove the orphaned files.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (VMDK copy, uploads, register, power-on) plus an overall
    ``workflow:clone`` step counter; pass ``None`` to stay silent.
    ``progress_context`` (typically the VM name) distinguishes concurrent clones.

    ``network`` is the port group the NIC attaches to. It must exist on the target
    — see ``list_networks`` — or the VM registers with a dead NIC.

    ``used_macs`` lets the caller exclude addresses already assigned elsewhere in
    a batch, which ``random_mac`` cannot see by itself.

    ``timeout_s`` and ``should_cancel`` bound and interrupt each vSphere task.
    """
    content = conn.content
    dc = get_datacenter(content)

    task_kw = dict(
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )

    # Steps: vmdk copy, nvram copy, [iso upload], vmx upload, register, [power on]
    total_steps = 4 + (1 if iso_path else 0) + (1 if power_on else 0)
    steps = StepReporter(progress, "clone", total_steps, context=progress_context)

    assert_vm_unique(content, name)

    disk = None
    if skip_disk_check:
        log.warning("Skipping datastore free-space check (skip_disk_check=True).")
    else:
        disk = validate_disk_usage(content, datastore, base, max_usage_pct)

    mac = mac or random_mac(used_macs)
    log.info("MAC address: %s", mac)

    base_vmx_path = f"{base}/{base}.vmx"
    base_vmx = _read_vmx_text(conn, datastore, dc.name, base_vmx_path)
    if guest_os is None:
        guest_os = _guest_os_from_vmx(base_vmx, base_vmx_path)
    log.info("Guest OS: %s", guest_os)
    warn_on_nic_slot_mismatch(base_vmx, base)
    log.info("Network: %s", network)

    iso_filename = f"{name}-config.iso" if iso_path else None
    vmx_content = render_vmx(
        name, mac, cpus, mem_mb, iso_filename=iso_filename, guest_os=guest_os,
        network=network,
    )

    steps.start()
    tmp_vmx = None
    try:
        make_directory(content, dc, datastore, name)
        copy_virtual_disk(content, dc, datastore, base, name, progress, **task_kw)
        steps.step("VMDK copy")
        copy_datastore_file(content, dc, datastore, base, name, "nvram", progress, **task_kw)
        steps.step("NVRAM copy")

        if iso_path:
            upload_file(
                conn.host, conn.user, conn.password, conn.port,
                datastore, dc.name, name, iso_path,
                remote_filename=f"{name}-config.iso",
                progress=progress,
                progress_context=progress_context,
                verify_tls=conn.verify_tls,
            )
            steps.step("ISO upload")

        tmp_vmx = _write_temp_vmx(vmx_content)
        upload_file(
            conn.host, conn.user, conn.password, conn.port,
            datastore, dc.name, name, tmp_vmx,
            remote_filename=f"{name}.vmx",
            progress=progress,
            progress_context=progress_context,
            verify_tls=conn.verify_tls,
        )
        steps.step("VMX upload")

        register_vm(content, dc, datastore, name, progress, **task_kw)
        steps.step("register")
    except Exception as exc:
        steps.fail(str(exc))
        raise
    finally:
        if tmp_vmx is not None:
            _unlink_quietly(tmp_vmx)

    log.info("Waiting for inventory to settle ...")
    time.sleep(3)
    names_after = list_vm_names(content)
    if name not in names_after:
        raise VmkitError(f"VM '{name}' not found in inventory after registration.")
    log.info("CONFIRMED: VM '%s' is now in inventory.", name)

    if power_on:
        power_on_vm(content, name, progress, **task_kw)
        steps.step("power on")

    steps.finish()

    return CloneResult(
        name=name, mac=mac, guest_os=guest_os,
        total_vms=len(names_after), powered_on=power_on, disk=disk, network=network,
    )


def update_workflow(
    conn: Connection,
    *,
    name: str,
    datastore: str,
    cpus: int | None = None,
    mem_mb: int | None = None,
    mac: str | None = None,
    iso_path: str | None = None,
    remove_iso: bool = False,
    network: str | None = None,
    power_on: bool = False,
    progress: ProgressCallback | None = None,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> UpdateResult:
    """Re-render and upload the VMX (CPU/RAM/MAC/ISO/network) for an existing VM.

    Unspecified hardware values are preserved from the current config. Raises
    VmNotFoundError if the VM is absent. The temp VMX is always cleaned up.

    ``network=None`` preserves the VM's current port group (read from its VMX),
    falling back to the default only if it cannot be read — so an update never
    silently moves a VM onto a different network.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (power-off, uploads, power-on); pass ``None`` to stay silent.

    Setting ``remove_iso=True`` detaches the config ISO. Worth doing once first
    boot has completed: the disc carries the agent token and, for a domain
    controller, an Administrator password in plaintext, and it otherwise stays
    attached for the VM's whole life.
    """
    content = conn.content
    dc = get_datacenter(content)

    task_kw = dict(
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )

    vm = get_vm_by_name(content, name)
    if vm is None:
        raise VmNotFoundError(f"VM '{name}' not found.")

    current = get_vm_config(vm)
    cpus = cpus if cpus is not None else current["cpus"]
    mem_mb = mem_mb if mem_mb is not None else current["mem_mb"]
    mac = mac if mac is not None else current["mac"]
    log.info("New config: %d CPUs, %d MB RAM, MAC %s", cpus, mem_mb, mac)

    if remove_iso:
        iso_filename, iso_action = None, "removed"
    elif iso_path:
        iso_filename, iso_action = f"{name}-config.iso", "uploaded"
    else:
        iso_filename, iso_action = f"{name}-config.iso", "kept"

    # One read serves both the guest OS and the current port group.
    vmx_text = _read_vmx_text(conn, datastore, dc.name, f"{name}/{name}.vmx")
    guest_os = (parse_guest_os(vmx_text) if vmx_text else None) or DEFAULT_GUEST_OS
    if network is None:
        network = (parse_network(vmx_text) if vmx_text else None) or DEFAULT_NETWORK
    log.info("Guest OS: %s", guest_os)
    log.info("Network: %s", network)

    vmx_content = render_vmx(
        name, mac, cpus, mem_mb, iso_filename=iso_filename, guest_os=guest_os,
        network=network,
    )

    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        log.info("Powering off VM ...")
        power_off_vm(content, name, progress, **task_kw)
        time.sleep(2)
    else:
        log.info("VM is already powered off.")

    tmp_vmx = None
    try:
        if iso_path:
            upload_file(
                conn.host, conn.user, conn.password, conn.port,
                datastore, dc.name, name, iso_path,
                remote_filename=f"{name}-config.iso",
                progress=progress,
                progress_context=progress_context,
                verify_tls=conn.verify_tls,
            )

        tmp_vmx = _write_temp_vmx(vmx_content)
        upload_file(
            conn.host, conn.user, conn.password, conn.port,
            datastore, dc.name, name, tmp_vmx,
            remote_filename=f"{name}.vmx",
            progress=progress,
            progress_context=progress_context,
            verify_tls=conn.verify_tls,
        )
    finally:
        if tmp_vmx is not None:
            _unlink_quietly(tmp_vmx)

    if power_on:
        power_on_vm(content, name, progress, **task_kw)

    return UpdateResult(
        name=name, cpus=cpus, mem_mb=mem_mb, mac=mac,
        guest_os=guest_os, iso_action=iso_action, powered_on=power_on,
        network=network,
    )


def destroy_workflow(
    conn: Connection,
    *,
    name: str,
    progress: ProgressCallback | None = None,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> DestroyResult:
    """Power off (if needed) and destroy an existing VM, deleting its files.

    Raises VmNotFoundError if the VM is absent. Destruction is permanent —
    ``Destroy_Task`` removes the VM from inventory and deletes its datastore
    directory.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (power-off, destroy); pass ``None`` to stay silent.
    """
    content = conn.content

    task_kw = dict(
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )

    vm = get_vm_by_name(content, name)
    if vm is None:
        raise VmNotFoundError(f"VM '{name}' not found.")

    was_on = vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn
    if was_on:
        log.info("Powering off VM ...")
        power_off_vm(content, name, progress, **task_kw)
        time.sleep(2)
    else:
        log.info("VM is already powered off.")

    log.info("Destroying VM: %s", name)
    task = vm.Destroy_Task()
    wait_for_task(task, "Destroy VM", progress, **task_kw)

    return DestroyResult(name=name, was_powered_on=was_on)
