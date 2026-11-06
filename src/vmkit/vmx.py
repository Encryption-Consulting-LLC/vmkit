import random
import re
from collections.abc import Sequence

from vmkit.errors import VmkitError

# Guest OS baked into a freshly rendered VMX when none can be read from an
# existing VMX (e.g. a base VM whose .vmx is missing or unreadable). This keeps
# the historical Windows Server default for backward compatibility.
DEFAULT_GUEST_OS = "windows2022srvNext-64"

# Port group a NIC is attached to when the caller does not say. This was
# hardcoded in the template before 0.4.0, so a VM deployed to any host without a
# port group of this name registered with a dead NIC — silent, and only noticed
# when first boot never phoned home.
DEFAULT_NETWORK = "VM Network"

# PCI slot ethernet0 is pinned to in VMX_TEMPLATE. On VMware the guest's
# predictable interface name derives from this slot (33 -> ens33), so pinning it
# gives every clone the same NIC name regardless of how its base image was built.
# A base at a different slot is named differently from its own clones.
NIC_PCI_SLOT = "33"

_GUEST_OS_RE = re.compile(r'^\s*guestOS\s*=\s*"(.+?)"', re.IGNORECASE | re.MULTILINE)
_NIC_PCI_SLOT_RE = re.compile(
    r'^\s*ethernet0\.pciSlotNumber\s*=\s*"(.+?)"', re.IGNORECASE | re.MULTILINE
)
_NETWORK_RE = re.compile(
    r'^\s*ethernet0\.networkName\s*=\s*"(.+?)"', re.IGNORECASE | re.MULTILINE
)

VMX_TEMPLATE = """\
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "21"
nvram = "{hostname}.nvram"
svga.present = "TRUE"
vmci0.present = "TRUE"
hpet0.present = "TRUE"
floppy0.present = "FALSE"
RemoteDisplay.maxConnections = "-1"
numvcpus = "{num_cpus}"
memSize = "{mem_mb}"
bios.bootRetry.delay = "10"
firmware = "efi"
powerType.powerOff = "default"
powerType.suspend = "soft"
powerType.reset = "default"
tools.upgrade.policy = "manual"
sched.cpu.units = "mhz"
sched.cpu.affinity = "all"
sched.cpu.latencySensitivity = "normal"
scsi0.virtualDev = "pvscsi"
scsi0.present = "TRUE"
sata0.present = "TRUE"
usb_xhci.present = "TRUE"
svga.autodetect = "TRUE"
scsi0:0.deviceType = "scsi-hardDisk"
scsi0:0.fileName = "{hostname}.vmdk"
sched.scsi0:0.shares = "normal"
sched.scsi0:0.throughputCap = "off"
scsi0:0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "{network}"
ethernet0.addressType = "static"
ethernet0.address = "{mac_address}"
ethernet0.wakeOnPcktRcv = "FALSE"
ethernet0.uptCompatibility = "TRUE"
ethernet0.present = "TRUE"
displayName = "{hostname}"
guestOS = "{guest_os}"
chipset.motherboardLayout = "acpi"
uefi.secureBoot.enabled = "TRUE"
disk.EnableUUID = "TRUE"
toolScripts.afterPowerOn = "TRUE"
toolScripts.afterResume = "TRUE"
toolScripts.beforeSuspend = "TRUE"
toolScripts.beforePowerOff = "TRUE"
tools.syncTime = "FALSE"
sched.cpu.min = "0"
sched.cpu.shares = "normal"
sched.mem.min = "0"
sched.mem.minSize = "0"
sched.mem.shares = "normal"
vmxstats.filename = "{hostname}.scoreboard"
numa.autosize.cookie = "20012"
numa.autosize.vcpu.maxPerVirtualNode = "{num_cpus}"
cpuid.coresPerSocket.cookie = "{num_cpus}"
pciBridge1.present = "TRUE"
pciBridge1.virtualDev = "pciRootBridge"
pciBridge1.functions = "1"
pciBridge1:0.pxm = "0"
pciBridge0.present = "TRUE"
pciBridge0.virtualDev = "pciRootBridge"
pciBridge0.functions = "1"
pciBridge0.pxm = "-1"
scsi0.pciSlotNumber = "32"
ethernet0.pciSlotNumber = "{nic_pci_slot}"
usb_xhci.pciSlotNumber = "34"
sata0.pciSlotNumber = "35"
migrate.hostlog = "./{hostname}.hlog"
scsi0:0.redo = ""
svga.vramSize = "16777216"
vmotion.checkpointFBSize = "4194304"
vmotion.checkpointSVGAPrimarySize = "16777216"
vmotion.svga.mobMaxSize = "16777216"
vmotion.svga.graphicsMemoryKB = "16384"
extendedConfigFile = "{hostname}.vmxf"
monitor.phys_bits_used = "45"
cleanShutdown = "TRUE"
softPowerOff = "TRUE"
{cdrom_block}svga.guestBackedPrimaryAware = "TRUE"
tools.capability.verifiedSamlToken = "TRUE"
tools.remindInstall = "FALSE"
toolsInstallManager.updateCounter = "1"
usb_xhci:4.present = "TRUE"
usb_xhci:4.deviceType = "hid"
usb_xhci:4.port = "4"
usb_xhci:4.parent = "-1"
"""

_CDROM_BLOCK = (
    'sata0:{unit}.present = "TRUE"\n'
    'sata0:{unit}.deviceType = "cdrom-image"\n'
    'sata0:{unit}.fileName = "{iso_filename}"\n'
    'sata0:{unit}.startConnected = "TRUE"\n'
)

#: CD-ROM drives :func:`render_vmx` will place, one per unit of the template's
#: single SATA controller (units 0-29).
MAX_CDROMS = 30


def random_mac(existing: set[str] | None = None, attempts: int = 100) -> str:
    """Generate a random MAC in the VMware static OUI range 00:50:56:00:00:00 – 00:50:56:3F:FF:FF.

    Only 22 bits are random, so collisions are not remote at batch scale: by the
    birthday bound a few hundred VMs on one network give a percent-level chance
    of a duplicate, and two VMs sharing a MAC is an intermittent networking fault
    that looks like anything but a provisioning bug.

    Pass ``existing`` (lower-case or not) to draw an address not already in use.
    Raises ``VmkitError`` if it cannot find a free one, rather than returning a
    known-duplicate address.
    """
    taken = {mac.lower() for mac in existing} if existing else set()
    for _ in range(attempts):
        b1 = random.randint(0x00, 0x3F)
        b2 = random.randint(0x00, 0xFF)
        b3 = random.randint(0x00, 0xFF)
        mac = f"00:50:56:{b1:02x}:{b2:02x}:{b3:02x}"
        if mac not in taken:
            return mac
    raise VmkitError(
        f"Could not find a free MAC address in the VMware static range after "
        f"{attempts} attempts ({len(taken)} already in use)."
    )


def parse_guest_os(vmx_text: str) -> str | None:
    """Return the guestOS identifier from VMX text, or None if absent.

    Reads the literal value of the `guestOS = "..."` line so it can be re-emitted
    verbatim into a freshly rendered VMX (preserving a base/existing VM's OS type
    instead of hardcoding one).
    """
    match = _GUEST_OS_RE.search(vmx_text)
    return match.group(1) if match else None


def parse_network(vmx_text: str) -> str | None:
    """Return the port group from ``ethernet0.networkName``, or None if absent.

    Lets an update preserve the NIC's existing attachment instead of silently
    resetting it to the default.
    """
    match = _NETWORK_RE.search(vmx_text)
    return match.group(1) if match else None


def parse_nic_pci_slot(vmx_text: str) -> str | None:
    """Return ``ethernet0.pciSlotNumber`` from VMX text, or None if absent.

    Lets a caller compare a base VM's slot against :data:`NIC_PCI_SLOT` and warn
    when a clone's NIC will be named differently from the image it came from.
    """
    match = _NIC_PCI_SLOT_RE.search(vmx_text)
    return match.group(1) if match else None


def render_vmx(
    hostname: str,
    mac_address: str,
    num_cpus: int,
    mem_mb: int,
    iso_filename: str | None = None,
    guest_os: str = DEFAULT_GUEST_OS,
    network: str = DEFAULT_NETWORK,
    cdroms: Sequence[str] = (),
) -> str:
    """Render a VMX for ``hostname``.

    ``iso_filename`` attaches one CD-ROM (``sata0:0``), named relative to the VM's
    own folder -- the clone/update config-ISO case. ``cdroms`` attaches several,
    in order from ``sata0:0``; each entry is a VMX file path, either relative to
    the VM folder or absolute (``/vmfs/volumes/<uuid>/ISO/x.iso``), so a VM can
    boot an ISO kept elsewhere on a datastore. Pass one or the other. EFI tries
    ``sata0:0`` first on a blank disk, so put the bootable image there.
    """
    if iso_filename is not None and cdroms:
        raise VmkitError("Pass iso_filename or cdroms, not both.")
    files = [iso_filename] if iso_filename is not None else list(cdroms)
    if len(files) > MAX_CDROMS:
        raise VmkitError(f"{len(files)} CD-ROMs requested; the limit is {MAX_CDROMS}.")
    for f in files:
        # The value lands inside a quoted VMX line: a quote or newline would end
        # it early and let the rest be read as further settings.
        if not f or any(c in f for c in '"\r\n'):
            raise VmkitError(f"Invalid CD-ROM file path: {f!r}")
    cdrom_block = "".join(
        _CDROM_BLOCK.format(unit=unit, iso_filename=f) for unit, f in enumerate(files)
    )
    return VMX_TEMPLATE.format(
        hostname=hostname,
        mac_address=mac_address,
        num_cpus=num_cpus,
        mem_mb=mem_mb,
        cdrom_block=cdrom_block,
        guest_os=guest_os,
        network=network,
        nic_pci_slot=NIC_PCI_SLOT,
    )
