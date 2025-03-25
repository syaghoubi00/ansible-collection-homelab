# Role Name

Installs Docker

## Requirements

None.

## Role Variables

`docker_user: docker` - The name of the user that will be created for rootless docker

`docker_rootless_mode: false` - Whether to install docker in rootless mode

`docker_ssh_public_key:` - The public key to add to the authorized_keys file of the docker user

## Dependencies

None.

## Example Playbook

```yaml
- hosts: docker
  vars:
    docker_rootless_mode: true
    docker_user: "rootless_docker"
  roles:
    - syaghoubi00.docker
```

## License

GPL-3.0-or-later

## Author Information

Created by Sebastian Yaghoubi
