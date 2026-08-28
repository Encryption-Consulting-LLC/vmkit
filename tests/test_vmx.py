"""Tests for VMX rendering, parsing and MAC generation — all pure functions."""

import re
from unittest.mock import patch

import pytest

from vmkit.errors import VmkitError
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


def _settings(vmx: str) -> dict[str, str]:
    """Parse a rendered VMX into a key -> value mapping."""
    out = {}
    for line in vmx.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def _render(**overrides) -> str:
    kwargs = dict(
        hostname="web01", mac_address="00:50:56:aa:bb:cc", num_cpus=2, mem_mb=4096
    )
    kwargs.update(overrides)
    return render_vmx(**kwargs)


# --------------------------------------------------------------------------- #
# render_vmx                                                                  #
# --------------------------------------------------------------------------- #
def test_core_settings():
    s = _settings(_render())
    assert s["displayName"] == "web01"
    assert s["numvcpus"] == "2"
    assert s["memSize"] == "4096"
    assert s["ethernet0.address"] == "00:50:56:aa:bb:cc"
    assert s["guestOS"] == DEFAULT_GUEST_OS


def test_hostname_drives_every_per_vm_filename():
    """A stale filename here points a new VM at another VM's disk or nvram."""
    s = _settings(_render(hostname="web01"))
    assert s["nvram"] == "web01.nvram"
    assert s["scsi0:0.fileName"] == "web01.vmdk"
    assert s["extendedConfigFile"] == "web01.vmxf"
    assert s["vmxstats.filename"] == "web01.scoreboard"
    assert s["migrate.hostlog"] == "./web01.hlog"


def test_cpu_count_reaches_the_numa_and_socket_cookies():
    s = _settings(_render(num_cpus=8))
    assert s["numvcpus"] == "8"
    assert s["numa.autosize.vcpu.maxPerVirtualNode"] == "8"
    assert s["cpuid.coresPerSocket.cookie"] == "8"


def test_network_defaults_to_vm_network():
    assert _settings(_render())["ethernet0.networkName"] == DEFAULT_NETWORK == "VM Network"


def test_network_is_overridable():
    """This was hardcoded before 0.4.0, so any host without a port group of that
    name got a VM with a dead NIC."""
    assert _settings(_render(network="DMZ-VLAN20"))["ethernet0.networkName"] == "DMZ-VLAN20"


def test_guest_os_is_overridable():
    assert _settings(_render(guest_os="ubuntu64Guest"))["guestOS"] == "ubuntu64Guest"


def test_no_cdrom_block_without_an_iso():
    vmx = _render()
    assert "sata0:0.present" not in vmx
    assert "cdrom-image" not in vmx


def test_cdrom_block_with_an_iso():
    s = _settings(_render(iso_filename="web01-config.iso"))
    assert s["sata0:0.present"] == "TRUE"
    assert s["sata0:0.deviceType"] == "cdrom-image"
    assert s["sata0:0.fileName"] == "web01-config.iso"
    assert s["sata0:0.startConnected"] == "TRUE"


def test_rendered_vmx_has_no_unsubstituted_placeholders():
    """A missing .format() key would otherwise ship a literal '{network}' to ESXi."""
    vmx = _render(iso_filename="x.iso", network="DMZ", guest_os="ubuntu64Guest")
    assert not re.search(r"\{[a-z_]+\}", vmx)


def test_every_line_is_a_quoted_assignment():
    for line in _render(iso_filename="x.iso").splitlines():
        if not line.strip():
            continue
        assert re.match(r'^[\w.:\-]+ = ".*"$', line), line


def test_render_is_deterministic():
    assert _render() == _render()


# --------------------------------------------------------------------------- #
# parse_guest_os / parse_network                                              #
# --------------------------------------------------------------------------- #
def test_parse_guest_os_round_trips_render():
    assert parse_guest_os(_render(guest_os="rhel9_64Guest")) == "rhel9_64Guest"


def test_parse_network_round_trips_render():
    assert parse_network(_render(network="Lab-Net")) == "Lab-Net"


@pytest.mark.parametrize(
    "text,expected",
    [
        ('guestOS = "ubuntu64Guest"', "ubuntu64Guest"),
        ('  guestOS="ubuntu64Guest"', "ubuntu64Guest"),
        ('GUESTOS = "ubuntu64Guest"', "ubuntu64Guest"),
        ('other = "x"\nguestOS = "win"\nmore = "y"', "win"),
    ],
)
def test_parse_guest_os_tolerates_formatting(text, expected):
    assert parse_guest_os(text) == expected


@pytest.mark.parametrize("text", ["", "no settings here", 'guest_os = "x"', "guestOS = x"])
def test_parse_guest_os_returns_none_when_absent(text):
    assert parse_guest_os(text) is None


@pytest.mark.parametrize("text", ["", 'ethernet1.networkName = "Other"', "networkName = x"])
def test_parse_network_returns_none_when_absent(text):
    """Only ethernet0 is the primary NIC; a second adapter must not be mistaken
    for it."""
    assert parse_network(text) is None


# --------------------------------------------------------------------------- #
# parse_nic_pci_slot                                                          #
# --------------------------------------------------------------------------- #
def test_render_pins_the_nic_pci_slot():
    """The guest's interface name derives from this slot, so every clone must get
    the same one whatever its base looks like."""
    assert _settings(_render())["ethernet0.pciSlotNumber"] == NIC_PCI_SLOT


def test_parse_nic_pci_slot_round_trips_render():
    assert parse_nic_pci_slot(_render()) == NIC_PCI_SLOT


@pytest.mark.parametrize(
    "text,expected",
    [
        ('ethernet0.pciSlotNumber = "34"', "34"),
        ('  ethernet0.pciSlotNumber="192"', "192"),
        ('ETHERNET0.PCISLOTNUMBER = "160"', "160"),
        ('other = "x"\nethernet0.pciSlotNumber = "33"\nmore = "y"', "33"),
    ],
)
def test_parse_nic_pci_slot_tolerates_formatting(text, expected):
    assert parse_nic_pci_slot(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", 'ethernet1.pciSlotNumber = "45"', 'scsi0.pciSlotNumber = "32"',
     "ethernet0.pciSlotNumber = 33"],
)
def test_parse_nic_pci_slot_returns_none_when_absent(text):
    """Only ethernet0 -- another device's slot must not be mistaken for the NIC's."""
    assert parse_nic_pci_slot(text) is None


# --------------------------------------------------------------------------- #
# random_mac                                                                  #
# --------------------------------------------------------------------------- #
MAC_RE = re.compile(r"^00:50:56:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")


def test_mac_format_and_vmware_static_range():
    for _ in range(200):
        mac = random_mac()
        assert MAC_RE.match(mac), mac
        # VMware reserves 00:50:56:00:00:00 - 00:50:56:3F:FF:FF for static
        # assignment; outside it ESXi may reassign the address itself.
        assert int(mac.split(":")[3], 16) <= 0x3F


def test_mac_avoids_supplied_addresses():
    existing = {random_mac() for _ in range(50)}
    for _ in range(100):
        assert random_mac(existing) not in existing


def test_mac_exclusion_is_case_insensitive():
    """ESXi reports lower case, Windows upper; a caller may pass either."""
    with patch("vmkit.vmx.random.randint", side_effect=[0x01, 0x02, 0x03, 0x04, 0x05, 0x06]):
        mac = random_mac({"00:50:56:01:02:03".upper()})
    assert mac == "00:50:56:04:05:06"


def test_mac_raises_rather_than_returning_a_known_duplicate():
    """Returning a duplicate would produce an intermittent networking fault that
    looks like anything but a provisioning bug."""
    with patch("vmkit.vmx.random.randint", return_value=0x01):
        with pytest.raises(VmkitError, match="Could not find a free MAC"):
            random_mac({"00:50:56:01:01:01"}, attempts=5)


def test_mac_attempts_are_bounded():
    with patch("vmkit.vmx.random.randint", return_value=0x01) as randint:
        with pytest.raises(VmkitError):
            random_mac({"00:50:56:01:01:01"}, attempts=3)
    assert randint.call_count == 9  # 3 attempts x 3 octets


def test_mac_with_empty_exclusion_set_succeeds():
    assert MAC_RE.match(random_mac(set()))


def test_mac_none_exclusion_is_the_default_behaviour():
    assert MAC_RE.match(random_mac(None))
