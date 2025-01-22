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
    wait_for_ip:
        description: Wait for IP address to be available after starting VM
        type: bool
        default: false
    ip_timeout:
        description: Maximum time in seconds to wait for IP address
        type: int
        default: 300
notes:
    - Requires QEMU to be installed on the host system
    - Requires appropriate permissions to create and manage VMs
seealso:
    - name: QEMU Documentation
      link: https://www.qemu.org/docs/master/
      description: Official QEMU documentation
requirements:
    - python >= 3.6
    - qemu-system-x86_64
    - qemu-img
"""

EXAMPLES = r"""
# Create multiple VMs and wait for IP addresses
- name: Create VM cluster
  qemu_vm:
    name: "vm-{{ item }}"
    state: started
    memory_mb: 4096
    vcpus: 2
    disk_gb: 40
    image_path: "/var/lib/qemu/vm-{{ item }}.qcow2"
    wait_for_ip: true
  register: vm_result
  loop: "{{ range(1, 4) }}"

# Build a dynamic inventory using the created VMs
- name: Add hosts to in-memory inventory
    add_host:
    name: "{{ item.vm_info.name }}"
    ansible_host: "{{ item.ip_address }}"
    groups: qemu_vms
    loop: "{{ vm_result.results }}"
    when: item.ip_address is defined

# Remove VMs and cleanup
- name: Stop VMs
  qemu_vm:
    name: "{{ item }}"
    state: absent
  loop: "{{ groups['qemu_vms'] }}"
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
ip_address:
    description: IP address of the VM if available
    type: str
    returned: when wait_for_ip=true or state=started
    sample: '192.168.1.100'
"""

import json
import os
import subprocess
import time
from ipaddress import IPv4Address

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
            "ip_address": None,
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

    def _get_vm_ip(self):
        """
        Get IP address for the VM using multiple methods
        """
        # Method 1: Try QEMU Guest Agent
        try:
            ga_socket = f"/var/run/qemu-ga/{self.params['name']}.sock"
            if os.path.exists(ga_socket):
                result = self._run_command(
                    ["qemu-ga", "--cmd", "guest-network-get-interfaces", ga_socket],
                    check_rc=False,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    for iface in data.get("return", []):
                        for ip in iface.get("ip-addresses", []):
                            if ip["ip-address-type"] == "ipv4":
                                try:
                                    # Validate IP address
                                    IPv4Address(ip["ip-address"])
                                    return ip["ip-address"]
                                except ValueError:
                                    continue
        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
            pass

        # Method 2: Try ARP table lookup
        try:
            # Get VM's MAC address from QEMU process
            vm_process = self._run_command(["ps", "-ef"], check_rc=False)
            mac_address = None

            for line in vm_process.stdout.splitlines():
                if f"-name {self.params['name']}" in line:
                    # Extract MAC from command line
                    for part in line.split():
                        if part.startswith("mac="):
                            mac_address = part.split("=")[1]
                            break

            if mac_address:
                arp_result = self._run_command(["arp", "-n"], check_rc=False)
                for line in arp_result.stdout.splitlines():
                    if mac_address.lower() in line.lower():
                        ip = line.split()[0]
                        try:
                            IPv4Address(ip)
                            return ip
                        except ValueError:
                            continue
        except (subprocess.CalledProcessError, IndexError):
            pass

        # Method 3: Check QEMU DHCP leases
        dhcp_lease_file = "/var/lib/qemu/dhcp.leases"
        if os.path.exists(dhcp_lease_file):
            try:
                with open(dhcp_lease_file, "r") as f:
                    for line in f:
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

    def _wait_for_ip(self, timeout=300):
        """
        Wait for IP address to become available
        """
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            ip = self._get_vm_ip()
            if ip:
                return ip
            time.sleep(5)
        return None

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
        """Start the VM and optionally wait for IP"""
        cmd = self._build_vm_command()
        self._run_command(cmd)

        # Verify VM started successfully
        status, pid = self._get_vm_status()
        if status != "running":
            raise QemuVmError("Failed to start VM")

        # Wait for IP if requested
        ip_address = None
        if self.params["wait_for_ip"]:
            ip_address = self._wait_for_ip(self.params["ip_timeout"])
            if not ip_address and self.params.get("fail_on_no_ip", False):
                raise QemuVmError("Failed to obtain IP address within timeout period")

        return pid, ip_address

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
        """
        Get current VM configuration including IP address
        Returns a dictionary with VM details or None if VM doesn't exist
        """
        if os.path.exists(self.params["image_path"]):
            # Build base VM information dictionary
            info = {
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

            # Get current VM status and pid
            status, pid = self._get_vm_status()
            info["status"] = status
            if pid:
                info["pid"] = pid

            # Only try to get IP if VM is running
            if status == "running":
                info["ip_address"] = self._get_vm_ip()

            return info
        return None

    def ensure_state(self):
        """Ensure VM is in desired state"""
        current_status, current_pid = self._get_vm_status()

        try:
            if self.params["state"] == "started":
                if current_status != "running":
                    pid, ip_address = self._start_vm()
                    self.result["changed"] = True
                    self.result["pid"] = pid
                    self.result["ip_address"] = ip_address
                elif self.params["wait_for_ip"]:
                    # VM already running but IP requested
                    self.result["ip_address"] = self._get_vm_ip()

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

            # Always try to get IP for running VMs
            if final_status == "running" and not self.result["ip_address"]:
                self.result["ip_address"] = self._get_vm_ip()

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
            wait_for_ip=dict(type="bool", default=False),
            ip_timeout=dict(type="int", default=300),
            fail_on_no_ip=dict(type="bool", default=False),
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
