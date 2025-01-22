# QEMU VM Module

## Overview

The `qemu_vm` module manages QEMU virtual machines on a host system.

## Requirements

- QEMU installed on the host
- Python 3.6 or higher
- Root/sudo access for VM management

## Usage

```yaml
# Create a new VM with 2GB RAM and 2 vCPUs
- name: Create basic VM
  qemu_vm:
    name: test-vm
    state: present
    memory_mb: 2048
    vcpus: 2
    disk_gb: 20
    image_path: /var/lib/qemu/test-vm.qcow2

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

# Stop all VMs
- name: Stop VMs
  qemu_vm:
    name: "{{ item }}"
    state: stopped
  loop: "{{ groups['qemu_vms'] }}"

# Remove VM and cleanup
- name: Remove VM
  qemu_vm:
    name: test-vm
    state: absent
```

### Getting VM Info

```yaml
- name: Get VM info
  qemu_vm:
    name: test-vm
    state: present
  register: vm_result

- name: Show VM info
  debug:
    msg: |
      VM Name: {{ vm_result.vm_info.name }}
      Status: {{ vm_result.vm_info.status }}
      IP Address: {{ vm_result.vm_info.ip_address | default('Not Available') }}
      Memory: {{ vm_result.vm_info.memory_mb }}MB
      vCPUs: {{ vm_result.vm_info.vcpus }}
```

### Building a Dynamic inventory

```yaml
- name: Create VMs and build inventory
  hosts: localhost
  tasks:
    - name: Create and start VM
      qemu_vm:
        name: "vm-{{ item }}"
        state: started
        wait_for_ip: true
        ip_timeout: 300
      register: vm_result
      loop: "{{ range(1, 4) }}" # Creates vm-1, vm-2, vm-3

    - name: Add hosts to in-memory inventory
      add_host:
        name: "{{ item.vm_info.name }}"
        ansible_host: "{{ item.ip_address }}"
        groups: qemu_vms
      loop: "{{ vm_result.results }}"
      when: item.ip_address is defined
```

#### Creating static inventory

```yaml
- name: Create VMs and build inventory
  hosts: localhost
  tasks:
    - name: Create and start VM
      qemu_vm:
        name: "vm-{{ item }}"
        state: started
        wait_for_ip: true
        ip_timeout: 300
      register: vm_result
      loop: "{{ range(1, 4) }}" # Creates vm-1, vm-2, vm-3

    - name: Build inventory file
      copy:
        content: |
          [qemu_vms]
          {% for vm in vm_result.results %}
          {{ vm.vm_info.name }} ansible_host={{ vm.ip_address }}
          {% endfor %}
        dest: "./qemu_inventory.ini"
      when: vm_result.changed
```

#### Running a command against qemu vm group

```yaml
- name: Configure all QEMU VMs
  hosts: qemu_vms
  tasks:
    - name: Do something with the VMs
      debug:
        msg: "Configuring {{ inventory_hostname }} at {{ ansible_host }}"
```
