#!/usr/bin/env python3
"""nox - Lightweight VM Manager using libvirt/KVM"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import base64
import secrets
import string

def get_version():
    """Read version from VERSION file."""
    locations = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
        "/usr/local/bin/VERSION",
    ]
    for version_file in locations:
        try:
            with open(version_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "unknown"

VERSION = get_version()
NOX_DIR = os.path.expanduser("~/.nox")
VMS_DIR = os.path.join(NOX_DIR, "vms")
IMAGES_DIR = os.path.join(NOX_DIR, "images")
CONFIG_FILE = os.path.join(NOX_DIR, "config.json")

DEFAULT_CONFIG = {
    "defaults": {"os": "debian", "cpus": 1, "ram": 512, "disk": 5},
}

# OS image URLs (cloud images with cloud-init support)
OS_IMAGES = {
    "debian": {
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
        "url_amd64": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_dirs():
    os.makedirs(VMS_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)


def host_arch():
    m = os.uname().machine
    if m in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def host_ram_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    return 1024

def host_disk_gb(path="/"):
    st = os.statvfs(path)
    return (st.f_frsize * st.f_blocks) // (1024 ** 3)

def resolve_resource(value, host_total):
    """Resolve a resource value. Only values strictly between 0 and 1 (exclusive)
    are treated as fractions of host_total. Everything else is absolute."""
    v = float(value)
    if 0 < v < 1:
        return max(1, int(math.ceil(v * host_total)))
    return max(1, int(v))

def resolve_cpus(value):
    """Resolve CPU value into (vcpus, cpu_quota_fraction).

    Returns:
        vcpus: number of virtual CPUs (always >= 1, ceil of value)
        cpu_fraction: the raw float for quota calculation

    Examples:
        0.5 → 1 vCPU, quota = 50% of 1 core
        1   → 1 vCPU, quota = 100% of 1 core
        1.5 → 2 vCPUs, quota = 150% total (1.5 cores)
        2   → 2 vCPUs, quota = 200% total (2 cores)
    """
    v = float(value)
    if v <= 0:
        v = 1
    vcpus = max(1, math.ceil(v))
    return vcpus, v

def run(cmd, check=True, capture=True):
    """Run a shell command."""
    if capture:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {cmd}\n{result.stderr}")
        return result
    else:
        result = subprocess.run(cmd, shell=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {cmd}")
        return result

def virsh(cmd, check=True):
    """Run a virsh command."""
    return run(f"virsh --connect qemu:///system {cmd}", check=check, capture=True)

def vm_exists(name):
    """Check if VM exists."""
    result = virsh(f"dominfo {name}", check=False)
    return result.returncode == 0

def vm_state(name):
    """Get VM state."""
    result = virsh(f"domstate {name}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()

def vm_dir(name):
    return os.path.join(VMS_DIR, name)

def meta_path(name):
    return os.path.join(vm_dir(name), "meta.json")

def load_meta(name):
    p = meta_path(name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None

def save_meta(name, meta):
    d = vm_dir(name)
    os.makedirs(d, exist_ok=True)
    with open(meta_path(name), "w") as f:
        json.dump(meta, f, indent=2)

def vm_ip(name, timeout=60):
    """Get VM IP address using qemu-guest-agent."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = virsh(f"domifaddr {name} --source agent", check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "ipv4" in line.lower() and "127.0.0.1" not in line:
                    parts = line.split()
                    for part in parts:
                        if "/" in part and not part.startswith("127."):
                            return part.split("/")[0]

        time.sleep(2)

    return None
def apply_cpu_limit(name, cpu_fraction):
    """Apply CPU cgroup limits so a VM cannot exceed its allocated CPU time.

    cpu_fraction is the raw value from --cpus (e.g. 0.5, 1, 2).
    quota = cpu_fraction * period gives exact CPU time limiting.
    """
    period = 100000  # 100ms default period
    quota = max(1000, int(cpu_fraction * period))  # min 1ms to avoid zero
    params = f"--set vcpu_quota={quota} --set vcpu_period={period} --set emulator_quota={quota} --set emulator_period={period} --set global_quota={quota} --set global_period={period}"

    state = vm_state(name)
    if state == "running":
        # Apply to both live and config
        virsh(f"schedinfo {name} {params} --config --live", check=False)
    else:
        # Config-only for stopped VMs
        virsh(f"schedinfo {name} {params} --config", check=False)
    print(f"✓ CPU limit applied: {cpu_fraction} CPU(s) max ({quota}/{period} quota/period)")


def generate_password():
    """Generate random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))

# ---------------------------------------------------------------------------
# Bridge networking
# ---------------------------------------------------------------------------

BRIDGE_NAME = "br0"
BRIDGE_CONF = "/etc/network/interfaces.d/br0"
LIBVIRT_NET = "bridged"

def detect_primary_interface():
    """Detect the primary physical network interface (the one with the default route)."""
    result = run("ip route show default", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return "eth0"
    # "default via 10.0.0.1 dev eth0 ..."
    parts = result.stdout.strip().split()
    for i, p in enumerate(parts):
        if p == "dev" and i + 1 < len(parts):
            iface = parts[i + 1]
            # If already on br0, find the enslaved physical interface
            if iface == BRIDGE_NAME:
                br_result = run(f"ls /sys/devices/virtual/net/{BRIDGE_NAME}/brif/", check=False)
                if br_result.returncode == 0 and br_result.stdout.strip():
                    return br_result.stdout.strip().split()[0]
            return iface
    return "eth0"

def bridge_exists():
    """Check if br0 bridge already exists."""
    return os.path.exists(f"/sys/class/net/{BRIDGE_NAME}")

def libvirt_net_exists():
    """Check if the bridged libvirt network is defined."""
    result = virsh(f"net-info {LIBVIRT_NET}", check=False)
    return result.returncode == 0

def get_interface_config():
    """Read current IP config from the primary interface or bridge."""
    iface = BRIDGE_NAME if bridge_exists() else detect_primary_interface()
    result = run(f"ip -4 addr show {iface}", check=False)
    if result.returncode != 0:
        return None, None, None
    ip_cidr = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            ip_cidr = line.split()[1]
            break
    if not ip_cidr:
        return None, None, None
    # Get gateway
    gw_result = run("ip route show default", check=False)
    gateway = None
    if gw_result.returncode == 0:
        for part in gw_result.stdout.split():
            if part.count('.') == 3 and not part.startswith("src"):
                # Find the token after "via"
                parts = gw_result.stdout.split()
                for i, p in enumerate(parts):
                    if p == "via" and i + 1 < len(parts):
                        gateway = parts[i + 1]
                        break
                break
    # Get DNS from dhcpcd or resolv.conf
    dns = None
    resolv = run("grep nameserver /etc/resolv.conf", check=False)
    if resolv.returncode == 0:
        for line in resolv.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver" and not parts[1].startswith("127"):
                dns = parts[1]
                break
    return ip_cidr, gateway, dns

def ensure_bridge():
    """Create br0 bridge and bridged libvirt network if they don't exist."""
    if bridge_exists() and libvirt_net_exists():
        # Ensure libvirt network is started
        virsh(f"net-start {LIBVIRT_NET}", check=False)
        return True

    phys_iface = detect_primary_interface()
    # Don't bridge if already on br0
    if phys_iface == BRIDGE_NAME:
        if not libvirt_net_exists():
            _create_libvirt_bridged_net()
        virsh(f"net-start {LIBVIRT_NET}", check=False)
        return True

    ip_cidr, gateway, dns = get_interface_config()
    if not ip_cidr or not gateway:
        print(f"Error: Could not detect IP config on {phys_iface}", file=sys.stderr)
        return False

    print(f"Setting up bridge {BRIDGE_NAME} on {phys_iface} ({ip_cidr})...")

    if not bridge_exists():
        # Install bridge-utils if needed
        run("dpkg -s bridge-utils >/dev/null 2>&1 || sudo apt-get install -y -qq bridge-utils", check=False)

        # Write bridge config for persistence
        dns_line = f"    dns-nameservers {dns}" if dns else ""
        conf = f"""# Managed by nox
auto {BRIDGE_NAME}
iface {BRIDGE_NAME} inet static
    bridge_ports {phys_iface}
    bridge_stp off
    bridge_fd 0
    address {ip_cidr}
    gateway {gateway}
{dns_line}

# Physical interface must be manual when bridged
auto {phys_iface}
iface {phys_iface} inet manual
"""
        run(f"sudo tee {BRIDGE_CONF} > /dev/null << 'NOXEOF'\n{conf}NOXEOF")

        # Create bridge live (without losing SSH)
        run(f"sudo ip link add name {BRIDGE_NAME} type bridge", check=False)
        run(f"sudo ip link set {BRIDGE_NAME} up")
        run(f"sudo ip link set {phys_iface} master {BRIDGE_NAME}")

        # Move IP from phys to bridge
        ip_addr = ip_cidr.split('/')[0]
        run(f"sudo ip addr add {ip_cidr} dev {BRIDGE_NAME}", check=False)
        run(f"sudo ip addr del {ip_cidr} dev {phys_iface}", check=False)
        run(f"sudo ip route add default via {gateway} dev {BRIDGE_NAME} src {ip_addr}", check=False)

        # Stop dhcpcd on the physical interface to prevent it reclaiming the IP
        run(f"sudo dhcpcd --release {phys_iface} 2>/dev/null", check=False)
        run(f"sudo ip addr flush dev {phys_iface}", check=False)

        print(f"✓ Bridge {BRIDGE_NAME} created on {phys_iface}")

    if not libvirt_net_exists():
        _create_libvirt_bridged_net()

    return True

def _create_libvirt_bridged_net():
    """Define and start the bridged libvirt network."""
    net_xml = f"""<network>
  <name>{LIBVIRT_NET}</name>
  <forward mode="bridge"/>
  <bridge name="{BRIDGE_NAME}"/>
</network>"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
        f.write(net_xml)
        tmp = f.name
    try:
        virsh(f"net-define {tmp}")
        virsh(f"net-start {LIBVIRT_NET}", check=False)
        virsh(f"net-autostart {LIBVIRT_NET}")
        print(f"✓ Libvirt network '{LIBVIRT_NET}' created")
    finally:
        os.unlink(tmp)

def remove_bridge():
    """Remove br0 bridge and restore direct interface config."""
    phys_iface = detect_primary_interface()
    ip_cidr, gateway, dns = get_interface_config()

    # Remove libvirt network
    if libvirt_net_exists():
        virsh(f"net-destroy {LIBVIRT_NET}", check=False)
        virsh(f"net-undefine {LIBVIRT_NET}", check=False)
        print(f"✓ Libvirt network '{LIBVIRT_NET}' removed")

    # Remove bridge
    if bridge_exists():
        # Move IP back to physical interface
        if ip_cidr and phys_iface:
            ip_addr = ip_cidr.split('/')[0]
            run(f"sudo ip addr add {ip_cidr} dev {phys_iface}", check=False)
            run(f"sudo ip link set {phys_iface} nomaster", check=False)
            run(f"sudo ip link delete {BRIDGE_NAME}", check=False)
            run(f"sudo ip route add default via {gateway} dev {phys_iface} src {ip_addr}", check=False)
        else:
            run(f"sudo ip link delete {BRIDGE_NAME}", check=False)

        # Remove config file
        if os.path.exists(BRIDGE_CONF):
            run(f"sudo rm -f {BRIDGE_CONF}")

        # Restart dhcpcd to reclaim the interface
        run("sudo systemctl restart dhcpcd", check=False)

        print(f"✓ Bridge {BRIDGE_NAME} removed, {phys_iface} restored")

# ---------------------------------------------------------------------------
# Cloud-init generation
# ---------------------------------------------------------------------------

def generate_cloud_init(name, password, ssh_key=None, os_name="debian"):
    """Generate cloud-init user-data."""
    # Find SSH public key
    if not ssh_key:
        for keyfile in ["id_ed25519.pub", "id_rsa.pub"]:
            p = os.path.expanduser(f"~/.ssh/{keyfile}")
            if os.path.exists(p):
                with open(p) as f:
                    ssh_key = f.read().strip()
                break

    user_data = f"""#cloud-config
hostname: {name}
fqdn: {name}.local
manage_etc_hosts: true

users:
  - name: nox
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
    lock_passwd: false
    passwd: {password}
    ssh_authorized_keys:
      - {ssh_key if ssh_key else ''}

ssh_pwauth: true

package_update: true
package_upgrade: false
packages:
  - qemu-guest-agent

runcmd:
  - echo 'nox:{password}' | chpasswd
  - systemctl enable qemu-guest-agent
  - systemctl start qemu-guest-agent
  - systemctl enable ssh
  - systemctl start ssh
"""

    meta_data = f"""instance-id: {name}
local-hostname: {name}
"""

    return user_data, meta_data

# ---------------------------------------------------------------------------
# VM creation
# ---------------------------------------------------------------------------

def create_vm(name, os_name=None, cpus=None, ram=None, disk=None,
              autostart=False, password=None, start=True, network=None):
    """Create a new VM."""
    if vm_exists(name):
        print(f"VM '{name}' already exists.")
        return False, None

    cfg = load_config()
    defaults = cfg.get("defaults", DEFAULT_CONFIG["defaults"])

    os_name = os_name or defaults.get("os", "debian")
    cpus = cpus if cpus is not None else defaults.get("cpus", 1)
    ram = ram if ram is not None else defaults.get("ram", 512)
    disk = disk if disk is not None else defaults.get("disk", 5)

    arch = host_arch()
    vcpus, cpu_fraction = resolve_cpus(cpus)
    ram_mb = resolve_resource(ram, host_ram_mb())
    disk_gb = resolve_resource(disk, host_disk_gb())

    if password is None:
        password = generate_password()

    # Ensure bridge network is set up
    if network is None:
        network = LIBVIRT_NET
    if network == LIBVIRT_NET:
        if not ensure_bridge():
            print("Error: Failed to set up bridge network.", file=sys.stderr)
            return False, None

    network_arg = f"network={network},model=virtio"

    print(f"Creating VM '{name}': os={os_name} vcpus={vcpus} ram={ram_mb}MB disk={disk_gb}GB")

    # Create VM directory
    vm_path = vm_dir(name)
    os.makedirs(vm_path, exist_ok=True)

    # Download cloud image to shared cache
    os_info = OS_IMAGES.get(os_name)
    if not os_info:
        raise RuntimeError(f"Unknown OS: {os_name}")

    # Select URL based on architecture
    if arch == "arm64":
        image_url = os_info.get("url")
    else:
        image_url = os_info.get("url_amd64")

    if not image_url:
        raise RuntimeError(f"No image URL for {os_name} on {arch}")

    # Use shared image cache
    image_filename = f"{os_name}-{arch}.qcow2"
    base_image = os.path.join(IMAGES_DIR, image_filename)

    if not os.path.exists(base_image):
        print(f"Downloading {os_name} cloud image...")
        run(f"curl -fsSL {image_url} -o {base_image}")
    else:
        print(f"Using cached {os_name} cloud image")

    # Create disk image from base
    disk_path = os.path.join(vm_path, f"{name}.qcow2")
    run(f"qemu-img create -f qcow2 -F qcow2 -b {base_image} {disk_path} {disk_gb}G")

    # Generate cloud-init
    user_data, meta_data = generate_cloud_init(name, password, os_name=os_name)
    user_data_path = os.path.join(vm_path, "user-data")
    meta_data_path = os.path.join(vm_path, "meta-data")

    with open(user_data_path, "w") as f:
        f.write(user_data)
    with open(meta_data_path, "w") as f:
        f.write(meta_data)

    # Create cloud-init ISO
    cloud_init_iso = os.path.join(vm_path, "cloud-init.iso")
    run(f"genisoimage -output {cloud_init_iso} -volid cidata -joliet -rock {user_data_path} {meta_data_path}")

    # Create VM with virt-install
    cmd_parts = [
        "virt-install",
        "--connect", "qemu:///system",
        "--name", name,
        "--memory", str(ram_mb),
        "--vcpus", str(vcpus),
        "--cpu", "host-passthrough",
        "--disk", f"{disk_path},format=qcow2,bus=virtio,cache=writeback,io=threads",
        "--disk", f"{cloud_init_iso},device=cdrom",
        "--os-variant", "generic",
        "--network", network_arg,
        "--graphics", "none",
        "--console", "pty,target_type=serial",
        "--import",
        "--noautoconsole",
    ]
    if not start:
        cmd_parts.append("--noreboot")

    cmd = " ".join(cmd_parts)

    try:
        run(cmd)
    except RuntimeError as e:
        print(f"Failed to create VM: {e}", file=sys.stderr)
        shutil.rmtree(vm_path, ignore_errors=True)
        return False, None

    # Configure autostart
    if autostart:
        virsh(f"autostart {name}")

    # Apply CPU cgroup limits to enforce vCPU allocation
    apply_cpu_limit(name, cpu_fraction)

    # Save metadata
    meta = {
        "name": name,
        "os": os_name,
        "arch": arch,
        "vcpus": vcpus,
        "cpu_fraction": cpu_fraction,
        "ram_mb": ram_mb,
        "disk_gb": disk_gb,
        "autostart": autostart,
        "network": network,
    }
    save_meta(name, meta)

    if not start:
        print(f"VM '{name}' created (not started).")
    else:
        print(f"VM '{name}' created and starting...")

    return True, password

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_create(args):
    """Create a new VM and show SSH credentials."""
    network = getattr(args, "network", None)

    success, password = create_vm(
        args.name, os_name=args.os, cpus=args.cpus, ram=args.ram,
        disk=args.disk, autostart=not getattr(args, "no_autostart", False),
        start=not args.no_start, network=network
    )

    if not success:
        return

    # Wait for IP if started
    if not args.no_start:
        print("\nWaiting for VM to boot and get IP address...")
        ip = vm_ip(args.name, timeout=180)

        print(f"\n{'='*60}")
        print(f"VM '{args.name}' is ready!")
        print(f"{'='*60}")
        print(f"\nSSH Access (password shown once):")
        print(f"  Password: {password}")
        if ip:
            print(f"  LAN IP:   {ip}")
        else:
            print(f"  IP not detected yet. Use 'nox list' to find it.")
        print(f"\nConnect via SSH:")
        print(f"  nox ssh {args.name}")
        print(f"\nIMPORTANT: Save this password - it won't be shown again!")
        print(f"{'='*60}")

def cmd_start(args):
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    virsh(f"start {args.name}")
    # Re-apply CPU limits on start (live)
    meta = load_meta(args.name)
    if meta:
        cpu_fraction = meta.get("cpu_fraction", meta.get("vcpus", 1))
        apply_cpu_limit(args.name, cpu_fraction)
    print(f"VM '{args.name}' started.")

def cmd_stop(args):
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    virsh(f"shutdown {args.name}")
    print(f"VM '{args.name}' shutting down.")

def cmd_restart(args):
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    virsh(f"reboot {args.name}")
    print(f"VM '{args.name}' restarted.")

def cmd_delete(args):
    if not vm_exists(args.name):
        d = vm_dir(args.name)
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"Cleaned up local files for '{args.name}'.")
        else:
            print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        return

    state = vm_state(args.name)
    if state == "running":
        virsh(f"destroy {args.name}")

    virsh(f"undefine {args.name} --nvram --remove-all-storage")
    d = vm_dir(args.name)
    if os.path.exists(d):
        shutil.rmtree(d)
    print(f"VM '{args.name}' deleted.")

def cmd_list(args):
    result = virsh("list --all", check=False)
    if result.returncode != 0:
        print("Could not list VMs. Is libvirt installed?", file=sys.stderr)
        sys.exit(1)

    # Parse virsh list output
    vm_names = []
    for line in result.stdout.splitlines()[2:]:  # Skip header
        parts = line.split()
        if len(parts) >= 2:
            vm_names.append(parts[1])

    if not vm_names:
        print("No VMs found.")
        return

    print(f"{'NAME':<20} {'STATE':<15} {'OS':<10} {'CPUS':<6} {'RAM':<8} {'DISK':<8} {'AUTOSTART':<10} {'IP'}")
    print("-" * 95)

    for name in vm_names:
        state = vm_state(name) or "UNKNOWN"

        meta = load_meta(name) or {}
        os_name = meta.get("os", "?")
        vcpus = meta.get("vcpus", "?")
        ram = meta.get("ram_mb", "?")
        disk_g = meta.get("disk_gb", "?")
        auto = meta.get("autostart", False)

        ip = ""
        if state == "running":
            ip = vm_ip(name, timeout=1) or ""

        ram_str = f"{ram}MB" if ram != "?" else "?"
        disk_str = f"{disk_g}GB" if disk_g != "?" else "?"
        auto_str = "yes" if auto else "no"

        print(f"{name:<20} {state:<15} {os_name:<10} {vcpus:<6} {ram_str:<8} {disk_str:<8} {auto_str:<10} {ip}")

def ensure_ssh_key():
    """Ensure SSH key exists, generate if needed."""
    ssh_dir = os.path.expanduser("~/.ssh")
    key_path = os.path.join(ssh_dir, "id_ed25519")
    pub_key_path = f"{key_path}.pub"

    if os.path.exists(pub_key_path):
        return

    # Create .ssh directory if it doesn't exist
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)

    # Generate SSH key silently
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", f"nox@{os.uname().nodename}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True
    )

def cmd_ssh(args):
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    state = vm_state(args.name)
    if state != "running":
        print(f"VM '{args.name}' is not running. Start it with: nox start {args.name}", file=sys.stderr)
        sys.exit(1)

    # Ensure SSH key exists
    ensure_ssh_key()

    # Get VM IP address
    print(f"Getting IP address for '{args.name}'...")
    ip = vm_ip(args.name, timeout=30)
    if not ip:
        print(f"Failed to get IP address for VM '{args.name}'.", file=sys.stderr)
        print("Make sure the VM has networking configured and qemu-guest-agent is running.", file=sys.stderr)
        sys.exit(1)

    # Build SSH command
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR"
    ]

    if args.ssh_command:
        # Execute command via SSH
        ssh_cmd = ["ssh"] + ssh_opts + [f"nox@{ip}"] + args.ssh_command
        result = subprocess.run(ssh_cmd)
        sys.exit(result.returncode)
    else:
        # Interactive SSH session
        print(f"Connecting to '{args.name}' at {ip}...")
        ssh_cmd = ["ssh"] + ssh_opts + [f"nox@{ip}"]
        os.execvp("ssh", ssh_cmd)

def cmd_status(args):
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    result = virsh(f"dominfo {args.name}")
    print(result.stdout)

def cmd_passwd(args):
    """Change password for a VM user via qemu guest agent."""
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    state = vm_state(args.name)
    if state != "running":
        print(f"VM '{args.name}' is not running. Start it with: nox start {args.name}", file=sys.stderr)
        sys.exit(1)

    print(f"Generating new password for VM '{args.name}'...")
    new_password = generate_password()

    # Change password via qemu guest agent (works without network)
    pw_b64 = base64.b64encode(f"nox:{new_password}".encode()).decode()
    cmd_str = f"echo $(echo {pw_b64} | base64 -d) | chpasswd"
    ga_cmd = json.dumps({
        "execute": "guest-exec",
        "arguments": {
            "path": "/bin/bash",
            "arg": ["-c", cmd_str],
            "capture-output": True
        }
    })

    try:
        result = virsh(f"qemu-agent-command {args.name} '{ga_cmd}'")
        data = json.loads(result.stdout)
        pid = data.get("return", {}).get("pid")

        if pid is None:
            raise RuntimeError("guest-exec returned no pid")

        # Wait for command to finish
        for _ in range(10):
            time.sleep(1)
            status_cmd = json.dumps({"execute": "guest-exec-status", "arguments": {"pid": pid}})
            res2 = virsh(f"qemu-agent-command {args.name} '{status_cmd}'")
            status = json.loads(res2.stdout).get("return", {})
            if status.get("exited"):
                if status.get("exitcode", 0) != 0:
                    err = base64.b64decode(status.get("err-data", "")).decode()
                    raise RuntimeError(f"chpasswd failed: {err}")
                break

        print(f"\n{'='*60}")
        print(f"Password changed for VM '{args.name}'!")
        print(f"{'='*60}")
        print(f"\nNew password (shown once):")
        print(f"  Password: {new_password}")
        print(f"\nConnect: nox ssh {args.name}")
        print(f"{'='*60}")

    except RuntimeError as e:
        print(f"Failed to change password: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_resize(args):
    """Resize VM resources (CPUs, RAM, or disk)."""
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    state = vm_state(args.name)
    meta = load_meta(args.name)
    if not meta:
        print(f"Could not load metadata for VM '{args.name}'", file=sys.stderr)
        sys.exit(1)

    vm_path = vm_dir(args.name)
    disk_path = os.path.join(vm_path, f"{args.name}.qcow2")

    needs_shutdown = (args.cpus is not None or args.ram is not None) and state == "running"

    # Shut down once if CPU or RAM changes require it
    if needs_shutdown:
        print("Note: CPU/RAM resize requires VM shutdown. Stopping VM...")
        virsh(f"shutdown {args.name}")
        for _ in range(30):
            if vm_state(args.name) == "shut off":
                break
            time.sleep(1)

    # Handle CPU resize
    if args.cpus is not None:
        vcpus, cpu_fraction = resolve_cpus(args.cpus)
        print(f"Resizing CPUs to {vcpus} vCPU(s) (limit: {cpu_fraction} cores)...")
        virsh(f"setvcpus {args.name} {vcpus} --maximum --config")
        virsh(f"setvcpus {args.name} {vcpus} --config")
        meta["vcpus"] = vcpus
        meta["cpu_fraction"] = cpu_fraction
        print(f"✓ CPUs updated to {vcpus} vCPU(s)")

    # Handle RAM resize
    if args.ram is not None:
        ram_mb = resolve_resource(args.ram, host_ram_mb())
        ram_kb = ram_mb * 1024
        print(f"Resizing RAM to {ram_mb}MB...")
        virsh(f"setmaxmem {args.name} {ram_kb} --config")
        virsh(f"setmem {args.name} {ram_kb} --config")
        meta["ram_mb"] = ram_mb
        print(f"✓ RAM updated to {ram_mb}MB")

    # Handle disk resize
    if args.disk is not None:
        disk_gb = resolve_resource(args.disk, host_disk_gb())
        current_disk = meta.get("disk_gb", 0)

        if disk_gb <= current_disk:
            print(f"Error: New disk size ({disk_gb}GB) must be larger than current size ({current_disk}GB)", file=sys.stderr)
            print("Disk shrinking is not supported.", file=sys.stderr)
            sys.exit(1)

        print(f"Expanding disk from {current_disk}GB to {disk_gb}GB...")
        run(f"qemu-img resize {disk_path} {disk_gb}G")

        if vm_state(args.name) == "running":
            virsh(f"blockresize {args.name} {disk_path} {disk_gb}G")

        meta["disk_gb"] = disk_gb
        print(f"✓ Disk expanded to {disk_gb}GB")
        print("Note: You may need to resize the filesystem inside the VM:")
        print("  sudo growpart /dev/vda 1")
        print("  sudo resize2fs /dev/vda1")

    # Save updated metadata
    save_meta(args.name, meta)

    # Restart once at the end if we shut it down
    if needs_shutdown:
        print("Restarting VM...")
        virsh(f"start {args.name}")

    # Re-apply CPU limits after resize
    if args.cpus is not None:
        apply_cpu_limit(args.name, meta["cpu_fraction"])

    print(f"\n✓ VM '{args.name}' resized successfully!")
    if needs_shutdown:
        print(f"VM state: running")
    elif state == "shut off":
        print(f"VM state: shut off (use 'nox start {args.name}' to start)")


def cmd_purge(args):
    """Remove all VMs, bridge, libvirt networks, and nox data."""
    print("This will destroy ALL VMs, remove the bridge, and delete all nox data.")
    if not args.force:
        try:
            answer = input("Are you sure? [y/N] ")
            if answer.lower() != 'y':
                print("Cancelled.")
                return
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return

    # Destroy and undefine all VMs
    result = virsh("list --all --name", check=False)
    if result.returncode == 0:
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if not name:
                continue
            print(f"Deleting VM '{name}'...")
            if vm_state(name) == "running":
                virsh(f"destroy {name}", check=False)
            virsh(f"undefine {name} --nvram --remove-all-storage", check=False)

    # Remove bridge and libvirt network
    remove_bridge()

    # Remove default NAT network if it exists
    virsh(f"net-destroy default", check=False)

    # Remove nox data directory
    if os.path.exists(NOX_DIR):
        shutil.rmtree(NOX_DIR)
        print(f"✓ Removed {NOX_DIR}")

    print("\n✓ nox purged. All VMs, networks, and data removed.")


def cmd_update(args):
    """Update nox to the latest version from GitHub."""
    import tempfile

    GITHUB_RAW_URL = "https://raw.githubusercontent.com/solosmith/nox/main"

    print(f"Current version: {VERSION}")
    print("Checking for updates from GitHub...")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            timestamp = int(time.time())

            if run("command -v curl >/dev/null 2>&1", check=False).returncode == 0:
                run(f"curl -H 'Cache-Control: no-cache' -fsSL '{GITHUB_RAW_URL}/VERSION?t={timestamp}' -o {tmpdir}/VERSION")
                run(f"curl -H 'Cache-Control: no-cache' -fsSL '{GITHUB_RAW_URL}/nox.py?t={timestamp}' -o {tmpdir}/nox.py")
            elif run("command -v wget >/dev/null 2>&1", check=False).returncode == 0:
                run(f"wget --no-cache -q '{GITHUB_RAW_URL}/VERSION?t={timestamp}' -O {tmpdir}/VERSION")
                run(f"wget --no-cache -q '{GITHUB_RAW_URL}/nox.py?t={timestamp}' -O {tmpdir}/nox.py")
            else:
                print("Error: Neither curl nor wget found.", file=sys.stderr)
                sys.exit(1)

            with open(f"{tmpdir}/VERSION", "r") as f:
                remote_version = f.read().strip()
                print(f"Latest version: {remote_version}")

                if remote_version == VERSION:
                    print("✓ Already up to date!")
                    return

            print("Installing updated version...")
            run(f"sudo cp {tmpdir}/nox.py /usr/local/bin/nox")
            run(f"sudo cp {tmpdir}/VERSION /usr/local/bin/VERSION")
            run("sudo chmod +x /usr/local/bin/nox")

            print(f"✓ nox updated successfully! ({VERSION} → {remote_version})")

        except Exception as e:
            print(f"Error updating nox: {e}", file=sys.stderr)
            sys.exit(1)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="nox", description="Lightweight VM Manager")
    parser.add_argument("--version", action="version", version=f"nox {VERSION}")
    sub = parser.add_subparsers(dest="command")

    # create
    p = sub.add_parser("create", help="Create a new VM")
    p.add_argument("name")
    p.add_argument("--os", choices=["debian"], default=None)
    p.add_argument("--cpus", type=float, default=None)
    p.add_argument("--ram", type=float, default=None)
    p.add_argument("--disk", type=float, default=None)
    p.add_argument("--network", type=str, default=None, help="Libvirt network (default: bridged)")
    p.add_argument("--no-autostart", action="store_true", help="Disable autostart on boot")
    p.add_argument("--no-start", action="store_true", help="Create but don't start VM")

    # start
    p = sub.add_parser("start", help="Start a VM")
    p.add_argument("name")

    # stop
    p = sub.add_parser("stop", help="Stop VM")
    p.add_argument("name")

    # restart
    p = sub.add_parser("restart", help="Restart VM")
    p.add_argument("name")

    # delete
    p = sub.add_parser("delete", aliases=["rm"], help="Delete VM")
    p.add_argument("name")

    # list
    sub.add_parser("list", aliases=["ls"], help="List all VMs")

    # status
    p = sub.add_parser("status", help="Show VM details")
    p.add_argument("name")

    # ssh
    p = sub.add_parser("ssh", help="SSH into VM")
    p.add_argument("name")
    p.add_argument("ssh_command", nargs="*", default=None)

    # passwd
    p = sub.add_parser("passwd", help="Change SSH password for VM")
    p.add_argument("name")

    # resize
    p = sub.add_parser("resize", help="Resize VM resources")
    p.add_argument("name")
    p.add_argument("--cpus", type=float, default=None, help="New CPU count")
    p.add_argument("--ram", type=float, default=None, help="New RAM in MB")
    p.add_argument("--disk", type=float, default=None, help="New disk size in GB (can only expand)")

    # update
    sub.add_parser("update", aliases=["up"], help="Update nox")

    # purge
    p = sub.add_parser("purge", help="Remove ALL VMs, bridge, and nox data")
    p.add_argument("--force", "-f", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "create": cmd_create,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "delete": cmd_delete,
        "rm": cmd_delete,
        "list": cmd_list,
        "ls": cmd_list,
        "status": cmd_status,
        "ssh": cmd_ssh,
        "passwd": cmd_passwd,
        "resize": cmd_resize,
        "update": cmd_update,
        "up": cmd_update,
        "purge": cmd_purge,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
