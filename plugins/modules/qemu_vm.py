#!/usr/bin/env python3
"""
QEMU VM management module for Ansible.
Provides lightweight VM creation for testing Ansible playbooks and roles.
"""

# Copyright: (c) 2025, Sebastian Yaghoubi <sebastianyaghoubi@gmail.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, TypedDict

import yaml
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_native

DOCUMENTATION = r"""
---
module: qemu_vm
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
notes:
    - Requires QEMU with KVM support
    - Requires cloud-utils package if using cloud-init
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
  qemu_vm:
    name: test-vm
    state: started
    image: /templates/ubuntu-22.04.qcow2
  register: vm

# Create a test VM with cloud-init configuration
- name: Create customized test VM
  qemu_vm:
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
  qemu_vm:
    name: "test-{{ item }}"
    image: "/templates/{{ item }}.qcow2"
    state: started
  loop:
    - ubuntu2204
    - rocky9
    - debian12
  register: vms

# Add VMs to inventory with dynamic ports
- name: Add to inventory
  add_host:
    name: "{{ item.name }}"
    ansible_host: localhost
    ansible_port: "{{ item.ssh_port }}"
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
    description: Port forwarded to VM's SSH port
    type: int
    returned: when network_mode=user
    sample: 2222
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

    def _run_command(self, command: List[str]) -> subprocess.CompletedProcess:
        """Execute a command and handle errors."""
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise QemuVmError(f"Command failed: {to_native(e.stderr)}")
        except Exception as e:
            raise QemuVmError(f"Error executing command: {to_native(e)}")

    def _find_free_port(self) -> int:
        """Find a free port for SSH forwarding."""
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

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
            cmd.extend(["-drive", f"file={cloud_init_iso},format=raw,readonly=on"])

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

        cmd.extend(["-enable-kvm", "-nographic", "-daemonize"])

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

            elif self.params["state"] == "stopped":
                if current_pid:
                    self._run_command(["kill", str(current_pid)])
                    self.result.changed = True

            # Update final state
            final_pid = self._get_vm_pid()
            self.result.state = "running" if final_pid else "stopped"
            if final_pid:
                self.result.pid = final_pid

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
