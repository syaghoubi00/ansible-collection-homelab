#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2025, Your Name <your.email@example.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: qemu_vm
short_description: Manages QEMU virtual machines
version_added: "1.0.0"
description:
    - Creates, modifies, and manages QEMU virtual machines
    - Supports basic VM lifecycle operations and configuration
    - Manages VM disk images and network settings
author:
    - Your Name (@github_handle)
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
        default: 1024
    vcpus:
        description: Number of virtual CPUs
        type: int
        default: 1
    disk_gb:
        description: Size of primary disk in GB
        type: int
        default: 20
    image_path:
        description: Path to disk image file
        type: path
        required: true
    network:
        description: Network interface type
        choices: ['user', 'bridge']
        default: user
        type: str
    bridge_interface:
        description: Bridge interface name when network=bridge
        type: str
notes:
    - Requires QEMU to be installed on the host system
    - Requires appropriate permissions to create and manage VMs
requirements:
    - python >= 3.6
    - qemu-system-x86_64
    - qemu-img
"""

EXAMPLES = r"""
- name: Create a new VM
  qemu_vm:
    name: test-vm
    state: present
    memory_mb: 2048
    vcpus: 2
    disk_gb: 30
    image_path: /var/lib/qemu/test-vm.qcow2
    network: user

- name: Start an existing VM
  qemu_vm:
    name: test-vm
    state: started

- name: Remove a VM and its disk image
  qemu_vm:
    name: test-vm
    state: absent
"""

RETURN = r"""
state:
    description: Final state of the VM
    type: str
    returned: always
    sample: 'running'
vm_info:
    description: Detailed information about the VM
    type: dict
    returned: when state != absent
    contains:
        name:
            description: VM name
            type: str
            sample: test-vm
        memory_mb:
            description: Configured memory in MB
            type: int
            sample: 2048
        vcpus:
            description: Number of configured vCPUs
            type: int
            sample: 2
        disk_size_gb:
            description: Size of primary disk in GB
            type: int
            sample: 30
        image_path:
            description: Path to disk image
            type: str
            sample: /var/lib/qemu/test-vm.qcow2
        network:
            description: Network configuration
            type: dict
            sample: {"type": "user"}
pid:
    description: Process ID of the running VM (if started)
    type: int
    returned: when state=started
    sample: 12345
"""

import os
import subprocess

from ansible.module_utils._text import to_native
from ansible.module_utils.basic import AnsibleModule


class QemuVmError(Exception):
    """Custom exception for QEMU VM operations"""

    pass


class QemuVM:
    """Class to manage QEMU virtual machine operations"""

    def __init__(self, module):
        self.module = module
        self.params = module.params
        self.result = {
            "changed": False,
            "state": "absent",
            "vm_info": None,
            "pid": None,
        }

    def _run_command(self, command, check_rc=True):
        """Execute a command and handle errors"""
        try:
            result = subprocess.run(
                command,
                check=check_rc,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            return result
        except subprocess.CalledProcessError as e:
            raise QemuVmError(f"Command failed: {to_native(e.stderr)}")
        except Exception as e:
            raise QemuVmError(f"Error executing command: {to_native(e)}")

    def _get_vm_status(self):
        """Check VM status and get process information"""
        try:
            result = self._run_command(["pgrep", "-a", "qemu-system-x86_64"])
            for line in result.stdout.splitlines():
                if f"-name {self.params['name']}" in line:
                    pid = int(line.split()[0])
                    return "running", pid

            if os.path.exists(self.params["image_path"]):
                return "stopped", None

            return "absent", None
        except QemuVmError:
            return "absent", None

    def _create_disk_image(self):
        """Create a QCOW2 disk image"""
        if not os.path.exists(self.params["image_path"]):
            cmd = [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                self.params["image_path"],
                f"{self.params['disk_gb']}G",
            ]
            self._run_command(cmd)
            return True
        return False

    def _build_vm_command(self):
        """Build QEMU command with all parameters"""
        cmd = [
            "qemu-system-x86_64",
            "-name",
            self.params["name"],
            "-m",
            str(self.params["memory_mb"]),
            "-smp",
            str(self.params["vcpus"]),
            "-drive",
            f"file={self.params['image_path']},format=qcow2",
            "-daemonize",
            "-display",
            "none",
        ]

        if self.params["network"] == "user":
            cmd.extend(["-net", "nic", "-net", "user"])
        elif self.params["network"] == "bridge":
            if not self.params.get("bridge_interface"):
                raise QemuVmError("bridge_interface is required when network=bridge")
            cmd.extend(
                ["-net", "nic", "-net", f"bridge,br={self.params['bridge_interface']}"]
            )

        return cmd

    def _start_vm(self):
        """Start the VM"""
        cmd = self._build_vm_command()
        self._run_command(cmd)
        # Verify VM started successfully
        status, pid = self._get_vm_status()
        if status != "running":
            raise QemuVmError("Failed to start VM")
        return pid

    def _stop_vm(self, pid):
        """Stop the VM gracefully"""
        if pid:
            try:
                self._run_command(["kill", "-SIGTERM", str(pid)])
                # Wait for VM to stop
                import time

                for _ in range(30):  # 30 second timeout
                    status, _ = self._get_vm_status()
                    if status != "running":
                        return True
                    time.sleep(1)
                # Force kill if VM doesn't stop gracefully
                self._run_command(["kill", "-SIGKILL", str(pid)])
            except QemuVmError as e:
                self.module.warn(f"Error stopping VM: {str(e)}")
        return True

    def _get_vm_info(self):
        """Get current VM configuration"""
        if os.path.exists(self.params["image_path"]):
            return {
                "name": self.params["name"],
                "memory_mb": self.params["memory_mb"],
                "vcpus": self.params["vcpus"],
                "disk_size_gb": self.params["disk_gb"],
                "image_path": self.params["image_path"],
                "network": {
                    "type": self.params["network"],
                    "bridge": self.params.get("bridge_interface"),
                },
            }
        return None

    def ensure_state(self):
        """Ensure VM is in desired state"""
        current_status, current_pid = self._get_vm_status()

        try:
            if self.params["state"] == "absent":
                if current_status != "absent":
                    if current_status == "running":
                        self._stop_vm(current_pid)
                    if os.path.exists(self.params["image_path"]):
                        os.remove(self.params["image_path"])
                    self.result["changed"] = True

            elif self.params["state"] in ["present", "started"]:
                if current_status == "absent":
                    self.result["changed"] = self._create_disk_image()
                    if self.params["state"] == "started":
                        self.result["pid"] = self._start_vm()
                        self.result["changed"] = True
                elif current_status == "stopped" and self.params["state"] == "started":
                    self.result["pid"] = self._start_vm()
                    self.result["changed"] = True

            elif self.params["state"] == "stopped":
                if current_status == "running":
                    self._stop_vm(current_pid)
                    self.result["changed"] = True

            # Update final status and VM info
            final_status, final_pid = self._get_vm_status()
            self.result["state"] = final_status
            self.result["vm_info"] = self._get_vm_info()
            if final_pid:
                self.result["pid"] = final_pid

        except QemuVmError as e:
            self.module.fail_json(msg=str(e))
        except Exception as e:
            self.module.fail_json(msg=f"Unexpected error: {to_native(e)}")

        return self.result


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", required=True),
            state=dict(
                type="str",
                default="present",
                choices=["present", "absent", "started", "stopped"],
            ),
            memory_mb=dict(type="int", default=1024),
            vcpus=dict(type="int", default=1),
            disk_gb=dict(type="int", default=20),
            image_path=dict(type="path", required=True),
            network=dict(type="str", default="user", choices=["user", "bridge"]),
            bridge_interface=dict(type="str"),
        ),
        supports_check_mode=True,
    )

    if module.check_mode:
        module.exit_json(changed=False)

    vm = QemuVM(module)
    result = vm.ensure_state()
    module.exit_json(**result)


if __name__ == "__main__":
    main()

