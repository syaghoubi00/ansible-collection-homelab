#!/usr/bin/env python3
"""
QEMU VM management module for Ansible.
Provides lightweight VM creation for testing Ansible playbooks and roles.
"""

# Copyright: (c) 2025, Sebastian Yaghoubi <sebastianyaghoubi@gmail.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, TypedDict

import yaml
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_native

DOCUMENTATION = r"""
---
module: qemu
short_description: Manages QEMU virtual machines for testing
version_added: "1.0.0"
description:
    - Creates and manages QEMU virtual machines for ansible testing
    - Optionally uses cloud-init for VM initialization
    - Supports snapshot mode for quick disposable VMs
    - Supports dynamic port allocation for parallel testing
options:
    name:
        description: Name of the virtual machine
        required: true
        type: str
    state:
        description: Desired state of the VM
        choices: ['present', 'absent', 'started', 'stopped']
        default: present
        type: str
    memory_mb:
        description: Memory size in MB
        type: int
        default: 2048
    vcpus:
        description: Number of virtual CPUs
        type: int
        default: 2
    image:
        description: Path to base QCOW2 image
        type: path
        required: true
    cloud_init:
        description: Cloud-init configuration
        type: dict
        required: false
    ssh_port:
        description: Host port to forward to guest SSH. Uses dynamic port if not specified.
        type: int
        required: false
    network_mode:
        description: Network configuration mode
        type: str
        choices: ['user', 'bridge']
        default: user
    bridge_interface:
        description: Bridge interface name when network_mode=bridge
        type: str
        required: false
    wait_for_ip:
        description: Wait for IP address to be available after starting VM
        type: bool
        default: true
    ip_timeout:
        description: Maximum time in seconds to wait for IP address
        type: int
        default: 300
notes:
    - Requires QEMU with KVM support
    - Requires cloud-utils package if using cloud-init
    - When using bridge mode, VM IP detection requires root privileges
attributes:
    check_mode:
        support: full
    diff_mode:
        support: none
    platform:
        platforms: posix
seealso:
    - name: QEMU Documentation
      link: https://www.qemu.org/docs/master/
    - name: Cloud-init Documentation
      link: https://cloudinit.readthedocs.io/
author:
    - Sebastian Yaghoubi (@syaghoubi00)
"""

EXAMPLES = r"""
# Create a test VM using image defaults (no cloud-init)
- name: Create simple test VM
  qemu:
    name: test-vm
    state: started
    image: /templates/ubuntu-22.04.qcow2
  register: vm

# Create a test VM with bridged networking
- name: Create bridged network VM
  qemu:
    name: test-vm-bridge
    state: started
    image: /templates/ubuntu-22.04.qcow2
    network_mode: bridge
    bridge_interface: br0
    wait_for_ip: true
  register: vm

# Create a test VM with cloud-init configuration
- name: Create customized test VM
  qemu:
    name: test-vm-custom
    state: started
    image: /templates/ubuntu-22.04.qcow2
    cloud_init:
      users:
        - name: ansible
          sudo: ALL=(ALL) NOPASSWD:ALL
          ssh_authorized_keys:
            - "{{ lookup('file', '~/.ssh/id_rsa.pub') }}"
      package_update: true
      packages:
        - python3
  register: vm

# Create multiple VMs for parallel testing
- name: Create test VMs for multiple distros
  qemu:
    name: "test-{{ item }}"
    image: "/templates/{{ item }}.qcow2"
    state: started
  loop:
    - ubuntu2204
    - rocky9
    - debian12
  register: vms

# Add VMs to inventory with dynamic ports or IPs
- name: Add to inventory
  add_host:
    name: "{{ item.name }}"
    ansible_host: "{{ item.ip_address | default('localhost') }}"
    ansible_port: "{{ item.ssh_port | default(22) }}"
    ansible_user: "{{ item.cloud_init.users[0].name | default('ubuntu') }}"
    groups: test_vms
  loop: "{{ vms.results }}"
"""

RETURN = r"""
name:
    description: VM name
    type: str
    returned: always
    sample: test-vm
state:
    description: Current state of the VM
    type: str
    returned: always
    sample: running
ssh_port:
    description: Port forwarded to VM's SSH port (user networking mode only)
    type: int
    returned: when network_mode=user
    sample: 2222
ip_address:
    description: IP address of the VM (bridge networking mode)
    type: str
    returned: when network_mode=bridge and IP is detected
    sample: 192.168.1.100
cloud_init:
    description: Cloud-init configuration used (if any)
    type: dict
    returned: when cloud_init is configured
    sample: {"users": [{"name": "ansible"}]}
cmd:
    description: QEMU command used to start the VM
    type: list
    returned: when state=started
    sample: ["qemu-system-x86_64", "-name", "test-vm", ...]
pid:
    description: Process ID of the running VM
    type: int
    returned: when state=running
    sample: 12345
"""


class QemuVmError(Exception):
    """Custom exception for QEMU VM operations."""


@dataclass
class QemuVmResult:
    """Data class for VM operation results."""

    changed: bool = False
    state: str = "absent"
    name: str = ""
    ssh_port: Optional[int] = None
    ip_address: Optional[str] = None
    cloud_init: Optional[Dict[str, Any]] = None
    cmd: Optional[List[str]] = None
    pid: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary, excluding None values."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


class QemuVmParams(TypedDict):
    name: str
    state: str
    memory_mb: int
    vcpus: int
    image: str
    cloud_init: Optional[Dict[str, Any]]
    ssh_port: Optional[int]
    network_mode: str
    bridge_interface: Optional[str]
    wait_for_ip: bool
    ip_timeout: int


class QemuVM:
    """Manages QEMU virtual machine operations."""

    def __init__(self, module: AnsibleModule) -> None:
        """Initialize QEMU VM manager."""
        self.module = module
        self.params: QemuVmParams = module.params  # type: ignore
        self.result = QemuVmResult(
            name=self.params["name"], cloud_init=self.params.get("cloud_init")
        )
        self.temp_files: List[Union[str, Path]] = []

    def __del__(self) -> None:
        """Cleanup temporary files on object destruction."""
        for temp_file in self.temp_files:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except OSError as e:
                self.module.warn(f"Failed to cleanup {temp_file}: {e}")

    def _run_command(
        self, command: List[str], check: bool = True
    ) -> subprocess.CompletedProcess:
        """Execute a command and handle errors."""
        try:
            return subprocess.run(command, check=check, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise QemuVmError(f"Command failed: {to_native(e.stderr)}")
        except Exception as e:
            raise QemuVmError(f"Error executing command: {to_native(e)}")

    def _find_free_port(self) -> int:
        """Find a free port for SSH forwarding."""
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _get_vm_mac(self) -> Optional[str]:
        """Extract MAC address from QEMU process command line."""
        try:
            result = self._run_command(["ps", "-ef"], check=False)
            for line in result.stdout.splitlines():
                if f"-name {self.params['name']}" in line:
                    # Look for MAC in netdev/device arguments
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if "virtio-net" in part and i + 1 < len(parts):
                            mac_part = parts[i + 1]
                            if "mac=" in mac_part:
                                return mac_part.split("=")[1].strip(",")
            return None
        except (QemuVmError, IndexError):
            return None

    def _get_vm_ip_from_arp(self, mac: Optional[str] = None) -> Optional[str]:
        """Get VM IP address from ARP table."""
        if not mac:
            mac = self._get_vm_mac()
        if not mac:
            return None

        try:
            result = self._run_command(["arp", "-n"], check=False)
            for line in result.stdout.splitlines():
                if mac.lower() in line.lower():
                    ip = line.split()[0]
                    try:
                        IPv4Address(ip)
                        return ip
                    except ValueError:
                        continue
        except QemuVmError:
            pass
        return None

    def _get_vm_ip_from_guest_agent(self) -> Optional[str]:
        """Get VM IP address using QEMU Guest Agent."""
        try:
            ga_socket = f"/var/run/qemu-ga/{self.params['name']}.sock"
            if Path(ga_socket).exists():
                result = self._run_command(
                    ["qemu-ga", "--cmd", "guest-network-get-interfaces", ga_socket],
                    check=False,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    for iface in data.get("return", []):
                        for ip in iface.get("ip-addresses", []):
                            if ip["ip-address-type"] == "ipv4":
                                try:
                                    IPv4Address(ip["ip-address"])
                                    return ip["ip-address"]
                                except ValueError:
                                    continue
        except (QemuVmError, json.JSONDecodeError, FileNotFoundError):
            pass
        return None

    def _get_vm_ip_from_dhcp_leases(self) -> Optional[str]:
        """Get VM IP address from QEMU DHCP lease file."""
        lease_file = Path("/var/lib/qemu/dhcp.leases")
        if lease_file.exists():
            try:
                content = lease_file.read_text()
                for line in content.splitlines():
                    if self.params["name"] in line:
                        parts = line.split()
                        for part in parts:
                            try:
                                IPv4Address(part)
                                return part
                            except ValueError:
                                continue
            except (IOError, ValueError):
                pass
        return None

    def _get_vm_ip(self) -> Optional[str]:
        """Get VM IP address using all available methods."""
        # Try guest agent first
        ip = self._get_vm_ip_from_guest_agent()
        if ip:
            return ip

        # Try ARP table lookup
        ip = self._get_vm_ip_from_arp()
        if ip:
            return ip

        # Try DHCP leases as last resort
        return self._get_vm_ip_from_dhcp_leases()

    def _wait_for_ip(self, timeout: int = 300) -> Optional[str]:
        """Wait for IP address to become available."""
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            ip = self._get_vm_ip()
            if ip:
                return ip
            time.sleep(5)
        return None

    def _create_cloud_init_iso(self) -> Optional[Path]:
        """Create cloud-init config drive using cloud-localds if config provided."""
        if not self.params.get("cloud_init"):
            return None

        temp_dir = Path(tempfile.mkdtemp())
        self.temp_files.append(temp_dir)

        user_data_file = temp_dir / "user-data"
        user_data_file.write_text(
            f"#cloud-config\n{yaml.safe_dump(self.params['cloud_init'])}"
        )

        iso_path = (
            Path(self.params["image"]).parent / f"{self.params['name']}-cloud-init.iso"
        )
        self.temp_files.append(iso_path)

        self._run_command(["cloud-localds", str(iso_path), str(user_data_file)])
        return iso_path

    def _build_vm_command(self, cloud_init_iso: Optional[Path] = None) -> List[str]:
        """Build QEMU command with all parameters."""
        cmd = [
            "qemu-system-x86_64",
            "-name",
            self.params["name"],
            "-m",
            str(self.params["memory_mb"]),
            "-smp",
            str(self.params["vcpus"]),
            "-drive",
            f"file={self.params['image']},format=qcow2,snapshot=on",
        ]

        if cloud_init_iso:
            cmd.extend(["-drive", f"file={cloud_init_iso},format=raw,media=cdrom"])

        if self.params["network_mode"] == "user":
            ssh_port = self.params.get("ssh_port") or self._find_free_port()
            self.result.ssh_port = ssh_port
            cmd.extend(
                [
                    "-netdev",
                    f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
                    "-device",
                    "virtio-net,netdev=net0",
                ]
            )
        else:
            if not self.params.get("bridge_interface"):
                raise QemuVmError(
                    "bridge_interface is required when network_mode=bridge"
                )
            cmd.extend(
                [
                    "-netdev",
                    f"bridge,id=net0,br={self.params['bridge_interface']}",
                    "-device",
                    "virtio-net,netdev=net0",
                ]
            )

        cmd.extend(["-enable-kvm", "-display", "none", "-daemonize", "-cpu", "host"])

        return cmd

    def _get_vm_pid(self) -> Optional[int]:
        """Check if VM is running and get its PID."""
        try:
            result = self._run_command(["pgrep", "-f", f"qemu.*{self.params['name']}"])
            return int(result.stdout.strip())
        except (QemuVmError, ValueError):
            return None

    def ensure_state(self) -> Dict[str, Any]:
        """Ensure VM is in desired state."""
        current_pid = self._get_vm_pid()

        try:
            if self.params["state"] == "absent":
                if current_pid:
                    self._run_command(["kill", str(current_pid)])
                    self.result.changed = True

            elif self.params["state"] in ["present", "started"]:
                if not current_pid:
                    cloud_init_iso = self._create_cloud_init_iso()
                    cmd = self._build_vm_command(cloud_init_iso)
                    self._run_command(cmd)
                    self.result.cmd = cmd
                    self.result.changed = True

                    # Wait for IP if requested and using bridge networking
                    if (
                        self.params["network_mode"] == "bridge"
                        and self.params["wait_for_ip"]
                    ):
                        self.result.ip_address = self._wait_for_ip(
                            self.params["ip_timeout"]
                        )

            elif self.params["state"] == "stopped":
                if current_pid:
                    self._run_command(["kill", str(current_pid)])
                    self.result.changed = True

            # Update final state and info
            final_pid = self._get_vm_pid()
            self.result.state = "running" if final_pid else "stopped"
            if final_pid:
                self.result.pid = final_pid
                # Always try to get IP for bridge mode when running
                if (
                    self.params["network_mode"] == "bridge"
                    and not self.result.ip_address
                ):
                    self.result.ip_address = self._get_vm_ip()

        except QemuVmError as e:
            self.module.fail_json(msg=str(e))

        return self.result.to_dict()


def main() -> None:
    """Module entry point."""
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", required=True),
            state=dict(
                type="str",
                default="present",
                choices=["present", "absent", "started", "stopped"],
            ),
            memory_mb=dict(type="int", default=2048),
            vcpus=dict(type="int", default=2),
            image=dict(type="path", required=True),
            cloud_init=dict(type="dict", required=False),
            ssh_port=dict(type="int", required=False),
            network_mode=dict(type="str", choices=["user", "bridge"], default="user"),
            bridge_interface=dict(type="str", required=False),
            wait_for_ip=dict(type="bool", default=True),
            ip_timeout=dict(type="int", default=300),
        ),
        required_if=[("network_mode", "bridge", ["bridge_interface"])],
        supports_check_mode=True,
    )

    if module.check_mode:
        module.exit_json(changed=False)

    vm = QemuVM(module)
    result = vm.ensure_state()
    module.exit_json(**result)


if __name__ == "__main__":
    main()
