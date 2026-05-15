# nox

Lightweight VM manager for Linux using KVM/libvirt. Built with Bun/TypeScript.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/0sage/nox/main/install.sh | bash
```

## Commands

| Command | Example | Description |
|---------|---------|-------------|
| `create <name>` | `nox create myvm --cpus 2 --ram 1024 --disk 10` | Create VM |
| `start <name>` | `nox start myvm` | Start VM |
| `stop <name>` | `nox stop myvm` | Stop VM |
| `restart <name>` | `nox restart myvm` | Restart VM |
| `delete\|rm <name>` | `nox rm myvm` | Delete VM |
| `list\|ls` | `nox ls` | List all VMs |
| `status <name>` | `nox status myvm` | VM details |
| `ssh <name>` | `nox ssh myvm -- uname -a` | SSH into VM |
| `passwd <name>` | `nox passwd myvm` | Change password |
| `resize <name>` | `nox resize myvm --cpus 4 --ram 2048 --disk 20` | Resize resources |
| `doctor` | `nox doctor` | System health check |
| `update\|up` | `nox update` | Update nox |

## Create Options

| Option | Default | Description |
|--------|---------|-------------|
| `--os` | `debian` | OS image |
| `--cpus` | `1` | CPU limit (fractional: `0.5` = half a core) |
| `--ram` | `512` | RAM in MB |
| `--disk` | `5` | Disk in GB |
| `--network` | auto | Libvirt network |
| `--no-autostart` | | Disable boot autostart |
| `--no-start` | | Create without starting |

## Requirements

- Linux (Debian/Ubuntu/Alpine), ARM64 or x86_64
- KVM/QEMU + libvirt

## Files

| Path | Description |
|------|-------------|
| `~/.nox/vms/` | VM storage and metadata |
| `~/.nox/images/` | Cached OS images |
| `~/.nox/config.json` | Configuration |

## License

MIT
