"""Tests for building a VM from scratch: create_workflow and its building blocks.

Covers the multi-CD-ROM VMX, datastore path handling (names with spaces and
parentheses are real: ``datastore1 (3)``), the blank-disk create and the ISO
existence check.

Like the clone tests, the workflow tests assert **step order** and that nothing
is created when a precondition fails.
"""

from unittest.mock import MagicMock, patch

import pytest
from pyVmomi import vim

from fakes import FakeConnection, FakeContent, FakeTask, make_datastore, make_vm
from vmkit import workflows
from vmkit.datastore import (
    create_virtual_disk,
    datastore_file_exists,
    split_datastore_path,
    vmfs_path,
)
from vmkit.errors import (
    DatastoreFileNotFoundError,
    NotFoundError,
    TaskFailedError,
    VmExistsError,
    VmkitError,
)
from vmkit.vmx import MAX_CDROMS, render_vmx

GIB = 1024**3
DS = "datastore1 (3)"
DS_URL = "ds:///vmfs/volumes/6adfea3d-03c427d4-3c6d-ecebb889e93c/"
DS_ROOT = "/vmfs/volumes/6adfea3d-03c427d4-3c6d-ecebb889e93c"
ISO = "ISO/ubuntu-24.04.5-autoinstall.iso"
SEED = "ISO/ub-24.04.5-seed.iso"


def _settings(vmx: str) -> dict[str, str]:
    out = {}
    for line in vmx.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("vmkit.esxi.time.sleep"), patch("vmkit.workflows.time.sleep"):
        yield


@pytest.fixture
def content():
    return FakeContent(
        datastores=[
            make_datastore(
                DS,
                url=DS_URL,
                files={"ISO": ["ubuntu-24.04.5-autoinstall.iso", "ub-24.04.5-seed.iso"]},
            ),
            make_datastore("datastore2", files={"": ["other.iso"]}),
        ]
    )


@pytest.fixture
def conn(content):
    return FakeConnection(content)


@pytest.fixture
def uploaded():
    """Patch the VMX upload, capturing the VMX text at upload time (the temp file
    is deleted straight after)."""
    captured: dict[str, str] = {}

    def fake_upload(host, user, password, port, datastore, dc_name, vm_name, local_path, **kw):
        with open(local_path, encoding="utf-8") as fh:
            captured[kw["remote_filename"]] = fh.read()

    with patch("vmkit.workflows.upload_file", side_effect=fake_upload) as upload:
        upload.captured = captured
        yield upload


def _create(conn, **overrides):
    kwargs = dict(
        name="ub-24.04.5-base-pre", datastore=DS, guest_os="ubuntu-64",
        disk_gb=40, cpus=2, mem_mb=4096, cdroms=[ISO, SEED],
    )
    kwargs.update(overrides)
    return workflows.create_workflow(conn, **kwargs)


# --------------------------------------------------------------------------- #
# render_vmx: several CD-ROMs                                                 #
# --------------------------------------------------------------------------- #
def _render(**kw):
    return render_vmx("vm1", "00:50:56:00:00:01", 2, 4096, **kw)


def test_cdroms_render_in_order_from_unit_zero():
    s = _settings(_render(cdroms=[f"{DS_ROOT}/{ISO}", f"{DS_ROOT}/{SEED}"]))
    assert s["sata0:0.fileName"] == f"{DS_ROOT}/{ISO}"
    assert s["sata0:1.fileName"] == f"{DS_ROOT}/{SEED}"
    for unit in (0, 1):
        assert s[f"sata0:{unit}.present"] == "TRUE"
        assert s[f"sata0:{unit}.deviceType"] == "cdrom-image"
        assert s[f"sata0:{unit}.startConnected"] == "TRUE"
    assert "sata0:2.present" not in s


def test_single_iso_filename_is_unchanged_by_the_cdroms_option():
    assert _render(iso_filename="web01-config.iso") == _render(cdroms=["web01-config.iso"])


def test_iso_filename_and_cdroms_are_exclusive():
    with pytest.raises(VmkitError):
        _render(iso_filename="a.iso", cdroms=["b.iso"])


def test_too_many_cdroms_are_rejected():
    with pytest.raises(VmkitError):
        _render(cdroms=[f"{i}.iso" for i in range(MAX_CDROMS + 1)])


@pytest.mark.parametrize("bad", ['x".iso', "a\nb.iso", ""])
def test_a_cdrom_path_cannot_break_out_of_its_vmx_line(bad):
    with pytest.raises(VmkitError):
        _render(cdroms=[bad])


# --------------------------------------------------------------------------- #
# Datastore paths                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, expected",
    [
        (f"[{DS}] {ISO}", (DS, ISO)),
        (ISO, ("default-ds", ISO)),
        (f"/{ISO}", ("default-ds", ISO)),
        ("[datastore2] other.iso", ("datastore2", "other.iso")),
        (f"  [{DS}]   {ISO}  ", (DS, ISO)),
    ],
)
def test_split_datastore_path(path, expected):
    assert split_datastore_path(path, "default-ds") == expected


@pytest.mark.parametrize("bad", [f"[{DS} {ISO}", "[] x.iso", f"[{DS}]", ""])
def test_split_datastore_path_rejects_malformed_paths(bad):
    with pytest.raises(VmkitError):
        split_datastore_path(bad, "default-ds")


def test_vmfs_path_uses_the_datastore_uuid_url():
    ds = make_datastore(DS, url=DS_URL)
    assert vmfs_path(ds, ISO) == f"{DS_ROOT}/{ISO}"


def test_vmfs_path_falls_back_to_the_name_symlink():
    ds = make_datastore(DS)
    ds.summary.url = None
    assert vmfs_path(ds, ISO) == f"/vmfs/volumes/{DS}/{ISO}"


def test_file_exists_finds_a_file():
    ds = make_datastore(DS, files={"ISO": ["a.iso"]})
    assert datastore_file_exists(ds, "ISO/a.iso")


def test_file_exists_is_false_for_a_missing_file():
    ds = make_datastore(DS, files={"ISO": ["a.iso"]})
    assert not datastore_file_exists(ds, "ISO/b.iso")


def test_file_exists_is_false_for_a_missing_folder():
    ds = make_datastore(DS, files={"ISO": ["a.iso"]})
    assert not datastore_file_exists(ds, "Nope/a.iso")


def test_file_exists_searches_the_datastore_root():
    ds = make_datastore(DS, files={"": ["a.iso"]})
    assert datastore_file_exists(ds, "a.iso")
    assert ds.browser.SearchDatastore_Task.call_args.kwargs["datastorePath"] == f"[{DS}]"


def test_file_exists_propagates_a_real_search_failure():
    ds = make_datastore(DS)
    ds.browser.SearchDatastore_Task = MagicMock(
        return_value=FakeTask(
            running_polls=0, final_state=vim.TaskInfo.State.error, fault=vim.fault.NoPermission()
        )
    )
    with pytest.raises(TaskFailedError):
        datastore_file_exists(ds, "ISO/a.iso")


# --------------------------------------------------------------------------- #
# create_virtual_disk                                                         #
# --------------------------------------------------------------------------- #
def test_create_virtual_disk_thin_by_default(content):
    create_virtual_disk(content, content.datacenter, DS, "vm1", 40)
    (path, spec), = content.created_disks
    assert path == f"[{DS}] vm1/vm1.vmdk"
    assert spec.diskType == "thin"
    assert spec.capacityKb == 40 * 1024 * 1024


def test_create_virtual_disk_thick(content):
    create_virtual_disk(content, content.datacenter, DS, "vm1", 10, thin=False)
    assert content.created_disks[0][1].diskType == "preallocated"


@pytest.mark.parametrize("size", [0, -5])
def test_create_virtual_disk_rejects_a_non_positive_size(content, size):
    with pytest.raises(VmkitError):
        create_virtual_disk(content, content.datacenter, DS, "vm1", size)
    assert content.created_disks == []


# --------------------------------------------------------------------------- #
# create_workflow                                                             #
# --------------------------------------------------------------------------- #
def test_create_step_order(conn, content, uploaded):
    """Directory, then disk, then VMX, then register -- registering before the
    disk exists yields a VM that cannot boot."""
    order = []
    content.fileManager.MakeDirectory.side_effect = lambda **kw: order.append("mkdir")
    disk_create = content.virtualDiskManager.CreateVirtualDisk_Task.side_effect
    content.virtualDiskManager.CreateVirtualDisk_Task.side_effect = (
        lambda **kw: (order.append("disk"), disk_create(**kw))[1]
    )
    uploaded.side_effect = lambda *a, **kw: order.append("vmx")
    register = content.datacenter.vmFolder.RegisterVM_Task.side_effect
    content.datacenter.vmFolder.RegisterVM_Task.side_effect = (
        lambda **kw: (order.append("register"), register(**kw))[1]
    )
    _create(conn)
    assert order == ["mkdir", "disk", "vmx", "register"]


def test_create_attaches_isos_by_their_uuid_paths(conn, uploaded):
    _create(conn)
    s = _settings(uploaded.captured["ub-24.04.5-base-pre.vmx"])
    assert s["sata0:0.fileName"] == f"{DS_ROOT}/{ISO}"
    assert s["sata0:1.fileName"] == f"{DS_ROOT}/{SEED}"


def test_create_vmx_carries_the_requested_hardware(conn, uploaded):
    _create(conn, cpus=4, mem_mb=8192, network="Lab-Net", mac="00:50:56:01:02:03")
    s = _settings(uploaded.captured["ub-24.04.5-base-pre.vmx"])
    assert s["numvcpus"] == "4"
    assert s["memSize"] == "8192"
    assert s["guestOS"] == "ubuntu-64"
    assert s["ethernet0.networkName"] == "Lab-Net"
    assert s["ethernet0.address"] == "00:50:56:01:02:03"
    assert s["scsi0:0.fileName"] == "ub-24.04.5-base-pre.vmdk"
    assert s["firmware"] == "efi"


def test_create_makes_a_disk_of_the_requested_size(conn, content, uploaded):
    _create(conn, disk_gb=60, thin=False)
    (path, spec), = content.created_disks
    assert path == f"[{DS}] ub-24.04.5-base-pre/ub-24.04.5-base-pre.vmdk"
    assert spec.capacityKb == 60 * 1024 * 1024
    assert spec.diskType == "preallocated"


def test_create_accepts_isos_on_another_datastore(conn, uploaded):
    _create(conn, cdroms=["[datastore2] other.iso"])
    s = _settings(uploaded.captured["ub-24.04.5-base-pre.vmx"])
    assert s["sata0:0.fileName"] == "/vmfs/volumes/uuid-datastore2/other.iso"


def test_create_without_cdroms_attaches_none(conn, uploaded):
    _create(conn, cdroms=[])
    assert "sata0:0.present" not in uploaded.captured["ub-24.04.5-base-pre.vmx"]


def test_a_missing_iso_fails_before_anything_is_created(conn, content, uploaded):
    with pytest.raises(DatastoreFileNotFoundError, match="nope.iso"):
        _create(conn, cdroms=[ISO, "ISO/nope.iso"])
    assert content.made_directories == []
    assert content.created_disks == []
    assert not uploaded.called
    assert content.registered == []


def test_missing_iso_error_is_a_not_found_error(conn, uploaded):
    with pytest.raises(NotFoundError):
        _create(conn, cdroms=["Missing/x.iso"])


def test_create_rejects_a_duplicate_name(conn, content, uploaded):
    content.vms.append(make_vm("ub-24.04.5-base-pre"))
    with pytest.raises(VmExistsError):
        _create(conn)
    assert content.made_directories == []


def test_create_registers_and_reports(conn, content, uploaded):
    result = _create(conn)
    assert content.registered == ["ub-24.04.5-base-pre"]
    assert result.name == "ub-24.04.5-base-pre"
    assert result.guest_os == "ubuntu-64"
    assert result.disk_gb == 40
    assert result.cdroms == [f"{DS_ROOT}/{ISO}", f"{DS_ROOT}/{SEED}"]
    assert result.powered_on is False
    assert result.mac.startswith("00:50:56:")


def test_create_powers_on_when_asked(conn, content, uploaded):
    result = _create(conn, power_on=True)
    assert content.vm("ub-24.04.5-base-pre").PowerOnVM_Task.called
    assert result.powered_on is True


def test_create_does_not_power_on_by_default(conn, content, uploaded):
    _create(conn)
    assert not content.vm("ub-24.04.5-base-pre").PowerOnVM_Task.called


def test_create_cleans_up_the_temp_vmx(conn, uploaded):
    paths = []
    uploaded.side_effect = lambda *a, **kw: paths.append(a[7])
    _create(conn)
    import os
    assert paths and not os.path.exists(paths[0])


def test_create_emits_an_overall_step_counter(conn, uploaded):
    events = []
    _create(conn, power_on=True, progress=events.append)
    overall = [e for e in events if e.key == "workflow:create"]
    assert overall[-1].completed == overall[-1].total == 5


@pytest.mark.parametrize("api_id", ["ubuntu64Guest", "otherLinux64Guest", "windows9Server64Guest"])
def test_create_rejects_a_vsphere_api_guest_id_before_creating_anything(
    conn, content, uploaded, api_id
):
    """ESXi registers a VMX with an API-style guestOS and only refuses it at
    power-on ("The guest operating system 'ubuntu64Guest' is not supported"),
    by which time the directory, disk and VMX all exist."""
    with pytest.raises(VmkitError, match="ubuntu-64"):
        _create(conn, guest_os=api_id)
    assert content.made_directories == [] and content.created_disks == []
    assert not uploaded.called and content.registered == []
