# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025, Sebastian Yaghoubi <sebastianyaghoubi@gmail.com>
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)

"""QEMU VM management module for Ansible.

Provides lightweight VM creation primarily for testing Ansible playbooks and roles.
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import time
from typing import Any, cast

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_native


class QemuError(Exception):
    """Custom exception for QEMU operations."""


class Qemu:
    """Manages QEMU virtual machine operations."""

    def __init__(self, module: AnsibleModule) -> None:
        """Initialize QEMU VM manager."""
        self.module = module
        # self.params = module.params
        # HACK: Explicitly cast to a dict because the type isn't read from AnsibleModule
        # NOTE: Needs further investigation
        self.params = cast(dict[str, Any], module.params)

    def _run_command(
        self,
        command: list | str,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a command and handle errors."""
        try:
            # NOTE: See if str is used, otherwise just pass in list and delete the
            # check and shlex split
            if isinstance(command, str):
                command = shlex.split(command)  # Safely split the string into a list
            return subprocess.run(command, check=check, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            msg = f"Command failed: {to_native(e.stderr)}"
            raise QemuError(msg) from e
        except Exception as e:
            msg = f"Error executing command: {to_native(e)}"
            raise QemuError(msg) from e

    def _get_vm_ip(self) -> str | None:
        """Get VM IP address."""
        vm_ip = "localhost"

        if self.params.get("network_mode") == "user":
            vm_ip = "localhost"

        return vm_ip

    # NOTE: Returning a dict with success/error info
    # idea is to pass into Ansible, like
    # ssh_access = self._wait_for_ssh()
    # if ssh_access['error']:
    #     module.fail_json(msg=ssh_access['error'])
    def _wait_for_ssh(
        self,
        ssh_port: int = 22,
        timeout: int = 300,
    ) -> dict[str, bool | str | None]:
        """Wait for the SSH port to be reachable."""
        retry_interval = 5
        deadline = time.time() + timeout

        while time.time() < deadline:
            # PERF: PERF203: Running the loop without try/except
            # with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            #     sock.settimeout(5)
            #     result = sock.connect_ex((ip_addr, ssh_port))
            #     if result == 0:
            #         return {"success": True, "error": None}
            #     time.sleep(retry_interval)

            try:
                with socket.create_connection((self._get_vm_ip(), ssh_port), timeout=5):
                    return {"success": True, "error": None}
            except (OSError, socket.timeout):
                time.sleep(retry_interval)
        return {
            "success": False,
            "error": f"SSH connection failed after {timeout} seconds",
        }


def main() -> None:
    """Ansible Module entry point."""
    module_args = {
        "name": {"type": "str", "required": True},
    }

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
    )

    Qemu(module)


if __name__ == "__main__":
    main()
