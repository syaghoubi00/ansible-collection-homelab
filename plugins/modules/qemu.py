# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025, Sebastian Yaghoubi <sebastianyaghoubi@gmail.com>
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)

"""QEMU VM management module for Ansible.

Provides lightweight VM creation primarily for testing Ansible playbooks and roles.
"""

from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import yaml
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_native

DOCUMENTATION = r"""
---
module: qemu
options:
    ssh_port:
        description: Host port to forward to guest SSH. Uses dynamic port if not specified.
        type: int
        required: false
"""


class QemuError(Exception):
    """Custom exception for QEMU operations."""


@dataclass
class QemuResult:
    """Data class for VM operation results."""

    changed: bool = False
    state: str = "started"
    name: str = ""
    ssh_port: int | None = None
    # NOTE: A list[str] would make machine parsing of cmd from Ansible
    # output easier. Unsure of a use case for that though, as most
    # everything put into cmd should be able to be found in the
    # 'module.param' inputs
    cmd: str | None = None
    ip_address: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary, excluding None values."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


class Qemu:
    """Manages QEMU virtual machine operations."""

    def __init__(self, module: Any) -> None:  # noqa: ANN401
        """Initialize QEMU VM manager."""
        self.module = module
        self.params = module.params
        self.check_mode = module.check_mode

        self.result = QemuResult()
        self.temp_files: list[str | Path] = []

    def __del__(self) -> None:
        """Cleanup temporary files on object destruction."""
        for temp_file in self.temp_files:
            try:
                Path(temp_file).unlink(missing_ok=True)
            except OSError as e:
                self.module.warn(f"Failed to cleanup {temp_file}: {e}")

    def _run_command(
        self,
        command: list | str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a command and handle errors."""
        try:
            # NOTE: _create_cloud_init_iso passes the tmp_path and tmp_file as str
            if isinstance(command, str):
                command = shlex.split(command)  # Safely split the string into a list
            return subprocess.run(command, check=check, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            msg = f"Command failed: {to_native(e.stderr)}"
            raise QemuError(msg) from e
        except Exception as e:
            msg = f"Error executing command: {to_native(e)}"
            raise QemuError(msg) from e

    def _create_cloud_init_iso(self) -> Path | None:
        """Create cloud-init drive if config is provided."""
        if not self.params.get("cloud_init"):
            return None

        temp_dir = Path(tempfile.mkdtemp())
        self.temp_files.append(temp_dir)

        user_data_file = temp_dir / "user-data"
        user_data_file.write_text(
            f"#cloud-config\n{yaml.safe_dump(self.params["cloud_init"])}",
        )

        iso_path = (
            Path(self.params["image"]).parent / f"{self.params["name"]}-cloud-init.iso"
        )
        self.temp_files.append(iso_path)

        # NOTE: Add more ISO gen tools for better portability
        # like "xorrisofs", "genisoimage", "mkisofs"
        iso_gen_program = ["cloud-localds"]
        for program in iso_gen_program:
            if shutil.which(program):
                break

        if iso_gen_program == "cloud-localds":
            self._run_command(["cloud-localds", str(iso_path), str(user_data_file)])

        return iso_path

    def _get_unused_port(self) -> int:
        """Get an unused ephemeral port by creating a temporary socket.

        Returns:
            int: An available port number.

        """
        with socket.socket() as s:
            # Bind to port 0 to let the OS assign an unused port
            s.bind(("", 0))
            # Return the port number assigned by the OS
            return s.getsockname()[1]

    def _build_vm_command(self) -> list[str]:
        """Build QEMU command with all parameters."""
        cmd = [
            self.params["qemu_binary"],
            "-name",
            self.params["name"],
            "-m",
            str(self.params["memory_mb"]),
            "-smp",
            str(self.params["vcpus"]),
            "-drive",
            # TODO: Make snapshots optional
            f"file={self.params['image']},format=qcow2,snapshot=on",
            "-display",
            "none",
            "-daemonize",
            "-cpu",
            "host",
        ]

        cloud_init_iso = self._create_cloud_init_iso()
        if cloud_init_iso:
            cmd.extend(["-drive", f"file={cloud_init_iso},format=raw,media=cdrom"])

        if self.params["network_mode"] == "user":
            ssh_port = self.params.get("ssh_port") or self._get_unused_port()
            self.result.ssh_port = ssh_port
            self.result.ip_address = "localhost"
            cmd.extend(
                [
                    "-nic",
                    f"user, hostfwd=tcp::{ssh_port}-:22",
                    # NOTE: Test -nic code before deleting -netdev
                    # "-netdev",
                    # f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
                    # "-device",
                    # "virtio-net,netdev=net0",
                ],
            )
        else:
            cmd.extend(
                [
                    "-nic",
                    f"{self.params["network_mode"]},br={self.params["network_interface"]}",
                    # "-netdev",
                    # f"{self.params["network_mode"]},id=net0,br={self.params["network_interface"]}",
                    # "-device",
                    # "virtio-net,netdev=net0",
                ],
            )
        self.result.cmd = " ".join(map(str, cmd))
        return cmd

    def _wait_for_ssh(
        self,
        ssh_port: int,
        wait_time: int,
    ) -> None:
        """Wait for the SSH port to be reachable."""
        retry_interval = 5
        deadline = time.time() + wait_time

        while time.time() < deadline:
            # PERF: PERF203: Running the loop without try/except
            # with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            #     sock.settimeout(5)
            #     result = sock.connect_ex((ip_addr, ssh_port))
            #     if result == 0:
            #         return {"success": True, "error": None}
            #     time.sleep(retry_interval)

            try:
                with socket.create_connection(
                    (self.result.ip_address, ssh_port),
                    timeout=5,
                ):
                    return
            except (OSError, socket.timeout):
                time.sleep(retry_interval)

        msg = f"SSH connection failed after {wait_time} seconds"
        raise QemuError(msg)

    def _get_vm_pid(self) -> int | None:
        """Check if VM is running and get its PID."""
        try:
            result = self._run_command(["pgrep", "-f", f"qemu.*{self.params['name']}"])
            return int(result.stdout.strip())
        except (QemuError, ValueError):
            return None

    def _stop_vm(self, vm_pid: int | None = None) -> None:
        vm_pid = vm_pid or self._get_vm_pid()
        if vm_pid:
            try:
                self._run_command(["kill", str(vm_pid)])
                self.result.changed = True
            except QemuError as e:
                self.module.fail_json(msg=str(e))

    def launch_vm(self) -> dict[str, Any]:
        """Launch a vm."""
        self.result.name = self.params["name"]
        vm_pid = self._get_vm_pid()

        try:
            if self.params["state"] == "started":
                if not vm_pid:
                    # cloud_init_iso = self._create_cloud_init_iso()
                    # self._build_vm_command(cloud_init_iso)

                    self._build_vm_command()

                    # self._run_command(cmd)
                    self._wait_for_ssh(
                        self.result.ssh_port,
                        self.params["network_timeout"],
                    )

                    # # Wait for IP if requested and using custom networking
                    # if (
                    #     self.params["network_mode"] == "custom"
                    #     and self.params["wait_for_ip"]
                    # ):
                    #     self.result.ip_address = self._wait_for_ip(
                    #         self.params["network_timeout"]
                    #     )

                    self.result.changed = True
            elif self.params["state"] == "stopped" and vm_pid:
                self._stop_vm(vm_pid)
        except QemuError as e:
            if self.params["cleanup_on_failure"]:
                self._stop_vm()
            self.module.fail_json(msg=str(e))

        return self.result.to_dict()


def main() -> None:
    """Ansible Module entry point."""
    module_args = {
        "name": {"type": "str", "required": True},
        "image": {"type": "path", "required": True},
        "state": {
            "type": "str",
            "default": "started",
            "choices": ["started", "stopped"],
        },
        "ssh_port": {"type": "int"},
        "cleanup_on_failure": {"type": "bool", "default": True},
        "network_mode": {
            "type": "str",
            "default": "user",
            "choices": ["user", "custom"],
        },
        "network_type": {
            "type": "str",
            "choices": ["bridge", "tap"],
        },
        "network_interface": {
            "type": "str",
        },
        "network_timeout": {"type": "int", "default": 300},
        # NOTE: Need to add executable validation, since
        # allowing custom binary path could allow
        # arbitrary code execution
        "qemu_binary": {"type": "str", "default": "qemu-system-x86_64"},
        "memory_mb": {"type": "int", "default": 1024},
        "vcpus": {"type": "int", "default": 1},
    }
    module_reqs = [
        ("network_mode", "custom", ["network_interface", "network_type"]),
    ]

    module = AnsibleModule(
        argument_spec=module_args,
        required_if=module_reqs,
        supports_check_mode=True,
    )

    qemu = Qemu(module)
    result = qemu.launch_vm()

    module.exit_json(**result)


if __name__ == "__main__":
    main()
