"""A fake pyVmomi inventory, good enough to drive the workflows.

Real pyVmomi managed objects resolve every property access through a SOAP stub,
so they cannot be constructed offline. `MagicMock(spec=vim.X)` can: it satisfies
`isinstance(obj, vim.X)` (which `get_datacenter` and `get_vm_config` rely on) and
still lets us set attributes freely. Enum values, fault classes and
`vim.TaskInfo.State` come from the real library, so the tests exercise the same
constants production does.

What this deliberately does NOT fake: the network. Anything that would make an
HTTPS call (`upload_file`, `read_datastore_file`) is patched at its call site by
the tests that need it.
"""

from unittest.mock import MagicMock

from pyVmomi import vim, vmodl


class FakeTask:
    """A vim.Task whose state advances on each `info` read.

    `wait_for_task` polls `task.info.state` in a loop, so a task that is
    `running` for a fixed number of polls and then terminal exercises the real
    loop — including the progress events it emits — without any sleeping beyond
    the caller's poll interval.
    """

    def __init__(
        self,
        *,
        running_polls: int = 1,
        final_state=vim.TaskInfo.State.success,
        result: object = None,
        error_msg: str | None = None,
        fault: object = None,
        cancelable: bool = True,
        progress_sequence: list[int] | None = None,
    ):
        self._running_polls = running_polls
        self._final_state = final_state
        self._result = result
        self._error_msg = error_msg
        self._fault = fault
        self._cancelable = cancelable
        self._progress_sequence = progress_sequence or [0, 25, 50, 75]
        self.polls = 0
        self.cancel_calls = 0

    @property
    def info(self):
        index = self.polls
        self.polls += 1
        info = MagicMock()
        if index < self._running_polls:
            info.state = vim.TaskInfo.State.running
            seq = self._progress_sequence
            info.progress = seq[min(index, len(seq) - 1)]
            info.error = None
            info.result = None
        else:
            info.state = self._final_state
            info.progress = 100
            info.result = self._result
            if self._final_state == vim.TaskInfo.State.success:
                info.error = None
            else:
                error = self._fault if self._fault is not None else MagicMock()
                if self._fault is None:
                    error.msg = self._error_msg or "unknown error"
                info.error = error
        return info

    def CancelTask(self):
        self.cancel_calls += 1
        if not self._cancelable:
            raise vim.fault.NotSupported()


class NeverEndingTask(FakeTask):
    """Stays `running` forever — for timeout and cancellation tests."""

    def __init__(self, **kwargs):
        super().__init__(running_polls=10**9, **kwargs)


def make_vm(
    name: str,
    *,
    powered_on: bool = False,
    cpus: int = 2,
    mem_mb: int = 4096,
    mac: str = "00:50:56:aa:bb:cc",
    guest_os: str = "windows2022srvNext-64",
    is_template: bool = False,
    destroyed: bool = False,
):
    """A vim.VirtualMachine stand-in.

    `destroyed=True` makes every property read raise ManagedObjectNotFound, which
    is what a VM deleted between the container-view snapshot and the property read
    actually does.
    """
    vm = MagicMock(spec=vim.VirtualMachine)
    power = (
        vim.VirtualMachinePowerState.poweredOn
        if powered_on
        else vim.VirtualMachinePowerState.poweredOff
    )

    if destroyed:
        type(vm).name = property(
            lambda _self: (_ for _ in ()).throw(vmodl.fault.ManagedObjectNotFound())
        )
        type(vm).summary = property(
            lambda _self: (_ for _ in ()).throw(vmodl.fault.ManagedObjectNotFound())
        )
        return vm

    vm.name = name

    nic = MagicMock(spec=vim.VirtualEthernetCard)
    nic.macAddress = mac
    # A device the config also carries, to prove get_vm_config picks the NIC and
    # not merely the first device.
    disk = MagicMock(spec=vim.VirtualDisk)

    vm.config.hardware.device = [disk, nic]
    vm.config.hardware.numCPU = cpus
    vm.config.hardware.memoryMB = mem_mb
    vm.runtime.powerState = power

    vm.summary.runtime.powerState = power
    vm.summary.config.name = name
    vm.summary.config.guestId = guest_os
    vm.summary.config.numCpu = cpus
    vm.summary.config.memorySizeMB = mem_mb
    vm.summary.config.template = is_template

    vm.Destroy_Task = MagicMock(return_value=FakeTask())
    vm.PowerOnVM_Task = MagicMock(return_value=FakeTask())
    vm.PowerOffVM_Task = MagicMock(return_value=FakeTask())
    return vm


def make_datastore(
    name: str,
    *,
    capacity: int = 1000 * 1024**3,
    free: int = 800 * 1024**3,
    ds_type: str = "VMFS",
    accessible: bool = True,
    vmdk_sizes: dict[str, int] | None = None,
    files: dict[str, list[str]] | None = None,
    url: str | None = None,
):
    """A vim.Datastore stand-in whose browser answers VMDK size and file searches.

    `vmdk_sizes` maps a base VM name to the total size its `*.vmdk` files report.
    `files` maps a folder ("" for the root) to the file names in it; a search of
    a folder not in the map fails with FileNotFound, as on a real host.
    """
    ds = MagicMock(spec=vim.Datastore)
    ds.name = name
    ds.summary.name = name
    ds.summary.capacity = capacity
    ds.summary.freeSpace = free
    ds.summary.type = ds_type
    ds.summary.accessible = accessible
    ds.summary.url = url if url is not None else f"ds:///vmfs/volumes/uuid-{name}/"

    sizes = vmdk_sizes or {}
    folders = files or {}

    def search(datastorePath, searchSpec):
        # datastorePath looks like "[datastore1] base-vm", or "[datastore1]" for the root
        folder = datastorePath.partition("]")[2].strip()
        pattern = searchSpec.matchPattern[0]
        found = []
        if pattern.endswith("*.vmdk"):
            if folder in sizes:
                entry = MagicMock()
                entry.fileSize = sizes[folder]
                found.append(entry)
            return FakeTask(running_polls=0, result=MagicMock(file=found))
        if folder not in folders:
            return FakeTask(
                running_polls=0,
                final_state=vim.TaskInfo.State.error,
                fault=vim.fault.FileNotFound(),
            )
        for fname in folders[folder]:
            if fname == pattern:
                entry = MagicMock()
                entry.path = fname
                found.append(entry)
        return FakeTask(running_polls=0, result=MagicMock(file=found))

    ds.browser.SearchDatastore_Task = MagicMock(side_effect=search)
    return ds


def make_network(name: str, *, accessible: bool = True):
    net = MagicMock(spec=vim.Network)
    net.name = name
    net.summary.accessible = accessible
    return net


def make_host(
    name: str = "esxi-01",
    *,
    cores: int = 16,
    threads: int = 32,
    mhz_per_core: int = 2400,
    cpu_used_mhz: int = 4000,
    memory_mb: int = 131072,
    memory_used_mb: int = 32768,
):
    host = MagicMock(spec=vim.HostSystem)
    host.name = name
    host.summary.hardware.numCpuCores = cores
    host.summary.hardware.numCpuThreads = threads
    host.summary.hardware.cpuMhz = mhz_per_core
    host.summary.hardware.memorySize = memory_mb * 1024 * 1024
    host.summary.quickStats.overallCpuUsage = cpu_used_mhz
    host.summary.quickStats.overallMemoryUsage = memory_used_mb
    host.parent.resourcePool = MagicMock(spec=vim.ResourcePool)
    return host


def make_datacenter(name: str = "ha-datacenter"):
    dc = MagicMock(spec=vim.Datacenter)
    dc.name = name
    dc.vmFolder.RegisterVM_Task = MagicMock(return_value=FakeTask())
    return dc


class FakeContent:
    """A vim.ServiceInstanceContent stand-in backed by in-memory lists.

    `CreateContainerView` returns whichever list matches the requested type, so
    the real view-create/destroy dance in the library runs unmodified.
    """

    def __init__(
        self,
        *,
        vms=(),
        datastores=(),
        networks=(),
        hosts=None,
        datacenter=None,
    ):
        self.vms = list(vms)
        self.datastores = list(datastores)
        self.networks = list(networks)
        self.hosts = list(hosts) if hosts is not None else [make_host()]
        self.datacenter = datacenter if datacenter is not None else make_datacenter()
        self.destroyed_views = 0
        self.made_directories: list[tuple[str, str]] = []
        self.deleted_paths: list[str] = []
        self.copied: list[tuple[str, str]] = []
        self.created_disks: list[tuple[str, object]] = []

        self.rootFolder = MagicMock()
        self.rootFolder.childEntity = [self.datacenter]
        self.about.fullName = "VMware ESXi 8.0.0"

        # Registration must actually put the VM in inventory: clone_workflow
        # re-reads the inventory afterwards to confirm the VM landed, and a fake
        # that skipped this would make every clone test fail that check.
        self.registered: list[str] = []
        self.datacenter.vmFolder.RegisterVM_Task = MagicMock(side_effect=self._register)

        self.viewManager = MagicMock()
        self.viewManager.CreateContainerView = MagicMock(side_effect=self._make_view)

        self.fileManager = MagicMock()
        self.fileManager.MakeDirectory = MagicMock(side_effect=self._make_directory)
        self.fileManager.CopyDatastoreFile_Task = MagicMock(side_effect=self._copy_file)
        self.fileManager.DeleteDatastoreFile_Task = MagicMock(side_effect=self._delete)

        self.virtualDiskManager = MagicMock()
        self.virtualDiskManager.CopyVirtualDisk_Task = MagicMock(side_effect=self._copy_disk)
        self.virtualDiskManager.CreateVirtualDisk_Task = MagicMock(side_effect=self._create_disk)

    # MagicMock-like attribute autocreation for the few odds and ends
    # (content.about.fullName) the library reads.
    def __getattr__(self, item):
        value = MagicMock()
        object.__setattr__(self, item, value)
        return value

    def _make_view(self, _folder, types, _recursive):
        kind = types[0]
        mapping = {
            vim.VirtualMachine: self.vms,
            vim.Datastore: self.datastores,
            vim.Network: self.networks,
            vim.HostSystem: self.hosts,
        }
        view = MagicMock()
        view.view = mapping.get(kind, [])
        view.Destroy = MagicMock(side_effect=self._count_destroy)
        return view

    def _count_destroy(self):
        self.destroyed_views += 1

    def _make_directory(self, name, datacenter, createParentDirectories):
        self.made_directories.append((name, str(createParentDirectories)))

    def _copy_disk(self, sourceName, sourceDatacenter, destName, destDatacenter, force):
        self.copied.append((sourceName, destName))
        return FakeTask()

    def _create_disk(self, name, datacenter, spec):
        self.created_disks.append((name, spec))
        return FakeTask()

    def _copy_file(
        self, sourceName, sourceDatacenter, destinationName, destinationDatacenter, force
    ):
        self.copied.append((sourceName, destinationName))
        return FakeTask()

    def _delete(self, name, datacenter):
        self.deleted_paths.append(name)
        return FakeTask()

    def _register(self, path, name, asTemplate, pool, host):
        self.registered.append(name)
        self.vms.append(make_vm(name))
        return FakeTask()

    def vm(self, name):
        """Return the fake VM object with this name (helper for assertions)."""
        return next(vm for vm in self.vms if vm.name == name)


class FakeConnection:
    """A vmkit.Connection stand-in that needs no session."""

    def __init__(self, content: FakeContent, **overrides):
        self._content = content
        self.host = overrides.get("host", "esxi.test")
        self.user = overrides.get("user", "root")
        self.password = overrides.get("password", "hunter2")
        self.port = overrides.get("port", 443)
        self.verify_tls = overrides.get("verify_tls", False)
        self.closed = False

    @property
    def content(self):
        return self._content

    def close(self):
        self.closed = True
