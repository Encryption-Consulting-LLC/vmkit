"""Tests for the clone/update/destroy workflows, capacity planning and cleanup.

The clone tests assert **step order**, not just the end result. The steps are a
sequence of side effects on a real host — directory, disk copy, nvram, ISO, VMX,
register — and getting them out of order produces a VM that registers against a
disk that is not there yet. An end-state assertion cannot see that.
"""

import logging
from unittest.mock import MagicMock, call, patch

import pytest
from pyVmomi import vim

from fakes import (
    FakeConnection,
    FakeContent,
    FakeTask,
    make_datacenter,
    make_datastore,
    make_host,
    make_vm,
)
from vmkit import workflows
from vmkit.errors import (
    InsufficientSpaceError,
    TaskFailedError,
    VmExistsError,
    VmkitError,
    VmNotFoundError,
)
from vmkit.progress import Phase
from vmkit.workflows import CapacityPlan, VmSpec

GIB = 1024**3
BASE_VMDK = 40 * GIB


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("vmkit.esxi.time.sleep"), patch("vmkit.workflows.time.sleep"):
        yield


@pytest.fixture
def content():
    return FakeContent(
        vms=[make_vm("ws-2025-base")],
        datastores=[
            make_datastore(
                "datastore1",
                capacity=1000 * GIB,
                free=800 * GIB,
                vmdk_sizes={"ws-2025-base": BASE_VMDK, "lin-base": 20 * GIB},
            )
        ],
        hosts=[make_host(cores=16, memory_mb=65536, memory_used_mb=16384)],
    )


@pytest.fixture
def conn(content):
    return FakeConnection(content)


@pytest.fixture
def uploads():
    """Patch the two HTTPS paths; everything else runs for real."""
    with patch("vmkit.workflows.upload_file") as upload, patch(
        "vmkit.workflows.read_datastore_file",
        return_value='guestOS = "windows2022srvNext-64"\nethernet0.networkName = "VM Network"\n',
    ) as read:
        yield upload, read


def _clone(conn, **overrides):
    kwargs = dict(
        name="web01", base="ws-2025-base", datastore="datastore1", cpus=2, mem_mb=4096
    )
    kwargs.update(overrides)
    return workflows.clone_workflow(conn, **kwargs)


# --------------------------------------------------------------------------- #
# Connection lifecycle (V1, V12)                                              #
# --------------------------------------------------------------------------- #
def test_connection_password_is_not_in_repr():
    """The default dataclass repr put the ESXi password into every traceback and
    frame-locals capture."""
    conn = workflows.Connection(
        si=MagicMock(), host="esxi.test", user="root", password="hunter2"
    )
    assert "hunter2" not in repr(conn)
    assert "esxi.test" in repr(conn)
    assert conn.password == "hunter2"


def test_connection_context_manager_closes():
    si = MagicMock()
    with patch("vmkit.workflows.disconnect") as disc:
        with workflows.Connection(si=si, host="h", user="u", password="p") as conn:
            assert conn.si is si
    disc.assert_called_once_with(si)


def test_connection_closes_even_when_the_body_raises():
    si = MagicMock()
    with patch("vmkit.workflows.disconnect") as disc:
        with pytest.raises(ValueError):
            with workflows.Connection(si=si, host="h", user="u", password="p"):
                raise ValueError("boom")
    disc.assert_called_once_with(si)


def test_close_connection_helper():
    si = MagicMock()
    conn = workflows.Connection(si=si, host="h", user="u", password="p")
    with patch("vmkit.workflows.disconnect") as disc:
        workflows.close_connection(conn)
    disc.assert_called_once_with(si)


def test_open_connection_threads_options_through():
    with patch("vmkit.workflows.connect", return_value=MagicMock()) as connect:
        conn = workflows.open_connection(
            "esxi.test", "root", "pw", 443, register_atexit=False, verify_tls=True
        )
    assert connect.call_args.kwargs == {"register_atexit": False, "verify_tls": True}
    assert conn.verify_tls is True


# --------------------------------------------------------------------------- #
# clone_workflow — step order (V17)                                           #
# --------------------------------------------------------------------------- #
def test_clone_step_order(conn, content, uploads):
    upload, _ = uploads
    _clone(conn, iso_path="/tmp/web01.iso")

    # Directory first, then disk, then nvram — a register before the copies would
    # produce a VM pointing at a disk that does not exist yet.
    assert content.made_directories == [("[datastore1] web01", "True")]
    assert content.copied == [
        ("[datastore1] ws-2025-base/ws-2025-base.vmdk", "[datastore1] web01/web01.vmdk"),
        ("[datastore1] ws-2025-base/ws-2025-base.nvram", "[datastore1] web01/web01.nvram"),
    ]
    # ISO before VMX, and both before registration.
    assert [c.kwargs["remote_filename"] for c in upload.call_args_list] == [
        "web01-config.iso",
        "web01.vmx",
    ]
    assert content.datacenter.vmFolder.RegisterVM_Task.called


def test_clone_without_iso_skips_the_iso_upload(conn, uploads):
    upload, _ = uploads
    _clone(conn)
    assert [c.kwargs["remote_filename"] for c in upload.call_args_list] == ["web01.vmx"]


def test_clone_returns_result(conn, uploads):
    result = _clone(conn, cpus=4, mem_mb=8192)
    assert result.name == "web01"
    assert result.guest_os == "windows2022srvNext-64"
    assert result.network == "VM Network"
    assert result.powered_on is False
    assert result.disk is not None
    assert result.disk.vmdk_size == BASE_VMDK


def test_clone_rejects_a_duplicate_name(conn, uploads):
    with pytest.raises(VmExistsError, match="ws-2025-base"):
        _clone(conn, name="ws-2025-base")


def test_clone_checks_disk_space(conn, content, uploads):
    content.datastores[0].summary.freeSpace = 10 * GIB  # 99% used after
    with pytest.raises(InsufficientSpaceError):
        _clone(conn)


def test_clone_can_skip_the_disk_check(conn, content, uploads):
    content.datastores[0].summary.freeSpace = 10 * GIB
    result = _clone(conn, skip_disk_check=True)
    assert result.disk is None


def test_clone_uses_the_supplied_mac(conn, uploads):
    assert _clone(conn, mac="00:50:56:11:22:33").mac == "00:50:56:11:22:33"


def test_clone_excludes_used_macs(conn, uploads):
    """random_mac cannot see addresses assigned elsewhere in the batch."""
    with patch("vmkit.workflows.random_mac", return_value="00:50:56:aa:aa:aa") as gen:
        _clone(conn, used_macs={"00:50:56:11:22:33"})
    gen.assert_called_once_with({"00:50:56:11:22:33"})


def test_clone_network_reaches_the_vmx(conn, uploads):
    """The port group was hardcoded before 0.4.0."""
    with patch("vmkit.workflows.render_vmx", wraps=workflows.render_vmx) as render:
        result = _clone(conn, network="DMZ-VLAN20")
    assert render.call_args.kwargs["network"] == "DMZ-VLAN20"
    assert result.network == "DMZ-VLAN20"


def test_clone_defaults_to_vm_network(conn, uploads):
    with patch("vmkit.workflows.render_vmx", wraps=workflows.render_vmx) as render:
        _clone(conn)
    assert render.call_args.kwargs["network"] == "VM Network"


def test_clone_detects_guest_os_from_the_base_vmx(conn):
    with patch("vmkit.workflows.upload_file"), patch(
        "vmkit.workflows.read_datastore_file", return_value='guestOS = "ubuntu64Guest"'
    ):
        assert _clone(conn).guest_os == "ubuntu64Guest"


def test_clone_falls_back_when_the_base_vmx_is_unreadable(conn):
    """A base whose VMX cannot be read must not fail the clone."""
    with patch("vmkit.workflows.upload_file"), patch(
        "vmkit.workflows.read_datastore_file", side_effect=OSError("404")
    ):
        assert _clone(conn).guest_os == "windows2022srvNext-64"


def test_explicit_guest_os_wins_over_the_vmx(conn):
    """The base VMX is still read -- the NIC PCI slot check needs it either way --
    but an explicit guest_os is not overridden by what it says."""
    with patch("vmkit.workflows.upload_file"), patch(
        "vmkit.workflows.read_datastore_file",
        return_value='guestOS = "ubuntu64Guest"',
    ):
        assert _clone(conn, guest_os="rhel9_64Guest").guest_os == "rhel9_64Guest"


def test_clone_powers_on_when_asked(conn, content, uploads):
    result = _clone(conn, power_on=True)
    assert result.powered_on is True
    assert content.vm("web01").PowerOnVM_Task.called


def test_clone_does_not_power_on_by_default(conn, content, uploads):
    result = _clone(conn)
    assert result.powered_on is False
    assert not content.vm("web01").PowerOnVM_Task.called


def test_clone_registers_the_vm(conn, content, uploads):
    _clone(conn)
    assert content.registered == ["web01"]


def test_clone_raises_if_the_vm_never_appears(conn, content, uploads):
    with patch("vmkit.workflows.list_vm_names", side_effect=[set(), set()]):
        with pytest.raises(VmkitError, match="not found in inventory"):
            _clone(conn)


def test_clone_cleans_up_the_temp_vmx(conn, uploads):
    with patch("vmkit.workflows._unlink_quietly") as unlink:
        _clone(conn)
    assert unlink.called


def test_clone_cleans_up_the_temp_vmx_on_failure(conn, uploads):
    upload, _ = uploads
    upload.side_effect = TaskFailedError("upload failed")
    with patch("vmkit.workflows._unlink_quietly") as unlink:
        with pytest.raises(TaskFailedError):
            _clone(conn)
    assert unlink.called


# --------------------------------------------------------------------------- #
# clone_workflow — progress (V5, V16)                                         #
# --------------------------------------------------------------------------- #
def test_clone_emits_an_overall_step_counter(conn, uploads):
    events = []
    _clone(conn, progress=events.append, iso_path="/tmp/x.iso")

    workflow_events = [e for e in events if e.key == "workflow:clone"]
    assert workflow_events, "no overall progress emitted"
    assert workflow_events[0].total == 5  # vmdk, nvram, iso, vmx, register
    assert workflow_events[0].unit == "steps"
    assert workflow_events[-1].phase == Phase.END


def test_overall_step_total_tracks_the_options(conn, uploads):
    """Consumers used to hardcode this count and derive it from their own request
    flags, so any change to the step sequence broke their bar silently."""
    for index, (kwargs, expected) in enumerate(
        (
            ({}, 4),
            ({"iso_path": "/tmp/x.iso"}, 5),
            ({"power_on": True}, 5),
            ({"iso_path": "/tmp/x.iso", "power_on": True}, 6),
        )
    ):
        events = []
        # A distinct name per iteration: each clone registers its VM, so reusing
        # one name would trip the uniqueness check on the next pass.
        _clone(conn, name=f"web{index:02d}", progress=events.append, **kwargs)
        assert [e for e in events if e.key == "workflow:clone"][0].total == expected


def test_overall_progress_reaches_its_total(conn, uploads):
    events = []
    _clone(conn, progress=events.append, iso_path="/tmp/x.iso", power_on=True)
    final = [e for e in events if e.key == "workflow:clone"][-1]
    assert final.completed == final.total == 6


def test_clone_progress_context_scopes_every_event(conn, uploads):
    events = []
    _clone(conn, progress=events.append, progress_context="web01")
    assert events, "no progress emitted"
    assert all(e.context == "web01" for e in events)
    assert all(e.key.startswith("web01/") for e in events)


def test_concurrent_clones_produce_distinct_keys(conn, uploads):
    events = []
    _clone(conn, name="web01", progress=events.append, progress_context="web01")
    _clone(conn, name="web02", progress=events.append, progress_context="web02")
    vmdk_keys = {e.key for e in events if "vmdk-copy" in e.key}
    assert vmdk_keys == {"web01/task:vmdk-copy", "web02/task:vmdk-copy"}


def test_overall_progress_reports_failure(conn, uploads):
    upload, _ = uploads
    upload.side_effect = TaskFailedError("upload failed")
    events = []
    with pytest.raises(TaskFailedError):
        _clone(conn, progress=events.append)
    workflow_events = [e for e in events if e.key == "workflow:clone"]
    assert workflow_events[-1].phase == Phase.ERROR


def test_a_raising_progress_callback_does_not_abort_the_clone(conn, uploads):
    """The V6 guard, end to end: a clone must survive a broken publisher."""
    def explode(_event):
        raise RuntimeError("websocket closed")

    assert _clone(conn, progress=explode).name == "web01"


# --------------------------------------------------------------------------- #
# plan_capacity (V2)                                                          #
# --------------------------------------------------------------------------- #
def test_plan_capacity_sums_the_whole_batch(content):
    """validate_disk_usage asks 'does one more fit', which N concurrent callers
    all answer yes to while collectively filling the datastore."""
    specs = [VmSpec(name=f"web{i:02d}", base="ws-2025-base") for i in range(10)]
    plan = workflows.plan_capacity(content, "datastore1", specs)

    assert plan.vm_count == 10
    assert plan.required == 10 * BASE_VMDK
    assert plan.used_after == plan.used + 10 * BASE_VMDK


def test_plan_capacity_detects_a_batch_that_does_not_fit(content):
    specs = [VmSpec(name=f"web{i:02d}", base="ws-2025-base") for i in range(20)]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert not plan.fits
    assert plan.problems
    with pytest.raises(InsufficientSpaceError):
        plan.raise_if_insufficient()


def test_each_vm_fits_individually_but_the_batch_does_not(content):
    """The exact failure the per-call check cannot see."""
    single = workflows.validate_disk_usage(content, "datastore1", "ws-2025-base", 80.0)
    assert single.pct_after < 80.0

    specs = [VmSpec(name=f"web{i:02d}", base="ws-2025-base") for i in range(16)]
    assert not workflows.plan_capacity(content, "datastore1", specs).fits


def test_plan_capacity_measures_each_distinct_base_once(content):
    """50 clones from one base should cost one datastore search, not 50."""
    specs = [VmSpec(name=f"web{i:02d}", base="ws-2025-base") for i in range(50)]
    workflows.plan_capacity(content, "datastore1", specs)
    assert content.datastores[0].browser.SearchDatastore_Task.call_count == 1


def test_plan_capacity_handles_mixed_bases(content):
    specs = [
        VmSpec(name="win01", base="ws-2025-base"),
        VmSpec(name="lin01", base="lin-base"),
        VmSpec(name="win02", base="ws-2025-base"),
    ]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert plan.required == 2 * BASE_VMDK + 20 * GIB
    assert content.datastores[0].browser.SearchDatastore_Task.call_count == 2


def test_plan_capacity_reports_cpu_and_memory(content):
    specs = [VmSpec(name=f"web{i}", base="ws-2025-base", cpus=4, mem_mb=8192) for i in range(3)]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert plan.total_cpus == 12
    assert plan.total_mem_mb == 24576
    assert plan.host_cpu_cores == 16
    assert plan.host_memory_mb == 65536


def test_plan_capacity_warns_on_memory_overcommit(content):
    """Nothing read host CPU/RAM at all before 0.4.0."""
    specs = [VmSpec(name=f"web{i}", base="ws-2025-base", mem_mb=32768) for i in range(4)]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert any("RAM" in w for w in plan.warnings)


def test_plan_capacity_warns_on_cpu_overcommit(content):
    specs = [VmSpec(name=f"web{i}", base="ws-2025-base", cpus=16) for i in range(8)]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert any("vCPU" in w for w in plan.warnings)


def test_overcommit_is_a_warning_not_a_failure(content):
    """ESXi overcommits both by design; the threshold is the caller's policy."""
    specs = [VmSpec(name="web0", base="ws-2025-base", cpus=32, mem_mb=65536)]
    plan = workflows.plan_capacity(content, "datastore1", specs)
    assert plan.fits
    plan.raise_if_insufficient()


def test_plan_capacity_survives_an_unreadable_host(content):
    """Advisory reporting must not block a plan whose disk arithmetic is sound."""
    content.hosts = []
    plan = workflows.plan_capacity(content, "datastore1", [VmSpec("web01", "ws-2025-base")])
    assert plan.fits
    assert plan.host is None


def test_plan_capacity_can_skip_the_host_read(content):
    plan = workflows.plan_capacity(
        content, "datastore1", [VmSpec("web01", "ws-2025-base")], read_host_capacity=False
    )
    assert plan.host is None
    assert plan.fits


def test_plan_capacity_for_an_empty_batch(content):
    plan = workflows.plan_capacity(content, "datastore1", [])
    assert plan.vm_count == 0
    assert plan.required == 0
    assert plan.fits


def test_capacity_plan_is_immutable(content):
    plan = workflows.plan_capacity(content, "datastore1", [VmSpec("web01", "ws-2025-base")])
    with pytest.raises(Exception):
        plan.fits = True


def test_raise_if_insufficient_returns_self_when_it_fits(content):
    plan = workflows.plan_capacity(content, "datastore1", [VmSpec("web01", "ws-2025-base")])
    assert plan.raise_if_insufficient() is plan


# --------------------------------------------------------------------------- #
# cleanup_partial_clone (V10)                                                 #
# --------------------------------------------------------------------------- #
def test_cleanup_deletes_the_datastore_directory(conn, content):
    result = workflows.cleanup_partial_clone(conn, "web01", "datastore1")
    assert result.deleted is True
    assert content.deleted_paths == ["[datastore1] web01"]


def test_cleanup_refuses_to_delete_a_registered_vms_files(conn, content):
    """That is a live VM, not a partial clone."""
    content.vms.append(make_vm("web01"))
    with pytest.raises(VmExistsError, match="destroy_workflow"):
        workflows.cleanup_partial_clone(conn, "web01", "datastore1")
    assert content.deleted_paths == []


def test_cleanup_is_safe_when_there_is_nothing_to_delete(conn, content):
    """A reconcile sweep calls this unconditionally."""
    content.fileManager.DeleteDatastoreFile_Task.side_effect = vim.fault.FileNotFound()
    result = workflows.cleanup_partial_clone(conn, "web01", "datastore1")
    assert result.deleted is False


def test_cleanup_tolerates_a_file_not_found_task_failure(conn, content):
    fault = vim.fault.FileNotFound()
    content.fileManager.DeleteDatastoreFile_Task.side_effect = None
    content.fileManager.DeleteDatastoreFile_Task.return_value = FakeTask(
        running_polls=0, final_state=vim.TaskInfo.State.error, fault=fault
    )
    assert workflows.cleanup_partial_clone(conn, "web01", "datastore1").deleted is False


def test_cleanup_propagates_a_real_failure(conn, content):
    content.fileManager.DeleteDatastoreFile_Task.side_effect = None
    content.fileManager.DeleteDatastoreFile_Task.return_value = FakeTask(
        running_polls=0,
        final_state=vim.TaskInfo.State.error,
        fault=vim.fault.NoPermission(),
    )
    with pytest.raises(TaskFailedError):
        workflows.cleanup_partial_clone(conn, "web01", "datastore1")


# --------------------------------------------------------------------------- #
# update_workflow                                                             #
# --------------------------------------------------------------------------- #
def test_update_preserves_unspecified_hardware(conn, content, uploads):
    content.vms.append(make_vm("web01", cpus=8, mem_mb=16384, mac="00:50:56:de:ad:be"))
    result = workflows.update_workflow(conn, name="web01", datastore="datastore1")
    assert (result.cpus, result.mem_mb, result.mac) == (8, 16384, "00:50:56:de:ad:be")


def test_update_applies_overrides(conn, content, uploads):
    content.vms.append(make_vm("web01", cpus=2, mem_mb=4096))
    result = workflows.update_workflow(
        conn, name="web01", datastore="datastore1", cpus=4, mem_mb=8192
    )
    assert (result.cpus, result.mem_mb) == (4, 8192)


def test_update_preserves_the_existing_network(conn, content):
    """An update must never silently move a VM onto a different port group."""
    content.vms.append(make_vm("web01"))
    with patch("vmkit.workflows.upload_file"), patch(
        "vmkit.workflows.read_datastore_file",
        return_value='guestOS = "ubuntu64Guest"\nethernet0.networkName = "DMZ"\n',
    ):
        result = workflows.update_workflow(conn, name="web01", datastore="datastore1")
    assert result.network == "DMZ"
    assert result.guest_os == "ubuntu64Guest"


def test_update_can_change_the_network(conn, content, uploads):
    content.vms.append(make_vm("web01"))
    result = workflows.update_workflow(
        conn, name="web01", datastore="datastore1", network="Lab-Net"
    )
    assert result.network == "Lab-Net"


def test_update_reads_the_vmx_once(conn, content):
    """Guest OS and port group come from the same file; two reads would be two
    HTTPS round trips per VM."""
    content.vms.append(make_vm("web01"))
    with patch("vmkit.workflows.upload_file"), patch(
        "vmkit.workflows.read_datastore_file",
        return_value='guestOS = "ubuntu64Guest"\nethernet0.networkName = "DMZ"\n',
    ) as read:
        workflows.update_workflow(conn, name="web01", datastore="datastore1")
    assert read.call_count == 1


def test_update_powers_off_a_running_vm_first(conn, content, uploads):
    vm = make_vm("web01", powered_on=True)
    content.vms.append(vm)
    workflows.update_workflow(conn, name="web01", datastore="datastore1")
    assert vm.PowerOffVM_Task.called


def test_update_iso_actions(conn, content, uploads):
    content.vms.append(make_vm("web01"))
    kept = workflows.update_workflow(conn, name="web01", datastore="datastore1")
    assert kept.iso_action == "kept"

    removed = workflows.update_workflow(
        conn, name="web01", datastore="datastore1", remove_iso=True
    )
    assert removed.iso_action == "removed"

    uploaded = workflows.update_workflow(
        conn, name="web01", datastore="datastore1", iso_path="/tmp/x.iso"
    )
    assert uploaded.iso_action == "uploaded"


def test_remove_iso_drops_the_cdrom_from_the_vmx(conn, content, uploads):
    """The config ISO carries the agent token and, for a DC, an Administrator
    password in plaintext; detaching it is the point of this option."""
    content.vms.append(make_vm("web01"))
    with patch("vmkit.workflows.render_vmx", wraps=workflows.render_vmx) as render:
        workflows.update_workflow(
            conn, name="web01", datastore="datastore1", remove_iso=True
        )
    assert render.call_args.kwargs["iso_filename"] is None


def test_update_missing_vm(conn, uploads):
    with pytest.raises(VmNotFoundError):
        workflows.update_workflow(conn, name="nope", datastore="datastore1")


# --------------------------------------------------------------------------- #
# destroy_workflow                                                            #
# --------------------------------------------------------------------------- #
def test_destroy_powered_off_vm(conn, content):
    vm = make_vm("web01", powered_on=False)
    content.vms.append(vm)
    result = workflows.destroy_workflow(conn, name="web01")
    assert result.was_powered_on is False
    assert vm.Destroy_Task.called
    assert not vm.PowerOffVM_Task.called


def test_destroy_powers_off_first(conn, content):
    vm = make_vm("web01", powered_on=True)
    content.vms.append(vm)
    result = workflows.destroy_workflow(conn, name="web01")
    assert result.was_powered_on is True
    assert vm.PowerOffVM_Task.called
    assert vm.Destroy_Task.called


def test_destroy_missing_vm(conn):
    with pytest.raises(VmNotFoundError, match="nope"):
        workflows.destroy_workflow(conn, name="nope")


def test_destroy_failure_is_a_task_failed_error(conn, content):
    vm = make_vm("web01")
    vm.Destroy_Task.return_value = FakeTask(
        running_polls=0, final_state=vim.TaskInfo.State.error, error_msg="file locked"
    )
    content.vms.append(vm)
    with pytest.raises(TaskFailedError, match="file locked"):
        workflows.destroy_workflow(conn, name="web01")


# --------------------------------------------------------------------------- #
# get_vm_config                                                               #
# --------------------------------------------------------------------------- #
def test_get_vm_config_picks_the_nic_not_the_first_device():
    vm = make_vm("web01", cpus=4, mem_mb=8192, mac="00:50:56:01:02:03")
    assert workflows.get_vm_config(vm) == {
        "cpus": 4,
        "mem_mb": 8192,
        "mac": "00:50:56:01:02:03",
    }


def test_get_vm_config_without_a_nic():
    vm = make_vm("web01")
    vm.config.hardware.device = [MagicMock(spec=vim.VirtualDisk)]
    assert workflows.get_vm_config(vm)["mac"] is None


# --------------------------------------------------------------------------- #
# warn_on_nic_slot_mismatch                                                   #
# --------------------------------------------------------------------------- #
def test_nic_slot_mismatch_warns(caplog):
    """A base built at a different PCI slot boots with a different interface name
    than its own clones -- silent until something baked into the image names one."""
    with caplog.at_level(logging.WARNING):
        workflows.warn_on_nic_slot_mismatch(
            'ethernet0.pciSlotNumber = "34"', "ub-24.04-base"
        )
    assert "ub-24.04-base" in caplog.text
    assert "34" in caplog.text


@pytest.mark.parametrize(
    "vmx",
    [
        None,                                   # base VMX unreadable
        'ethernet0.pciSlotNumber = "33"',       # matches what the clone gets
        'guestOS = "ubuntu64Guest"',            # no slot line; ESXi assigns it
    ],
)
def test_nic_slot_mismatch_silent(vmx, caplog):
    with caplog.at_level(logging.WARNING):
        workflows.warn_on_nic_slot_mismatch(vmx, "ub-24.04-base")
    assert caplog.text == ""
