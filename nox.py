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
import tempfile
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
BACKUPS_DIR = os.path.join(NOX_DIR, "backups")
CONFIG_FILE = os.path.join(NOX_DIR, "config.json")

DEFAULT_CONFIG = {
    "defaults": {"os": "debian", "cpus": 1, "ram": 512, "disk": 5},
    "env": {},
    "s3": {
        "enabled": False,
        "endpoint": "",
        "bucket": "",
        "access_key": "",
        "secret_key": "",
        "region": "us-east-1"
    }
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
    os.makedirs(BACKUPS_DIR, exist_ok=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def host_arch():
    m = os.uname().machine
    if m in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"

def host_cpus():
    return os.cpu_count() or 1

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
    """If value <= 1.0, treat as fraction of host_total. Otherwise absolute."""
    v = float(value)
    if v <= 1.0:
        return max(1, int(math.ceil(v * host_total)))
    return int(v)

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
            stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode('utf-8')
            for line in stdout.splitlines():
                if "ipv4" in line.lower() and "127.0.0.1" not in line:
                    parts = line.split()
                    for part in parts:
                        if "/" in part and not part.startswith("127."):
                            return part.split("/")[0]

        time.sleep(2)

    return None

def generate_password():
    """Generate random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))

def list_networks():
    """List available libvirt networks."""
    try:
        result = virsh("net-list --all", check=False)
        if result.returncode != 0:
            return []
        
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode('utf-8')
        
        networks = []
        for line in output.splitlines()[2:]:  # Skip header lines
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                state = parts[1]
                networks.append({'name': name, 'state': state, 'type': 'libvirt'})
        return networks
    except Exception as e:
        print(f"Warning: Failed to list networks: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# S3 Helper Functions
# ---------------------------------------------------------------------------

def upload_to_s3(backup_path, backup_name, s3_config):
    """Upload backup to S3-compatible storage."""
    try:
        print(f"\nUploading backup to S3...")
        
        endpoint = s3_config.get("endpoint")
        bucket = s3_config.get("bucket")
        access_key = s3_config.get("access_key")
        secret_key = s3_config.get("secret_key")
        region = s3_config.get("region", "us-east-1")
        
        # Create tarball of backup
        import tarfile
        tarball_path = f"{backup_path}.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(backup_path, arcname=backup_name)
        
        # Upload using AWS CLI or boto3
        s3_path = f"s3://{bucket}/nox-backups/{backup_name}.tar.gz"
        
        # Try using aws cli first
        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = access_key
        env["AWS_SECRET_ACCESS_KEY"] = secret_key
        env["AWS_DEFAULT_REGION"] = region
        
        if endpoint:
            env["AWS_ENDPOINT_URL"] = endpoint
            cmd = f"aws s3 cp {tarball_path} {s3_path} --endpoint-url {endpoint}"
        else:
            cmd = f"aws s3 cp {tarball_path} {s3_path}"
        
        result = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
        
        # Clean up tarball
        os.remove(tarball_path)
        
        if result.returncode == 0:
            print(f"✓ Backup uploaded to S3: {s3_path}")
        else:
            print(f"Warning: Failed to upload to S3: {result.stderr}", file=sys.stderr)
            
    except Exception as e:
        print(f"Warning: S3 upload failed: {e}", file=sys.stderr)

def list_s3_backups(s3_config):
    """List backups from S3."""
    try:
        endpoint = s3_config.get("endpoint")
        bucket = s3_config.get("bucket")
        access_key = s3_config.get("access_key")
        secret_key = s3_config.get("secret_key")
        region = s3_config.get("region", "us-east-1")
        
        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = access_key
        env["AWS_SECRET_ACCESS_KEY"] = secret_key
        env["AWS_DEFAULT_REGION"] = region
        
        s3_path = f"s3://{bucket}/nox-backups/"
        
        if endpoint:
            env["AWS_ENDPOINT_URL"] = endpoint
            cmd = f"aws s3 ls {s3_path} --endpoint-url {endpoint}"
        else:
            cmd = f"aws s3 ls {s3_path}"
        
        result = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
        
        if result.returncode == 0:
            backups = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[-1].endswith('.tar.gz'):
                    backup_name = parts[-1].replace('.tar.gz', '')
                    date_str = f"{parts[0]} {parts[1]}"
                    size = parts[2]
                    backups.append({
                        'name': backup_name,
                        'date': date_str,
                        'size': size,
                        'source': 's3'
                    })
            return backups
        else:
            return []
            
    except Exception as e:
        print(f"Warning: Failed to list S3 backups: {e}", file=sys.stderr)
        return []

def download_from_s3(backup_name, s3_config):
    """Download backup from S3."""
    try:
        print(f"Downloading backup from S3...")
        
        endpoint = s3_config.get("endpoint")
        bucket = s3_config.get("bucket")
        access_key = s3_config.get("access_key")
        secret_key = s3_config.get("secret_key")
        region = s3_config.get("region", "us-east-1")
        
        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = access_key
        env["AWS_SECRET_ACCESS_KEY"] = secret_key
        env["AWS_DEFAULT_REGION"] = region
        
        s3_path = f"s3://{bucket}/nox-backups/{backup_name}.tar.gz"
        tarball_path = os.path.join(BACKUPS_DIR, f"{backup_name}.tar.gz")
        
        if endpoint:
            env["AWS_ENDPOINT_URL"] = endpoint
            cmd = f"aws s3 cp {s3_path} {tarball_path} --endpoint-url {endpoint}"
        else:
            cmd = f"aws s3 cp {s3_path} {tarball_path}"
        
        result = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
        
        if result.returncode == 0:
            # Extract tarball
            import tarfile
            with tarfile.open(tarball_path, "r:gz") as tar:
                tar.extractall(BACKUPS_DIR)
            
            # Clean up tarball
            os.remove(tarball_path)
            
            print(f"✓ Backup downloaded from S3")
            return True
        else:
            print(f"Error: Failed to download from S3: {result.stderr}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"Error: S3 download failed: {e}", file=sys.stderr)
        return False

def interactive_backup_selection(backups):
    """Interactive backup selection using arrow keys."""
    if not backups:
        print("No backups available.")
        return None
    
    try:
        import curses
        
        def select_backup(stdscr):
            curses.curs_set(0)
            current_idx = 0
            
            while True:
                stdscr.clear()
                h, w = stdscr.getmaxyx()
                
                stdscr.addstr(0, 0, "Select a backup to restore (↑/↓ to navigate, Enter to select, q to quit):", curses.A_BOLD)
                stdscr.addstr(1, 0, "-" * min(w-1, 80))
                
                for idx, backup in enumerate(backups):
                    y = idx + 3
                    if y >= h - 1:
                        break
                    
                    source_tag = "[S3]" if backup.get('source') == 's3' else "[Local]"
                    line = f"{source_tag} {backup['name']} - {backup.get('date', 'N/A')}"
                    
                    if idx == current_idx:
                        stdscr.addstr(y, 0, f"> {line}", curses.A_REVERSE)
                    else:
                        stdscr.addstr(y, 0, f"  {line}")
                
                stdscr.refresh()
                
                key = stdscr.getch()
                
                if key == curses.KEY_UP and current_idx > 0:
                    current_idx -= 1
                elif key == curses.KEY_DOWN and current_idx < len(backups) - 1:
                    current_idx += 1
                elif key == ord('\n'):
                    return backups[current_idx]
                elif key == ord('q') or key == ord('Q'):
                    return None
        
        return curses.wrapper(select_backup)
        
    except ImportError:
        # Fallback to simple numbered selection if curses not available
        print("\nAvailable backups:")
        for idx, backup in enumerate(backups):
            source_tag = "[S3]" if backup.get('source') == 's3' else "[Local]"
            print(f"{idx + 1}. {source_tag} {backup['name']} - {backup.get('date', 'N/A')}")
        
        try:
            choice = int(input("\nEnter backup number (0 to cancel): "))
            if choice > 0 and choice <= len(backups):
                return backups[choice - 1]
        except (ValueError, KeyboardInterrupt):
            pass
        
        return None

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
    vcpus = resolve_resource(cpus, host_cpus())
    ram_mb = resolve_resource(ram, host_ram_mb())
    disk_gb = resolve_resource(disk, host_disk_gb())

    if password is None:
        password = generate_password()

    # Use default libvirt network
    if network is None:
        network = 'default'

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

    # Save metadata
    meta = {
        "name": name,
        "os": os_name,
        "arch": arch,
        "vcpus": vcpus,
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
    # Use default network or network from args
    network = getattr(args, "network", None) or 'default'

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
    import json, base64
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
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode()
        data = json.loads(stdout)
        pid = data.get("return", {}).get("pid")

        if pid is None:
            raise RuntimeError("guest-exec returned no pid")

        # Wait for command to finish
        import time
        for _ in range(10):
            time.sleep(1)
            status_cmd = json.dumps({"execute": "guest-exec-status", "arguments": {"pid": pid}})
            res2 = virsh(f"qemu-agent-command {args.name} '{status_cmd}'")
            out2 = res2.stdout if isinstance(res2.stdout, str) else res2.stdout.decode()
            status = json.loads(out2).get("return", {})
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

    # Handle CPU resize
    if args.cpus is not None:
        vcpus = resolve_resource(args.cpus, host_cpus())
        print(f"Resizing CPUs to {vcpus}...")
        
        # Set maximum vCPUs (requires VM to be shut off)
        if state == "running":
            print("Note: Setting maximum vCPUs requires VM shutdown. Stopping VM...")
            virsh(f"shutdown {args.name}")
            # Wait for shutdown
            for _ in range(30):
                if vm_state(args.name) == "shut off":
                    break
                time.sleep(1)
        
        virsh(f"setvcpus {args.name} {vcpus} --maximum --config")
        virsh(f"setvcpus {args.name} {vcpus} --config")
        meta["vcpus"] = vcpus
        print(f"✓ CPUs updated to {vcpus}")
        
        if state == "running":
            print("Restarting VM...")
            virsh(f"start {args.name}")

    # Handle RAM resize
    if args.ram is not None:
        ram_mb = resolve_resource(args.ram, host_ram_mb())
        ram_kb = ram_mb * 1024
        print(f"Resizing RAM to {ram_mb}MB...")
        
        if state == "running":
            print("Note: RAM resize requires VM shutdown. Stopping VM...")
            virsh(f"shutdown {args.name}")
            # Wait for shutdown
            for _ in range(30):
                if vm_state(args.name) == "shut off":
                    break
                time.sleep(1)
        
        virsh(f"setmaxmem {args.name} {ram_kb} --config")
        virsh(f"setmem {args.name} {ram_kb} --config")
        meta["ram_mb"] = ram_mb
        print(f"✓ RAM updated to {ram_mb}MB")
        
        if state == "running":
            print("Restarting VM...")
            virsh(f"start {args.name}")

    # Handle disk resize
    if args.disk is not None:
        disk_gb = resolve_resource(args.disk, host_disk_gb())
        current_disk = meta.get("disk_gb", 0)
        
        if disk_gb <= current_disk:
            print(f"Error: New disk size ({disk_gb}GB) must be larger than current size ({current_disk}GB)", file=sys.stderr)
            print("Disk shrinking is not supported.", file=sys.stderr)
            sys.exit(1)
        
        print(f"Expanding disk from {current_disk}GB to {disk_gb}GB...")
        
        # Resize the qcow2 image
        run(f"qemu-img resize {disk_path} {disk_gb}G")
        
        # If VM is running, use virsh blockresize
        if state == "running":
            virsh(f"blockresize {args.name} {disk_path} {disk_gb}G")
        
        meta["disk_gb"] = disk_gb
        print(f"✓ Disk expanded to {disk_gb}GB")
        print("Note: You may need to resize the filesystem inside the VM:")
        print("  sudo growpart /dev/vda 1")
        print("  sudo resize2fs /dev/vda1")

    # Save updated metadata
    save_meta(args.name, meta)
    
    print(f"\n✓ VM '{args.name}' resized successfully!")
    if state == "running" and (args.cpus is not None or args.ram is not None):
        print(f"VM state: running")
    elif state == "shut off":
        print(f"VM state: shut off (use 'nox start {args.name}' to start)")


def cmd_backup(args):
    """Backup a VM using live snapshot (no downtime)."""
    if not vm_exists(args.name):
        print(f"VM '{args.name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    state = vm_state(args.name)
    was_running = state == "running"
    
    # Create backup directory
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"{args.name}_{timestamp}"
    backup_path = os.path.join(BACKUPS_DIR, backup_name)
    os.makedirs(backup_path, exist_ok=True)

    print(f"Creating live backup '{backup_name}'...")

    vm_path = vm_dir(args.name)
    disk_path = os.path.join(vm_path, f"{args.name}.qcow2")
    snapshot_disk = os.path.join(vm_path, f"{args.name}_snapshot.qcow2")

    try:
        # Create external snapshot if VM is running (live backup)
        if was_running:
            print("Creating live snapshot (VM continues running)...")
            # Create external snapshot - VM writes to new file, original becomes read-only
            virsh(f"snapshot-create-as {args.name} backup_snapshot --disk-only --atomic --no-metadata")
            # Now the original disk is frozen and can be safely backed up
            time.sleep(1)  # Brief pause to ensure snapshot is ready

        # Backup disk image with compression
        print("Backing up disk image (compressed)...")
        backup_disk = os.path.join(backup_path, f"{args.name}.qcow2")
        run(f"qemu-img convert -O qcow2 -c {disk_path} {backup_disk}")

        # If we created a snapshot, merge it back
        if was_running:
            print("Merging snapshot back...")
            # Commit changes from snapshot back to original
            virsh(f"blockcommit {args.name} vda --active --pivot")
            # Clean up snapshot file
            if os.path.exists(snapshot_disk):
                os.remove(snapshot_disk)

        # Backup metadata
        meta = load_meta(args.name)
        if meta:
            backup_meta = os.path.join(backup_path, "meta.json")
            with open(backup_meta, "w") as f:
                json.dump(meta, f, indent=2)

        # Backup cloud-init files if they exist
        for filename in ["user-data", "meta-data", "cloud-init.iso"]:
            src = os.path.join(vm_path, filename)
            if os.path.exists(src):
                dst = os.path.join(backup_path, filename)
                shutil.copy2(src, dst)

        # Get VM XML definition
        result = virsh(f"dumpxml {args.name}")
        xml_path = os.path.join(backup_path, "domain.xml")
        xml_content = result.stdout if isinstance(result.stdout, str) else result.stdout.decode('utf-8')
        with open(xml_path, "w") as f:
            f.write(xml_content)

        # Create backup info file
        backup_info = {
            "vm_name": args.name,
            "backup_name": backup_name,
            "timestamp": timestamp,
            "was_running": was_running,
            "metadata": meta,
        }
        info_path = os.path.join(backup_path, "backup_info.json")
        with open(info_path, "w") as f:
            json.dump(backup_info, f, indent=2)

        print(f"✓ Backup created successfully: {backup_name}")
        print(f"  Location: {backup_path}")
        if was_running:
            print(f"  VM '{args.name}' remained running during backup")

        # Upload to S3 if configured
        cfg = load_config()
        s3_config = cfg.get("s3", {})
        if s3_config.get("enabled"):
            upload_to_s3(backup_path, backup_name, s3_config)

    except Exception as e:
        print(f"Error creating backup: {e}", file=sys.stderr)
        # Try to clean up snapshot if it exists
        if was_running:
            try:
                virsh(f"blockcommit {args.name} vda --active --pivot", check=False)
                if os.path.exists(snapshot_disk):
                    os.remove(snapshot_disk)
            except:
                pass
        shutil.rmtree(backup_path, ignore_errors=True)
        sys.exit(1)

def cmd_restore(args):
    """Restore a VM from backup with interactive selection."""
    cfg = load_config()
    s3_config = cfg.get("s3", {})
    
    # If no backup name provided, show interactive selection
    if not args.backup_name:
        # Collect local backups
        local_backups = []
        if os.path.exists(BACKUPS_DIR):
            for backup_name in os.listdir(BACKUPS_DIR):
                backup_path = os.path.join(BACKUPS_DIR, backup_name)
                if not os.path.isdir(backup_path):
                    continue
                
                info_path = os.path.join(backup_path, "backup_info.json")
                if os.path.exists(info_path):
                    with open(info_path) as f:
                        info = json.load(f)
                    local_backups.append({
                        'name': backup_name,
                        'date': info.get('timestamp', 'N/A'),
                        'source': 'local'
                    })
        
        # Collect S3 backups if enabled
        s3_backups = []
        if s3_config.get("enabled"):
            s3_backups = list_s3_backups(s3_config)
        
        # Combine all backups
        all_backups = local_backups + s3_backups
        
        if not all_backups:
            print("No backups available.")
            sys.exit(1)
        
        # Interactive selection
        selected = interactive_backup_selection(all_backups)
        
        if not selected:
            print("Restore cancelled.")
            sys.exit(0)
        
        backup_name = selected['name']
        
        # Download from S3 if needed
        if selected.get('source') == 's3':
            backup_path = os.path.join(BACKUPS_DIR, backup_name)
            if not os.path.exists(backup_path):
                if not download_from_s3(backup_name, s3_config):
                    sys.exit(1)
    else:
        backup_name = args.backup_name
    
    backup_path = os.path.join(BACKUPS_DIR, backup_name)
    
    if not os.path.exists(backup_path):
        print(f"Backup '{backup_name}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Load backup info
    info_path = os.path.join(backup_path, "backup_info.json")
    if not os.path.exists(info_path):
        print(f"Invalid backup: missing backup_info.json", file=sys.stderr)
        sys.exit(1)

    with open(info_path) as f:
        backup_info = json.load(f)

    original_name = backup_info["vm_name"]
    restore_name = args.name if args.name else original_name

    # Check if VM already exists
    if vm_exists(restore_name):
        if not args.force:
            print(f"VM '{restore_name}' already exists. Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)
        
        print(f"Deleting existing VM '{restore_name}'...")
        state = vm_state(restore_name)
        if state == "running":
            virsh(f"destroy {restore_name}")
        virsh(f"undefine {restore_name} --nvram --remove-all-storage", check=False)

    print(f"Restoring VM '{restore_name}' from backup '{backup_name}'...")

    # Create VM directory
    vm_path = vm_dir(restore_name)
    os.makedirs(vm_path, exist_ok=True)

    try:
        # Restore disk image
        print("Restoring disk image...")
        backup_disk = os.path.join(backup_path, f"{original_name}.qcow2")
        restore_disk = os.path.join(vm_path, f"{restore_name}.qcow2")
        run(f"qemu-img convert -O qcow2 {backup_disk} {restore_disk}")

        # Restore metadata
        backup_meta = os.path.join(backup_path, "meta.json")
        if os.path.exists(backup_meta):
            with open(backup_meta) as f:
                meta = json.load(f)
            meta["name"] = restore_name
            save_meta(restore_name, meta)

        # Restore cloud-init files
        for filename in ["user-data", "meta-data", "cloud-init.iso"]:
            src = os.path.join(backup_path, filename)
            if os.path.exists(src):
                dst = os.path.join(vm_path, filename)
                shutil.copy2(src, dst)

        # Restore VM from XML
        xml_path = os.path.join(backup_path, "domain.xml")
        if os.path.exists(xml_path):
            # Read and modify XML to update VM name and paths
            with open(xml_path) as f:
                xml_content = f.read()
            
            # Replace VM name and disk paths
            xml_content = xml_content.replace(f"<name>{original_name}</name>", f"<name>{restore_name}</name>")
            xml_content = xml_content.replace(f"{original_name}.qcow2", f"{restore_name}.qcow2")
            
            # Write modified XML to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as tmp:
                tmp.write(xml_content)
                tmp_xml = tmp.name
            
            try:
                virsh(f"define {tmp_xml}")
            finally:
                os.unlink(tmp_xml)

        print(f"✓ VM '{restore_name}' restored successfully!")
        
        if backup_info.get("was_running") and not args.no_start:
            print(f"Starting VM '{restore_name}'...")
            virsh(f"start {restore_name}")
        else:
            print(f"VM '{restore_name}' is ready. Use 'nox start {restore_name}' to start it.")

    except Exception as e:
        print(f"Error restoring backup: {e}", file=sys.stderr)
        shutil.rmtree(vm_path, ignore_errors=True)
        sys.exit(1)

def cmd_list_backups(args):
    """List all backups from local and S3."""
    cfg = load_config()
    s3_config = cfg.get("s3", {})
    
    # Collect local backups
    local_backups = []
    if os.path.exists(BACKUPS_DIR):
        for backup_name in os.listdir(BACKUPS_DIR):
            backup_path = os.path.join(BACKUPS_DIR, backup_name)
            if not os.path.isdir(backup_path):
                continue

            info_path = os.path.join(backup_path, "backup_info.json")
            if os.path.exists(info_path):
                with open(info_path) as f:
                    info = json.load(f)
                
                # Calculate backup size
                total_size = 0
                for root, dirs, files in os.walk(backup_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp):
                            total_size += os.path.getsize(fp)
                
                size_gb = total_size / (1024 ** 3)
                
                local_backups.append({
                    'name': backup_name,
                    'vm_name': info.get("vm_name", "?"),
                    'timestamp': info.get("timestamp", "?"),
                    'size': f"{size_gb:.2f}GB",
                    'source': 'Local'
                })
    
    # Collect S3 backups if enabled
    s3_backups = []
    if s3_config.get("enabled"):
        print("Fetching S3 backups...")
        s3_list = list_s3_backups(s3_config)
        for backup in s3_list:
            # Parse VM name from backup name (format: vmname_timestamp)
            parts = backup['name'].rsplit('_', 2)
            vm_name = parts[0] if len(parts) >= 3 else "?"
            
            s3_backups.append({
                'name': backup['name'],
                'vm_name': vm_name,
                'timestamp': backup.get('date', '?'),
                'size': backup.get('size', '?'),
                'source': 'S3'
            })
    
    all_backups = local_backups + s3_backups
    
    if not all_backups:
        print("No backups found.")
        return

    print(f"{'SOURCE':<8} {'BACKUP NAME':<40} {'VM NAME':<20} {'DATE':<20} {'SIZE'}")
    print("-" * 105)

    for backup in sorted(all_backups, key=lambda x: x.get("timestamp", ""), reverse=True):
        source = backup['source']
        name = backup['name']
        vm_name = backup['vm_name']
        timestamp = backup['timestamp']
        size = backup['size']
        
        # Format timestamp if needed
        if timestamp != "?" and len(timestamp) == 15:  # Format: YYYYMMDD_HHMMSS
            try:
                dt = time.strptime(timestamp, "%Y%m%d_%H%M%S")
                date_str = time.strftime("%Y-%m-%d %H:%M:%S", dt)
            except:
                date_str = timestamp
        else:
            date_str = timestamp

        print(f"{source:<8} {name:<40} {vm_name:<20} {date_str:<20} {size}")

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
    p.add_argument("--network", type=str, default=None, help="Libvirt network to use (default: 'default')")
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

    # backup
    p = sub.add_parser("backup", help="Backup a VM")
    p.add_argument("name")

    # restore
    p = sub.add_parser("restore", help="Restore a VM from backup (interactive if no backup specified)")
    p.add_argument("backup_name", nargs="?", default=None, help="Name of the backup to restore (optional - will show interactive selection)")
    p.add_argument("--name", default=None, help="New name for restored VM (default: original name)")
    p.add_argument("--force", action="store_true", help="Overwrite existing VM")
    p.add_argument("--no-start", action="store_true", help="Don't start VM after restore")

    # backups
    sub.add_parser("backups", help="List all backups")

    # update
    sub.add_parser("update", aliases=["up"], help="Update nox")

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
        "backup": cmd_backup,
        "restore": cmd_restore,
        "backups": cmd_list_backups,
        "update": cmd_update,
        "up": cmd_update,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
