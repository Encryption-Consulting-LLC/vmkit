import logging
import time
from pathlib import Path
from typing import IO

import requests

from pyVmomi import vim

from vmkit.errors import TaskFailedError
from vmkit._http import suppress_insecure_warnings
from vmkit.progress import ProgressCallback, Reporter

log = logging.getLogger(__name__)


def make_directory(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    name: str,
) -> None:
    """Create <datastore>/<name>/ directory (no error if it already exists)."""
    path = f"[{datastore}] {name}"
    log.info("Creating directory: %s", path)
    fm = content.fileManager
    assert fm is not None
    try:
        fm.MakeDirectory(name=path, datacenter=dc, createParentDirectories=True)
        log.info("  Directory created.")
    except vim.fault.FileAlreadyExists:
        log.warning("  Directory already exists, continuing.")


def copy_virtual_disk(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    base: str,
    name: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel=None,
) -> None:
    """Server-side copy of the base VMDK to the new VM folder."""
    from vmkit.esxi import wait_for_task

    src = f"[{datastore}] {base}/{base}.vmdk"
    dst = f"[{datastore}] {name}/{name}.vmdk"
    log.info("Copying virtual disk:")
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    vdm = content.virtualDiskManager
    assert vdm is not None
    task = vdm.CopyVirtualDisk_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destName=dst,
        destDatacenter=dc,
        force=False,
    )
    wait_for_task(
        task,
        "VMDK copy",
        progress,
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )


def copy_datastore_file(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    base: str,
    name: str,
    ext: str,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    timeout_s: float | None = None,
    should_cancel=None,
) -> None:
    """Server-side copy of an arbitrary datastore file (e.g. .nvram)."""
    from vmkit.esxi import wait_for_task

    src = f"[{datastore}] {base}/{base}.{ext}"
    dst = f"[{datastore}] {name}/{name}.{ext}"
    log.info("Copying %s file:", ext)
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    fm = content.fileManager
    assert fm is not None
    task = fm.CopyDatastoreFile_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destinationName=dst,
        destinationDatacenter=dc,
        force=False,
    )
    wait_for_task(
        task,
        f"{ext} copy",
        progress,
        progress_context=progress_context,
        timeout_s=timeout_s,
        should_cancel=should_cancel,
    )


def upload_file(
    host: str,
    user: str,
    password: str,
    port: int,
    datastore: str,
    dc_name: str,
    vm_name: str,
    local_path: str,
    remote_filename: str | None = None,
    progress: ProgressCallback | None = None,
    *,
    progress_context: str | None = None,
    verify_tls: bool | str = False,
) -> None:
    """Upload a local file to <datastore>/<vm_name>/<remote_filename> via HTTPS PUT."""
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"Local file not found: {local}")

    if remote_filename is None:
        remote_filename = f"{vm_name}.vmx"
    remote_path = f"{vm_name}/{remote_filename}"
    url = (
        f"https://{host}:{port}/folder/{remote_path}"
        f"?dcPath={dc_name}&dsName={datastore}"
    )
    total = local.stat().st_size
    log.info("Uploading %s:", remote_filename)
    log.info("  local : %s (%d bytes)", local, total)
    log.info("  remote: [%s] %s", datastore, remote_path)

    reporter = Reporter(
        progress,
        key=f"upload:{remote_filename}",
        label=f"upload {remote_filename}",
        total=total,
        unit="B",
        context=progress_context,
    )

    class _ProgressReader:
        def __init__(self, fileobj: IO[bytes], total_bytes: int) -> None:
            self.f = fileobj
            self.total = total_bytes

        def read(self, size: int = -1) -> bytes:
            chunk = self.f.read(size)
            reporter.advance(len(chunk))
            return chunk

        def __len__(self) -> int:
            return self.total

    if not verify_tls:
        suppress_insecure_warnings()

    reporter.start()
    with open(local, "rb") as fh:
        try:
            resp = requests.put(
                url,
                data=_ProgressReader(fh, total),
                auth=(user, password),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(total),
                },
                verify=verify_tls,
                timeout=120,
            )
        except Exception as exc:
            reporter.fail(str(exc))
            raise
    if resp.status_code not in (200, 201):
        reporter.fail(f"HTTP {resp.status_code}")
        raise TaskFailedError(
            f"Upload failed (HTTP {resp.status_code}): {resp.text[:200]}",
            label=f"upload {remote_filename}",
        )
    reporter.finish()
    log.info("  %s: done", remote_filename)


def read_datastore_file(
    host: str,
    user: str,
    password: str,
    port: int,
    datastore: str,
    dc_name: str,
    remote_path: str,
    *,
    verify_tls: bool | str = False,
) -> str:
    """Read a datastore file via HTTPS GET and return its text.

    The HTTPS GET mirror of upload_file(). ``remote_path`` is the path within the
    datastore (e.g. ``ws-2025-base/ws-2025-base.vmx``). Raises on non-200 so the
    caller can fall back to a default.
    """
    url = (
        f"https://{host}:{port}/folder/{remote_path}"
        f"?dcPath={dc_name}&dsName={datastore}"
    )
    log.info("Reading [%s] %s", datastore, remote_path)
    if not verify_tls:
        suppress_insecure_warnings()
    resp = requests.get(
        url,
        auth=(user, password),
        verify=verify_tls,
        timeout=60,
    )
    if resp.status_code != 200:
        raise TaskFailedError(
            f"Read failed (HTTP {resp.status_code}): {resp.text[:200]}",
            label=f"read {remote_path}",
        )
    return resp.text


def get_base_vmdk_size(datastore_obj: vim.Datastore, base: str) -> int:
    """Return the total on-disk size (bytes) of the base VM's .vmdk file(s)."""
    browser = datastore_obj.browser
    details = vim.host.DatastoreBrowser.FileInfo.Details()
    details.fileSize = True
    details.fileType = True
    spec = vim.host.DatastoreBrowser.SearchSpec()
    spec.matchPattern = [f"{base}*.vmdk"]
    spec.details = details
    folder = f"[{datastore_obj.name}] {base}"
    task = browser.SearchDatastore_Task(datastorePath=folder, searchSpec=spec)
    while task.info.state in (
        vim.TaskInfo.State.running,
        vim.TaskInfo.State.queued,
    ):
        time.sleep(0.5)
    task_info = task.info
    if task_info.state != vim.TaskInfo.State.success:
        err = task_info.error
        msg = task_info.error.msg if err else "unknown error"
        raise TaskFailedError(
            f"Could not read base VMDK size: {msg}",
            label="base VMDK size",
            fault=err,
        )
    return sum(
        (getattr(f, "fileSize", 0) or 0) for f in (task_info.result.file or [])
    )
