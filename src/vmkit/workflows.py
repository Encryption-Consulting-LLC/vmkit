"""High-level VM deployment workflows — the reusable core behind the CLIs / API.

Functions take parameters (including credentials, via a ``Connection``) and raise
typed ``vmkit.errors`` exceptions. They do not prompt, print, or call sys.exit.
Diagnostics go through stdlib ``logging``; long-running step progress is emitted
as structured ``vmkit.progress.ProgressEvent``s through an optional ``progress``
callback, so any front-end (CLI bar, websocket, polling) can render it.
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass

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
    get_datacenter,
    get_datastore,
    get_vm_by_name,
    list_vm_names,
    power_off_vm,
    power_on_vm,
    register_vm,
    wait_for_task,
)
from vmkit.progress import ProgressCallback, human_bytes
from vmkit.vmx import DEFAULT_GUEST_OS, parse_guest_os, random_mac, render_vmx

# Use the same logger name that setup_logging() configures, so workflow progress
# shows on the console/log file alongside the esxi/datastore modules.
log = logging.getLogger("deploy-vm")


# --------------------------------------------------------------------------- #
# Connection                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Connection:
    """An open ESXi/vCenter session plus the credentials needed for datastore
    HTTP file transfers (which use raw https, not the SOAP ServiceInstance)."""

    si: vim.ServiceInstance
    host: str
    user: str
    password: str
    port: int = 443

    @property
    def content(self) -> vim.ServiceInstanceContent:
        return self.si.content


def open_connection(host: str, user: str, password: str, port: int = 443) -> Connection:
    """Connect to ESXi/vCenter and return a Connection (raises on failure)."""
    si = connect(host, user, password, port)
    return Connection(si=si, host=host, user=user, password=password, port=port)


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


@dataclass
class UpdateResult:
    name: str
    cpus: int
    mem_mb: int
    mac: str
    guest_os: str
    iso_action: str  # "uploaded" | "removed" | "kept"
    powered_on: bool


@dataclass
class DestroyResult:
    name: str
    was_powered_on: bool


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
    datastore past ``max_usage_pct`` full. Returns the usage figures otherwise."""
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


def resolve_guest_os(
    conn: Connection,
    datastore: str,
    dc_name: str,
    vmx_path: str,
    default: str = DEFAULT_GUEST_OS,
) -> str:
    """Read the guestOS id from the VMX at ``vmx_path``; fall back to ``default``
    (so a Linux base/VM yields a Linux VMX instead of hardcoded Windows)."""
    try:
        vmx = read_datastore_file(
            conn.host, conn.user, conn.password, conn.port, datastore, dc_name, vmx_path
        )
        detected = parse_guest_os(vmx)
        if detected:
            return detected
        log.warning("No guestOS line in %s; defaulting to %s.", vmx_path, default)
    except Exception as exc:
        log.warning("Could not read %s (%s); defaulting guest OS to %s.", vmx_path, exc, default)
    return default


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
    max_usage_pct: float = 80.0,
    skip_disk_check: bool = False,
    power_on: bool = False,
    progress: ProgressCallback | None = None,
) -> CloneResult:
    """Clone the base VM's disk server-side, render+upload a VMX, register the VM.

    Raises VmExistsError, InsufficientSpaceError, or VmkitError (and lets ESXi
    errors propagate). The temp VMX is always cleaned up.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (VMDK copy, uploads, register, power-on); pass ``None`` to stay silent.
    """
    content = conn.content
    dc = get_datacenter(content)

    assert_vm_unique(content, name)

    disk = None
    if skip_disk_check:
        log.warning("Skipping datastore free-space check (skip_disk_check=True).")
    else:
        disk = validate_disk_usage(content, datastore, base, max_usage_pct)

    mac = mac or random_mac()
    log.info("MAC address: %s", mac)

    if guest_os is None:
        guest_os = resolve_guest_os(conn, datastore, dc.name, f"{base}/{base}.vmx")
    log.info("Guest OS: %s", guest_os)

    iso_filename = f"{name}-config.iso" if iso_path else None
    vmx_content = render_vmx(
        name, mac, cpus, mem_mb, iso_filename=iso_filename, guest_os=guest_os
    )

    tmp_vmx = None
    try:
        make_directory(content, dc, datastore, name)
        copy_virtual_disk(content, dc, datastore, base, name, progress)
        copy_datastore_file(content, dc, datastore, base, name, "nvram", progress)

        if iso_path:
            upload_file(
                conn.host, conn.user, conn.password, conn.port,
                datastore, dc.name, name, iso_path,
                remote_filename=f"{name}-config.iso",
                progress=progress,
            )

        tmp_vmx = _write_temp_vmx(vmx_content)
        upload_file(
            conn.host, conn.user, conn.password, conn.port,
            datastore, dc.name, name, tmp_vmx,
            remote_filename=f"{name}.vmx",
            progress=progress,
        )

        register_vm(content, dc, datastore, name, progress)
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
        power_on_vm(content, name, progress)

    return CloneResult(
        name=name, mac=mac, guest_os=guest_os,
        total_vms=len(names_after), powered_on=power_on, disk=disk,
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
    power_on: bool = False,
    progress: ProgressCallback | None = None,
) -> UpdateResult:
    """Re-render and upload the VMX (CPU/RAM/MAC/ISO) for an existing VM.

    Unspecified hardware values are preserved from the current config. Raises
    VmNotFoundError if the VM is absent. The temp VMX is always cleaned up.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (power-off, uploads, power-on); pass ``None`` to stay silent.
    """
    content = conn.content
    dc = get_datacenter(content)

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

    guest_os = resolve_guest_os(conn, datastore, dc.name, f"{name}/{name}.vmx")
    log.info("Guest OS: %s", guest_os)

    vmx_content = render_vmx(
        name, mac, cpus, mem_mb, iso_filename=iso_filename, guest_os=guest_os
    )

    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        log.info("Powering off VM ...")
        power_off_vm(content, name, progress)
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
            )

        tmp_vmx = _write_temp_vmx(vmx_content)
        upload_file(
            conn.host, conn.user, conn.password, conn.port,
            datastore, dc.name, name, tmp_vmx,
            remote_filename=f"{name}.vmx",
            progress=progress,
        )
    finally:
        if tmp_vmx is not None:
            _unlink_quietly(tmp_vmx)

    if power_on:
        power_on_vm(content, name, progress)

    return UpdateResult(
        name=name, cpus=cpus, mem_mb=mem_mb, mac=mac,
        guest_os=guest_os, iso_action=iso_action, powered_on=power_on,
    )


def destroy_workflow(
    conn: Connection,
    *,
    name: str,
    progress: ProgressCallback | None = None,
) -> DestroyResult:
    """Power off (if needed) and destroy an existing VM, deleting its files.

    Raises VmNotFoundError if the VM is absent. Destruction is permanent —
    ``Destroy_Task`` removes the VM from inventory and deletes its datastore
    directory.

    ``progress`` (optional) receives a ``ProgressEvent`` for each long-running
    step (power-off, destroy); pass ``None`` to stay silent.
    """
    content = conn.content

    vm = get_vm_by_name(content, name)
    if vm is None:
        raise VmNotFoundError(f"VM '{name}' not found.")

    was_on = vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn
    if was_on:
        log.info("Powering off VM ...")
        power_off_vm(content, name, progress)
        time.sleep(2)
    else:
        log.info("VM is already powered off.")

    log.info("Destroying VM: %s", name)
    task = vm.Destroy_Task()
    wait_for_task(task, "Destroy VM", progress)

    return DestroyResult(name=name, was_powered_on=was_on)
