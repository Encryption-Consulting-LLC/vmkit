"""Tests for session lifecycle, inventory lookup, discovery and wait_for_task."""

import atexit
import ssl
import socket
from unittest.mock import MagicMock, patch

import pytest
from pyVmomi import vim

from fakes import (
    FakeContent,
    FakeTask,
    NeverEndingTask,
    make_datacenter,
    make_datastore,
    make_host,
    make_network,
    make_vm,
)
from vmkit import esxi
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
from vmkit.progress import Phase


@pytest.fixture(autouse=True)
def no_sleep():
    """wait_for_task polls with time.sleep; the tests must not actually wait."""
    with patch("vmkit.esxi.time.sleep"):
        yield


# --------------------------------------------------------------------------- #
# connect / disconnect (V1, V8, V14)                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_atexit():
    """Keep the module's hook registry isolated between tests."""
    esxi._atexit_hooks.clear()
    yield
    esxi._atexit_hooks.clear()


def test_connect_returns_service_instance(clean_atexit):
    si = MagicMock()
    with patch("vmkit.esxi.SmartConnect", return_value=si) as smart:
        got = esxi.connect("esxi.test", "root", "pw", 443, register_atexit=False)
    assert got is si
    assert smart.call_args.kwargs["host"] == "esxi.test"


def test_connect_registers_atexit_by_default(clean_atexit):
    si = MagicMock()
    with patch("vmkit.esxi.SmartConnect", return_value=si), patch(
        "vmkit.esxi.atexit.register"
    ) as register:
        esxi.connect("esxi.test", "root", "pw", 443)
    assert register.called
    assert id(si) in esxi._atexit_hooks


def test_atexit_registration_is_opt_out(clean_atexit):
    """A server opening one connection per batch must not accumulate hooks that
    retain every session (and its credentials) until the process exits."""
    si = MagicMock()
    with patch("vmkit.esxi.SmartConnect", return_value=si), patch(
        "vmkit.esxi.atexit.register"
    ) as register:
        esxi.connect("esxi.test", "root", "pw", 443, register_atexit=False)
    assert not register.called
    assert esxi._atexit_hooks == {}


def test_disconnect_closes_the_session(clean_atexit):
    si = MagicMock()
    with patch("vmkit.esxi.SmartConnect", return_value=si):
        esxi.connect("esxi.test", "root", "pw", 443, register_atexit=False)
    with patch("vmkit.esxi.Disconnect") as disc:
        esxi.disconnect(si)
    disc.assert_called_once_with(si)


def test_disconnect_removes_only_its_own_atexit_hook(clean_atexit):
    """atexit.unregister matches by equality. A shared function object would make
    closing one session strip every other session's hook too."""
    first, second = MagicMock(), MagicMock()
    with patch("vmkit.esxi.SmartConnect", side_effect=[first, second]):
        esxi.connect("a", "root", "pw", 443)
        esxi.connect("b", "root", "pw", 443)

    assert len(esxi._atexit_hooks) == 2
    with patch("vmkit.esxi.Disconnect"):
        esxi.disconnect(first)

    assert id(first) not in esxi._atexit_hooks
    assert id(second) in esxi._atexit_hooks, "closing one session unregistered another"


def test_disconnect_is_idempotent(clean_atexit):
    si = MagicMock()
    with patch("vmkit.esxi.SmartConnect", return_value=si):
        esxi.connect("esxi.test", "root", "pw", 443)
    with patch("vmkit.esxi.Disconnect"):
        esxi.disconnect(si)
        esxi.disconnect(si)  # must not raise


def test_disconnect_survives_a_dead_session(clean_atexit):
    """A session the host already expired must not turn cleanup into a failure."""
    si = MagicMock()
    with patch("vmkit.esxi.Disconnect", side_effect=Exception("session expired")):
        esxi.disconnect(si)


def test_bad_credentials_raise_authentication_error(clean_atexit):
    with patch("vmkit.esxi.SmartConnect", side_effect=vim.fault.InvalidLogin()):
        with pytest.raises(AuthenticationError, match="invalid username or password"):
            esxi.connect("esxi.test", "root", "bad", 443)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (socket.gaierror("name resolution failed"), HostUnreachableError),
        (ConnectionRefusedError("refused"), HostUnreachableError),
        (TimeoutError("timed out"), HostUnreachableError),
        (ssl.SSLCertVerificationError("bad cert"), TlsError),
        (ssl.SSLError("handshake"), TlsError),
        (vim.fault.NoPermission(), PermissionDeniedError),
        (RuntimeError("something else"), ConnectionFailedError),
    ],
)
def test_connection_failures_are_classified(clean_atexit, exc, expected):
    """Everything used to collapse into one type, which is why consumers walked
    the __cause__ chain by hand looking for NoPermission."""
    with patch("vmkit.esxi.SmartConnect", side_effect=exc):
        with pytest.raises(expected):
            esxi.connect("esxi.test", "root", "pw", 443)


def test_classification_walks_the_cause_chain(clean_atexit):
    wrapped = RuntimeError("connection failed")
    wrapped.__cause__ = vim.fault.NoPermission()
    with patch("vmkit.esxi.SmartConnect", side_effect=wrapped):
        with pytest.raises(PermissionDeniedError):
            esxi.connect("esxi.test", "root", "pw", 443)


def test_every_classified_failure_is_a_connection_failed_error(clean_atexit):
    """Subclassing keeps `except ConnectionFailedError` working for old callers."""
    for exc in (socket.gaierror("x"), ssl.SSLError("x"), vim.fault.NoPermission()):
        with patch("vmkit.esxi.SmartConnect", side_effect=exc):
            with pytest.raises(ConnectionFailedError):
                esxi.connect("esxi.test", "root", "pw", 443)


def test_verify_tls_off_by_default_disables_verification(clean_atexit):
    with patch("vmkit.esxi.SmartConnect", return_value=MagicMock()) as smart:
        esxi.connect("esxi.test", "root", "pw", 443, register_atexit=False)
    ctx = smart.call_args.kwargs["sslContext"]
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_tls_on_verifies(clean_atexit):
    with patch("vmkit.esxi.SmartConnect", return_value=MagicMock()) as smart:
        esxi.connect("esxi.test", "root", "pw", 443, register_atexit=False, verify_tls=True)
    ctx = smart.call_args.kwargs["sslContext"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# --------------------------------------------------------------------------- #
# Inventory lookup                                                            #
# --------------------------------------------------------------------------- #
def test_get_datacenter():
    dc = make_datacenter("dc-1")
    content = FakeContent(datacenter=dc)
    assert esxi.get_datacenter(content) is dc


def test_get_datacenter_raises_when_absent():
    content = FakeContent()
    content.rootFolder.childEntity = [MagicMock()]  # not a Datacenter
    with pytest.raises(DatacenterNotFoundError):
        esxi.get_datacenter(content)


def test_datacenter_not_found_is_still_a_runtime_error():
    """Dual inheritance keeps `except RuntimeError` working for old callers."""
    assert issubclass(DatacenterNotFoundError, RuntimeError)
    assert issubclass(DatastoreNotFoundError, RuntimeError)


def test_list_vm_names():
    content = FakeContent(vms=[make_vm("web01"), make_vm("web02")])
    assert esxi.list_vm_names(content) == {"web01", "web02"}


def test_list_vm_names_skips_concurrently_deleted_vms():
    """A VM destroyed between the view snapshot and the property read must simply
    not be listed, not blow up the enumeration."""
    content = FakeContent(vms=[make_vm("web01"), make_vm("gone", destroyed=True)])
    assert esxi.list_vm_names(content) == {"web01"}


def test_get_vm_by_name():
    target = make_vm("web02")
    content = FakeContent(vms=[make_vm("web01"), target])
    assert esxi.get_vm_by_name(content, "web02") is target
    assert esxi.get_vm_by_name(content, "nope") is None


def test_get_datastore():
    ds = make_datastore("datastore1")
    content = FakeContent(datastores=[ds])
    assert esxi.get_datastore(content, "datastore1") is ds


def test_get_datastore_raises_for_unknown_name():
    content = FakeContent(datastores=[make_datastore("datastore1")])
    with pytest.raises(DatastoreNotFoundError, match="datastore9"):
        esxi.get_datastore(content, "datastore9")


def test_container_views_are_always_destroyed():
    """Views are server-side objects; leaking one per lookup would accumulate on
    the host across a long-lived process."""
    content = FakeContent(vms=[make_vm("web01")], datastores=[make_datastore("ds")])
    esxi.list_vm_names(content)
    esxi.get_vm_by_name(content, "web01")
    esxi.get_datastore(content, "ds")
    assert content.destroyed_views == 3


def test_view_is_destroyed_even_when_lookup_raises():
    content = FakeContent(datastores=[])
    with pytest.raises(DatastoreNotFoundError):
        esxi.get_datastore(content, "missing")
    assert content.destroyed_views == 1


# --------------------------------------------------------------------------- #
# Discovery (V3)                                                              #
# --------------------------------------------------------------------------- #
def test_list_datastores():
    content = FakeContent(
        datastores=[
            make_datastore("zzz", capacity=100, free=40),
            make_datastore("aaa", capacity=200, free=50),
        ]
    )
    out = esxi.list_datastores(content)
    assert [d.name for d in out] == ["aaa", "zzz"]  # sorted for stable dropdowns
    assert out[1].used == 60
    assert out[1].pct_used == 60.0


def test_list_datastores_handles_zero_capacity():
    content = FakeContent(datastores=[make_datastore("empty", capacity=0, free=0)])
    assert esxi.list_datastores(content)[0].pct_used == 0.0


def test_list_networks():
    content = FakeContent(networks=[make_network("DMZ"), make_network("VM Network")])
    out = esxi.list_networks(content)
    assert [n.name for n in out] == ["DMZ", "VM Network"]
    assert all(n.accessible for n in out)


def test_list_vms_reports_hardware():
    content = FakeContent(vms=[make_vm("web01", cpus=4, mem_mb=8192)])
    info = esxi.list_vms(content)[0]
    assert (info.name, info.cpus, info.mem_mb) == ("web01", 4, 8192)
    assert info.guest_os == "windows2022srvNext-64"


def test_list_base_vms_excludes_powered_on_vms():
    """Cloning copies the base VMDK server-side without snapshotting, so a
    powered-on base would be copied mid-write."""
    content = FakeContent(
        vms=[
            make_vm("running", powered_on=True),
            make_vm("stopped", powered_on=False),
            make_vm("template", powered_on=False, is_template=True),
        ]
    )
    assert {vm.name for vm in esxi.list_base_vms(content)} == {"stopped", "template"}


def test_list_base_vms_includes_a_powered_on_template():
    content = FakeContent(vms=[make_vm("tpl", powered_on=True, is_template=True)])
    assert [vm.name for vm in esxi.list_base_vms(content)] == ["tpl"]


def test_get_host_capacity():
    content = FakeContent(
        hosts=[
            make_host(
                "esxi-01", cores=16, mhz_per_core=2400, cpu_used_mhz=4800,
                memory_mb=65536, memory_used_mb=16384,
            )
        ]
    )
    cap = esxi.get_host_capacity(content)
    assert cap.name == "esxi-01"
    assert cap.cpu_mhz_total == 16 * 2400
    assert cap.cpu_mhz_free == 16 * 2400 - 4800
    assert cap.memory_mb_total == 65536
    assert cap.memory_mb_free == 65536 - 16384


def test_host_capacity_free_never_goes_negative():
    """Overcommitted hosts report usage above capacity; a negative 'free' would
    propagate into a caller's arithmetic as a phantom surplus."""
    content = FakeContent(
        hosts=[make_host(memory_mb=1024, memory_used_mb=2048, cpu_used_mhz=10**9)]
    )
    cap = esxi.get_host_capacity(content)
    assert cap.memory_mb_free == 0
    assert cap.cpu_mhz_free == 0


def test_get_host_capacity_with_no_hosts():
    content = FakeContent(hosts=[])
    with pytest.raises(DatacenterNotFoundError):
        esxi.get_host_capacity(content)


# --------------------------------------------------------------------------- #
# wait_for_task (V7, V9)                                                      #
# --------------------------------------------------------------------------- #
def test_successful_task_returns_result():
    task = FakeTask(running_polls=2, result="the-vm")
    assert esxi.wait_for_task(task, "Register VM") == "the-vm"


def test_success_emits_start_updates_and_end():
    events = []
    task = FakeTask(running_polls=3, progress_sequence=[10, 40, 70])
    esxi.wait_for_task(task, "VMDK copy", events.append)

    assert events[0].phase == Phase.START
    assert events[-1].phase == Phase.END
    assert events[-1].completed == 100
    assert [e.completed for e in events if e.phase == Phase.UPDATE] == [10, 40, 70]


def test_task_key_is_derived_from_the_label():
    events = []
    esxi.wait_for_task(FakeTask(running_polls=0), "VMDK copy", events.append)
    assert events[0].key == "task:vmdk-copy"


def test_progress_context_scopes_the_key():
    events = []
    esxi.wait_for_task(
        FakeTask(running_polls=0), "VMDK copy", events.append, progress_context="web01"
    )
    assert events[0].key == "web01/task:vmdk-copy"
    assert events[0].context == "web01"


def test_failed_task_raises_task_failed_error():
    """The most common real failure; it used to escape as a bare RuntimeError."""
    task = FakeTask(running_polls=1, final_state=vim.TaskInfo.State.error, error_msg="disk full")
    with pytest.raises(TaskFailedError) as exc:
        esxi.wait_for_task(task, "VMDK copy")
    assert "disk full" in str(exc.value)
    assert exc.value.label == "VMDK copy"


def test_task_failed_error_is_still_a_runtime_error():
    assert issubclass(TaskFailedError, RuntimeError)


def test_failed_task_carries_the_fault_object():
    fault = vim.fault.InsufficientDisks()
    task = FakeTask(running_polls=0, final_state=vim.TaskInfo.State.error, fault=fault)
    with pytest.raises(TaskFailedError) as exc:
        esxi.wait_for_task(task, "VMDK copy")
    assert exc.value.fault is fault


def test_failure_emits_an_error_event():
    events = []
    task = FakeTask(running_polls=0, final_state=vim.TaskInfo.State.error, error_msg="boom")
    with pytest.raises(TaskFailedError):
        esxi.wait_for_task(task, "VMDK copy", events.append)
    assert events[-1].phase == Phase.ERROR
    assert events[-1].error == "boom"


def test_timeout_cancels_and_raises():
    """An unbounded wait means a hung CopyVirtualDisk_Task holds a worker slot
    forever with no way to intervene."""
    task = NeverEndingTask()
    clock = iter([0.0] + [100.0] * 50)
    with patch("vmkit.esxi.time.monotonic", side_effect=lambda: next(clock)):
        with pytest.raises(TaskTimeoutError, match="timed out"):
            esxi.wait_for_task(task, "VMDK copy", timeout_s=30)
    assert task.cancel_calls == 1


def test_timeout_error_is_a_task_failed_error():
    assert issubclass(TaskTimeoutError, TaskFailedError)


def test_should_cancel_cancels_and_raises():
    task = NeverEndingTask()
    with pytest.raises(TaskCancelledError):
        esxi.wait_for_task(task, "VMDK copy", should_cancel=lambda: True)
    assert task.cancel_calls == 1


def test_cancellation_is_not_a_task_failure():
    """The operator asked to stop; that is not the operation failing."""
    assert not issubclass(TaskCancelledError, TaskFailedError)


def test_should_cancel_false_does_not_interrupt():
    task = FakeTask(running_polls=2, result="ok")
    assert esxi.wait_for_task(task, "x", should_cancel=lambda: False) == "ok"
    assert task.cancel_calls == 0


def test_uncancelable_task_still_raises():
    """ESXi refuses CancelTask for some tasks; the caller must still be released."""
    task = NeverEndingTask(cancelable=False)
    with pytest.raises(TaskCancelledError):
        esxi.wait_for_task(task, "VMDK copy", should_cancel=lambda: True)


def test_no_timeout_by_default_does_not_consult_the_clock():
    task = FakeTask(running_polls=2)
    with patch("vmkit.esxi.time.monotonic", side_effect=AssertionError("clock read")):
        esxi.wait_for_task(task, "x")


# --------------------------------------------------------------------------- #
# Power operations                                                            #
# --------------------------------------------------------------------------- #
def test_power_on_vm():
    vm = make_vm("web01")
    content = FakeContent(vms=[vm])
    esxi.power_on_vm(content, "web01")
    vm.PowerOnVM_Task.assert_called_once()


def test_power_on_missing_vm_is_a_warning_not_an_error():
    """Historical behaviour: a caller powering on a VM that vanished should not
    have the whole workflow fail."""
    content = FakeContent(vms=[])
    esxi.power_on_vm(content, "gone")  # must not raise


def test_power_off_vm():
    vm = make_vm("web01", powered_on=True)
    content = FakeContent(vms=[vm])
    esxi.power_off_vm(content, "web01")
    vm.PowerOffVM_Task.assert_called_once()


def test_register_vm_uses_the_expected_datastore_path():
    dc = make_datacenter()
    content = FakeContent(datacenter=dc, hosts=[make_host()])
    esxi.register_vm(content, dc, "datastore1", "web01")
    kwargs = dc.vmFolder.RegisterVM_Task.call_args.kwargs
    assert kwargs["path"] == "[datastore1] web01/web01.vmx"
    assert kwargs["name"] == "web01"
    assert kwargs["asTemplate"] is False


def test_register_vm_failure_propagates_as_task_failed_error():
    dc = make_datacenter()
    content = FakeContent(datacenter=dc)
    # Assigned after FakeContent construction, which installs its own
    # inventory-updating RegisterVM_Task.
    dc.vmFolder.RegisterVM_Task = MagicMock(
        return_value=FakeTask(
            running_polls=0,
            final_state=vim.TaskInfo.State.error,
            error_msg="already exists",
        )
    )
    with pytest.raises(TaskFailedError, match="already exists"):
        esxi.register_vm(content, dc, "datastore1", "web01")
