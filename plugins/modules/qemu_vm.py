#!/usr/bin/python

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: qemu_vm
short_description: Manages QEMU virtual machines
description:
    - Create, delete, start, stop QEMU virtual machines
    - Configures basic VM parameters like memory, CPU, and disk
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
        type: str
        required: true
    network:
        description: Network interface type
        choices: ['user', 'bridge']
        default: user
        type: str
    bridge_interface:
        description: Bridge interface name when network=bridge
        type: str
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

- name: Remove a VM
  qemu_vm:
    name: test-vm
    state: absent
"""

import os
import subprocess

from ansible.module_utils.basic import AnsibleModule


def get_vm_status(module, vm_name):
    """Check if VM exists and get its status"""
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        if f"qemu-system-x86_64.*-name {vm_name}" in result.stdout:
            return "running"
        elif os.path.exists(f"/var/lib/qemu/{vm_name}.qcow2"):
            return "stopped"
        return "absent"
    except subprocess.CalledProcessError:
        module.fail_json(msg=f"Failed to get status for VM {vm_name}")


def create_disk_image(module, image_path, size_gb):
    """Create a QCOW2 disk image"""
    if not os.path.exists(image_path):
        try:
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", image_path, f"{size_gb}G"],
                check=True,
            )
        except subprocess.CalledProcessError:
            module.fail_json(msg=f"Failed to create disk image {image_path}")


def start_vm(module, params):
    """Start the VM with specified parameters"""
    cmd = [
        "qemu-system-x86_64",
        "-name",
        params["name"],
        "-m",
        str(params["memory_mb"]),
        "-smp",
        str(params["vcpus"]),
        "-drive",
        f"file={params['image_path']},format=qcow2",
        "-daemonize",
    ]

    if params["network"] == "user":
        cmd.extend(["-net", "nic", "-net", "user"])
    elif params["network"] == "bridge":
        if not params.get("bridge_interface"):
            module.fail_json(msg="bridge_interface is required when network=bridge")
        cmd.extend(["-net", "nic", "-net", f"bridge,br={params['bridge_interface']}"])

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        module.fail_json(msg=f"Failed to start VM {params['name']}")


def stop_vm(module, vm_name):
    """Stop the VM"""
    try:
        subprocess.run(
            ["pkill", "-f", f"qemu-system-x86_64.*-name {vm_name}"], check=True
        )
    except subprocess.CalledProcessError:
        module.fail_json(msg=f"Failed to stop VM {vm_name}")


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
            image_path=dict(type="str", required=True),
            network=dict(type="str", default="user", choices=["user", "bridge"]),
            bridge_interface=dict(type="str"),
        )
    )

    params = module.params
    vm_status = get_vm_status(module, params["name"])
    changed = False

    if params["state"] == "absent":
        if vm_status != "absent":
            if vm_status == "running":
                stop_vm(module, params["name"])
            try:
                os.remove(params["image_path"])
            except OSError:
                module.fail_json(
                    msg=f"Failed to remove VM image {params['image_path']}"
                )
            changed = True

    elif params["state"] in ["present", "started"]:
        if vm_status == "absent":
            create_disk_image(module, params["image_path"], params["disk_gb"])
            if params["state"] == "started":
                start_vm(module, params)
            changed = True
        elif vm_status == "stopped" and params["state"] == "started":
            start_vm(module, params)
            changed = True

    elif params["state"] == "stopped":
        if vm_status == "running":
            stop_vm(module, params["name"])
            changed = True

    module.exit_json(changed=changed, status=get_vm_status(module, params["name"]))


if __name__ == "__main__":
    main()
