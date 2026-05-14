#!/usr/bin/env bash
# nox installer - Complete installation script for KVM/libvirt
# Includes comprehensive system verification for all nox functionality
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[nox]${NC} $*"; }
warn()  { echo -e "${YELLOW}[nox]${NC} $*"; }
error() { echo -e "${RED}[nox]${NC} $*" >&2; }
section() { echo -e "\n${BLUE}[nox]${NC} ── $* ──"; }

ERRORS=0
WARNINGS=0

check_pass() { info "  ✓ $*"; }
check_fail() { error "  ✗ $*"; ERRORS=$((ERRORS + 1)); }
check_warn() { warn "  ⚠ $*"; WARNINGS=$((WARNINGS + 1)); }

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

if [[ "$OSTYPE" == "darwin"* ]]; then
    error "macOS is not supported. KVM requires a Linux kernel."
    exit 1
fi

if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
else
    SUDO=""
fi

GITHUB_REPO="solosmith/nox"
GITHUB_RAW_URL="https://raw.githubusercontent.com/$GITHUB_REPO/main"
GITHUB_RELEASE_URL="https://github.com/$GITHUB_REPO/releases/download/latest"
CURRENT_USER=$(whoami)
HOME_DIR=$(eval echo "~$CURRENT_USER")

info "nox - Lightweight VM Manager using KVM/libvirt"
info "========================================"

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    elif [ -f /etc/redhat-release ]; then
        echo "rhel"
    else
        echo "unknown"
    fi
}

detect_arch() {
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64|amd64) echo "amd64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) echo "$arch" ;;
    esac
}

# ---------------------------------------------------------------------------
# Existing installation check
# ---------------------------------------------------------------------------

check_existing_installation() {
    if command -v nox >/dev/null 2>&1; then
        CURRENT_VERSION="unknown"
        if [ -f /usr/local/lib/nox/VERSION ]; then
            CURRENT_VERSION=$(cat /usr/local/lib/nox/VERSION)
        elif [ -f /usr/local/bin/VERSION ]; then
            CURRENT_VERSION=$(cat /usr/local/bin/VERSION)
        fi
        info "Found existing nox installation (version: $CURRENT_VERSION)"

        info "Checking for updates..."
        TEMP_DIR=$(mktemp -d)
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$GITHUB_RAW_URL/VERSION" -o "$TEMP_DIR/VERSION" 2>/dev/null || true
        elif command -v wget >/dev/null 2>&1; then
            wget -q "$GITHUB_RAW_URL/VERSION" -O "$TEMP_DIR/VERSION" 2>/dev/null || true
        fi

        if [ -f "$TEMP_DIR/VERSION" ]; then
            LATEST_VERSION=$(cat "$TEMP_DIR/VERSION")
            if [ "$CURRENT_VERSION" = "$LATEST_VERSION" ]; then
                info "Already up to date (version $CURRENT_VERSION)"
                rm -rf "$TEMP_DIR"
                return 0
            else
                info "Update available: $CURRENT_VERSION → $LATEST_VERSION"
                rm -rf "$TEMP_DIR"
                return 1
            fi
        fi
        rm -rf "$TEMP_DIR"
        return 1
    fi
    return 1
}

SKIP_DEPS=false
if check_existing_installation; then
    read -p "Verify and update dependencies if needed? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        SKIP_DEPS=true
    fi
else
    info "Will install/update nox and verify all dependencies"
fi

# ---------------------------------------------------------------------------
# Package installation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Package installation (system dependencies)
# ---------------------------------------------------------------------------

install_debian() {
    info "Installing/updating dependencies (Debian/Ubuntu)..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq \
        qemu-system \
        qemu-utils \
        libvirt-daemon-system \
        libvirt-clients \
        virtinst \
        bridge-utils \
        genisoimage \
        openssh-client \
        dnsmasq-base \
        iptables-persistent \
        unzip \
        curl

    $SUDO systemctl enable libvirtd
    $SUDO systemctl start libvirtd
}

install_alpine() {
    info "Installing/updating dependencies (Alpine)..."
    $SUDO apk add \
        qemu-system-x86_64 \
        qemu-system-aarch64 \
        qemu-img \
        libvirt-daemon \
        libvirt-client \
        virt-install \
        bridge-utils \
        cdrkit \
        openssh-client \
        dnsmasq \
        unzip \
        curl

    $SUDO rc-update add libvirtd boot || true
    $SUDO service libvirtd start || true
}

# ---------------------------------------------------------------------------
# KVM setup
# ---------------------------------------------------------------------------

enable_kvm() {
    local arch=$(detect_arch)

    if [ "$arch" = "amd64" ]; then
        if grep -q "vmx" /proc/cpuinfo; then
            $SUDO modprobe kvm_intel 2>/dev/null || $SUDO modprobe kvm 2>/dev/null || true
        elif grep -q "svm" /proc/cpuinfo; then
            $SUDO modprobe kvm_amd 2>/dev/null || $SUDO modprobe kvm 2>/dev/null || true
        fi
    elif [ "$arch" = "arm64" ]; then
        $SUDO modprobe kvm 2>/dev/null || true
    fi

    if [ -c /dev/kvm ]; then
        $SUDO chown root:kvm /dev/kvm 2>/dev/null || true
        $SUDO chmod 660 /dev/kvm 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Network setup
# ---------------------------------------------------------------------------

configure_libvirt_network() {
    info "Configuring libvirt NAT network..."

    # Enable IP forwarding
    $SUDO sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
    if ! grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" | $SUDO tee -a /etc/sysctl.conf >/dev/null
    fi

    # Remove old bridge-mode nox-net if present
    if $SUDO virsh --connect qemu:///system net-list --all 2>/dev/null | grep -q "nox-net"; then
        info "Removing old nox-net (bridge mode)..."
        $SUDO virsh --connect qemu:///system net-destroy nox-net 2>/dev/null || true
        $SUDO virsh --connect qemu:///system net-undefine nox-net 2>/dev/null || true
    fi

    # Check if nox-nat already exists and is correct
    if $SUDO virsh --connect qemu:///system net-list --all 2>/dev/null | grep -q "nox-nat"; then
        if $SUDO virsh --connect qemu:///system net-dumpxml nox-nat 2>/dev/null | grep -q "mode='nat'"; then
            info "✓ nox-nat already configured"
            $SUDO virsh --connect qemu:///system net-start nox-nat 2>/dev/null || true
            $SUDO virsh --connect qemu:///system net-autostart nox-nat 2>/dev/null || true
            return
        fi
        $SUDO virsh --connect qemu:///system net-destroy nox-nat 2>/dev/null || true
        $SUDO virsh --connect qemu:///system net-undefine nox-nat 2>/dev/null || true
    fi

    info "Creating nox-nat (NAT, 192.168.100.0/24)..."
    $SUDO virsh --connect qemu:///system net-define /dev/stdin <<'EOF'
<network>
  <name>nox-nat</name>
  <forward mode='nat'/>
  <bridge name='virbr-nox' stp='on' delay='0'/>
  <ip address='192.168.100.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.100.10' end='192.168.100.254'/>
    </dhcp>
  </ip>
</network>
EOF
    $SUDO virsh --connect qemu:///system net-start nox-nat 2>/dev/null || true
    $SUDO virsh --connect qemu:///system net-autostart nox-nat 2>/dev/null || true
    info "✓ nox-nat created (NAT — VMs get 192.168.100.x IPs, isolated from LAN)"
}

# ---------------------------------------------------------------------------
# Permissions setup
# ---------------------------------------------------------------------------

setup_permissions() {
    info "Configuring user permissions..."

    for group in libvirt libvirt-qemu kvm; do
        if getent group "$group" >/dev/null 2>&1; then
            $SUDO usermod -aG "$group" "$CURRENT_USER" 2>/dev/null || true
        fi
    done

    # Home dir must be traversable (o+x) for libvirt-qemu to access ~/.nox/vms/
    $SUDO chmod 711 "$HOME_DIR" 2>/dev/null || true

    if [ -d /etc/polkit-1/rules.d ]; then
        $SUDO tee /etc/polkit-1/rules.d/50-libvirt.rules > /dev/null <<EOF
polkit.addRule(function(action, subject) {
    if (action.id == "org.libvirt.unix.manage" &&
        subject.isInGroup("libvirt")) {
            return polkit.Result.YES;
    }
});
EOF
    fi
}

# ===========================================================================
# COMPREHENSIVE VERIFICATION
# Checks every dependency, permission, service, and capability that nox needs
# ===========================================================================

run_full_verification() {
    ERRORS=0
    WARNINGS=0

    # Helper: try sudo -n (non-interactive) first, fall back to no sudo
    _virsh() {
        $SUDO -n virsh --connect qemu:///system "$@" 2>/dev/null || virsh --connect qemu:///system "$@" 2>/dev/null
    }

    # -----------------------------------------------------------------------
    section "1. Core Binaries"
    # -----------------------------------------------------------------------

    # bash (used by run() helper to execute shell commands)
    if command -v bash >/dev/null 2>&1; then
        check_pass "bash: $(bash --version | head -1 | sed 's/GNU bash, version //')"
    else
        check_fail "bash: NOT FOUND — required for command execution"
    fi

    # curl (image downloads, update checks)
    if command -v curl >/dev/null 2>&1; then
        check_pass "curl: $(command -v curl)"
    else
        check_fail "curl: NOT FOUND — required for downloading OS images and updates"
    fi

    # -----------------------------------------------------------------------
    section "2. Virtualization Binaries"
    # -----------------------------------------------------------------------

    # virsh (all VM lifecycle operations)
    if command -v virsh >/dev/null 2>&1; then
        local virsh_ver=$(virsh --version 2>&1)
        check_pass "virsh: $virsh_ver ($(command -v virsh))"
    else
        check_fail "virsh: NOT FOUND — required for all VM operations"
    fi

    # qemu-img (disk creation, resize)
    if command -v qemu-img >/dev/null 2>&1; then
        local qimg_ver=$(qemu-img --version 2>&1 | head -1)
        check_pass "qemu-img: $qimg_ver"
    else
        check_fail "qemu-img: NOT FOUND — required for disk creation and resize"
    fi

    # virt-install (VM creation)
    if command -v virt-install >/dev/null 2>&1; then
        local vi_ver=$(virt-install --version 2>&1)
        check_pass "virt-install: $vi_ver ($(command -v virt-install))"
    else
        check_fail "virt-install: NOT FOUND — required for VM creation"
    fi

    # genisoimage (cloud-init ISO generation)
    if command -v genisoimage >/dev/null 2>&1; then
        check_pass "genisoimage: $(command -v genisoimage)"
    else
        check_fail "genisoimage: NOT FOUND — required for cloud-init ISO creation"
    fi

    # -----------------------------------------------------------------------
    section "3. SSH Binaries"
    # -----------------------------------------------------------------------

    # ssh client (nox ssh command)
    if command -v ssh >/dev/null 2>&1; then
        local ssh_ver=$(ssh -V 2>&1)
        check_pass "ssh: $ssh_ver"
    else
        check_fail "ssh: NOT FOUND — required for 'nox ssh' command"
    fi

    # ssh-keygen (auto key generation)
    if command -v ssh-keygen >/dev/null 2>&1; then
        check_pass "ssh-keygen: $(command -v ssh-keygen)"
    else
        check_fail "ssh-keygen: NOT FOUND — required for SSH key generation"
    fi

    # base64 (used inside VMs for passwd command via guest agent)
    if command -v base64 >/dev/null 2>&1; then
        check_pass "base64: $(command -v base64)"
    else
        check_warn "base64: NOT FOUND — 'nox passwd' may not work"
    fi

    # -----------------------------------------------------------------------
    section "4. System Utilities"
    # -----------------------------------------------------------------------

    # df (disk space detection in hostDiskGb)
    if command -v df >/dev/null 2>&1; then
        check_pass "df: $(command -v df)"
    else
        check_warn "df: NOT FOUND — disk space detection will use fallback"
    fi

    # hostname (SSH key comment generation)
    if command -v hostname >/dev/null 2>&1; then
        check_pass "hostname: $(hostname)"
    else
        check_warn "hostname: NOT FOUND — SSH key comments will be generic"
    fi

    # sudo (needed for various operations)
    if command -v sudo >/dev/null 2>&1; then
        check_pass "sudo: $(command -v sudo)"
    else
        if [ "$EUID" -eq 0 ]; then
            check_pass "running as root (sudo not needed)"
        else
            check_warn "sudo: NOT FOUND — some operations may fail"
        fi
    fi

    # -----------------------------------------------------------------------
    section "5. KVM / Hardware Virtualization"
    # -----------------------------------------------------------------------

    local arch=$(detect_arch)

    # /dev/kvm device
    if [ -c /dev/kvm ]; then
        check_pass "/dev/kvm device exists"

        # Check permissions on /dev/kvm
        if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
            check_pass "/dev/kvm is readable and writable by current user"
        else
            # Check via group membership
            local kvm_group=$(stat -c '%G' /dev/kvm 2>/dev/null || stat -f '%Sg' /dev/kvm 2>/dev/null)
            if id -nG "$CURRENT_USER" 2>/dev/null | grep -qw "$kvm_group"; then
                check_pass "/dev/kvm accessible via group '$kvm_group'"
            else
                check_warn "/dev/kvm not accessible — add user to '$kvm_group' group"
            fi
        fi
    else
        check_warn "/dev/kvm not found — VMs will use emulation (very slow)"
    fi

    # CPU virtualization extensions
    if [ "$arch" = "amd64" ]; then
        if grep -qE 'vmx|svm' /proc/cpuinfo 2>/dev/null; then
            if grep -q 'vmx' /proc/cpuinfo; then
                check_pass "CPU: Intel VT-x supported"
            else
                check_pass "CPU: AMD-V supported"
            fi
        else
            check_warn "CPU: no hardware virtualization extensions detected"
        fi
    elif [ "$arch" = "arm64" ]; then
        check_pass "CPU: ARM64 architecture ($(uname -m))"
    fi

    # -----------------------------------------------------------------------
    section "6. Libvirt Service"
    # -----------------------------------------------------------------------

    # libvirtd running
    if systemctl is-active --quiet libvirtd 2>/dev/null; then
        check_pass "libvirtd: active (running)"
    elif service libvirtd status >/dev/null 2>&1; then
        check_pass "libvirtd: active (running via init)"
    else
        check_fail "libvirtd: NOT RUNNING — start with: sudo systemctl start libvirtd"
    fi

    # libvirtd enabled on boot
    if systemctl is-enabled --quiet libvirtd 2>/dev/null; then
        check_pass "libvirtd: enabled on boot"
    else
        check_warn "libvirtd: not enabled on boot — enable with: sudo systemctl enable libvirtd"
    fi

    # QEMU emulator binary exists for this arch
    if [ "$arch" = "arm64" ]; then
        if command -v qemu-system-aarch64 >/dev/null 2>&1; then
            check_pass "qemu-system-aarch64: $(command -v qemu-system-aarch64)"
        else
            check_fail "qemu-system-aarch64: NOT FOUND — cannot create ARM64 VMs"
        fi
    else
        if command -v qemu-system-x86_64 >/dev/null 2>&1; then
            check_pass "qemu-system-x86_64: $(command -v qemu-system-x86_64)"
        else
            check_fail "qemu-system-x86_64: NOT FOUND — cannot create x86_64 VMs"
        fi
    fi

    # UEFI firmware (required for VM boot)
    if [ "$arch" = "arm64" ]; then
        if [ -f /usr/share/AAVMF/AAVMF_CODE.no-secboot.fd ]; then
            check_pass "UEFI firmware: AAVMF (no-secboot) found"
        elif [ -f /usr/share/AAVMF/AAVMF_CODE.fd ]; then
            check_pass "UEFI firmware: AAVMF found"
        else
            check_fail "UEFI firmware: AAVMF NOT FOUND — ARM64 VMs cannot boot"
            check_fail "  Install with: sudo apt install qemu-efi-aarch64"
        fi
        if [ -f /usr/share/AAVMF/AAVMF_VARS.fd ]; then
            check_pass "UEFI vars template: AAVMF_VARS.fd found"
        else
            check_fail "UEFI vars template: NOT FOUND"
        fi
        # Warn about secure boot default
        if [ -L /usr/share/AAVMF/AAVMF_CODE.ms.fd ]; then
            local sb_target=$(readlink -f /usr/share/AAVMF/AAVMF_CODE.ms.fd)
            if echo "$sb_target" | grep -q "secboot"; then
                check_warn "UEFI default (AAVMF_CODE.ms.fd) points to Secure Boot firmware"
                check_warn "  nox disables Secure Boot for cloud images — this is expected"
            fi
        fi
    else
        if [ -f /usr/share/OVMF/OVMF_CODE_4M.fd ]; then
            check_pass "UEFI firmware: OVMF found"
        else
            check_fail "UEFI firmware: OVMF NOT FOUND — x86_64 VMs cannot boot"
            check_fail "  Install with: sudo apt install ovmf"
        fi
        if [ -f /usr/share/OVMF/OVMF_VARS_4M.fd ]; then
            check_pass "UEFI vars template: OVMF_VARS_4M.fd found"
        else
            check_fail "UEFI vars template: NOT FOUND"
        fi
        if [ -L /usr/share/OVMF/OVMF_CODE_4M.ms.fd ]; then
            local sb_target=$(readlink -f /usr/share/OVMF/OVMF_CODE_4M.ms.fd)
            if echo "$sb_target" | grep -q "secboot"; then
                check_warn "UEFI default (OVMF_CODE_4M.ms.fd) points to Secure Boot firmware"
                check_warn "  nox disables Secure Boot for cloud images — this is expected"
            fi
        fi
    fi

    # -----------------------------------------------------------------------
    section "7. Networking"
    # -----------------------------------------------------------------------

    # nox-nat
    if _virsh net-info nox-nat 2>/dev/null | grep -q "Active:.*yes"; then
        check_pass "nox-nat: active"
    else
        check_warn "nox-nat: not active — run installer to create it"
    fi

    if _virsh net-dumpxml nox-nat 2>/dev/null | grep -q "mode='nat'"; then
        check_pass "nox-nat: NAT mode configured (VMs isolated from LAN)"
    else
        check_warn "nox-nat: not in NAT mode or not defined"
    fi

    # IPv4 forwarding
    if [ -f /proc/sys/net/ipv4/ip_forward ]; then
        local ip_fwd=$(cat /proc/sys/net/ipv4/ip_forward)
        if [ "$ip_fwd" = "1" ]; then
            check_pass "IPv4 forwarding: enabled"
        else
            check_warn "IPv4 forwarding: disabled"
            check_warn "  Enable with: sudo sysctl -w net.ipv4.ip_forward=1"
        fi
    fi

    # -----------------------------------------------------------------------
    section "8. User Groups & Permissions"
    # -----------------------------------------------------------------------

    # libvirt group
    if getent group libvirt >/dev/null 2>&1; then
        if id -nG "$CURRENT_USER" 2>/dev/null | grep -qw "libvirt"; then
            check_pass "User '$CURRENT_USER' in 'libvirt' group"
        else
            check_warn "User '$CURRENT_USER' NOT in 'libvirt' group — may need to log out/in"
        fi
    else
        check_warn "'libvirt' group does not exist"
    fi

    # kvm group
    if getent group kvm >/dev/null 2>&1; then
        if id -nG "$CURRENT_USER" 2>/dev/null | grep -qw "kvm"; then
            check_pass "User '$CURRENT_USER' in 'kvm' group"
        else
            check_warn "User '$CURRENT_USER' NOT in 'kvm' group — KVM access may fail"
        fi
    fi

    # libvirt-qemu group
    if getent group libvirt-qemu >/dev/null 2>&1; then
        if id -nG "$CURRENT_USER" 2>/dev/null | grep -qw "libvirt-qemu"; then
            check_pass "User '$CURRENT_USER' in 'libvirt-qemu' group"
        else
            check_warn "User '$CURRENT_USER' NOT in 'libvirt-qemu' group"
        fi
    fi

    # Home directory traversable by libvirt-qemu (critical for VM disk access)
    local home_perms=$(stat -c '%a' "$HOME_DIR" 2>/dev/null || stat -f '%Lp' "$HOME_DIR" 2>/dev/null)
    if [ -n "$home_perms" ]; then
        # Check if "others" have execute permission (needed for libvirt-qemu to traverse)
        local other_x=$((home_perms % 10))
        if [ $((other_x & 1)) -eq 1 ]; then
            check_pass "Home directory ($HOME_DIR): traversable (mode $home_perms)"
        else
            check_fail "Home directory ($HOME_DIR): NOT traversable by libvirt-qemu (mode $home_perms)"
            check_fail "  Fix with: chmod 711 $HOME_DIR"
            check_fail "  Without this, VM disk images under ~/.nox/ cannot be accessed by the hypervisor"
        fi
    fi

    # polkit rules
    if [ -f /etc/polkit-1/rules.d/50-libvirt.rules ]; then
        check_pass "polkit libvirt rule: configured"
    else
        check_warn "polkit libvirt rule: not found — may need password for virsh commands"
    fi

    # -----------------------------------------------------------------------
    section "9. Filesystem & Storage"
    # -----------------------------------------------------------------------

    # ~/.nox directory structure
    local nox_dir="$HOME_DIR/.nox"
    if [ -d "$nox_dir" ]; then
        check_pass "nox data directory: $nox_dir exists"
    else
        check_warn "nox data directory: $nox_dir does not exist (will be created on first run)"
    fi

    if [ -d "$nox_dir/vms" ]; then
        check_pass "VMs directory: $nox_dir/vms exists"
    else
        check_warn "VMs directory: will be created on first run"
    fi

    if [ -d "$nox_dir/images" ]; then
        check_pass "Images cache directory: $nox_dir/images exists"
    else
        check_warn "Images cache directory: will be created on first run"
    fi

    # Disk space check (need at least 2GB for a minimal VM image + disk)
    local avail_gb=$(df --output=avail -BG "$HOME_DIR" 2>/dev/null | tail -1 | tr -d ' G')
    if [ -n "$avail_gb" ] && [ "$avail_gb" -gt 0 ] 2>/dev/null; then
        if [ "$avail_gb" -ge 10 ]; then
            check_pass "Available disk space: ${avail_gb}GB (plenty)"
        elif [ "$avail_gb" -ge 2 ]; then
            check_warn "Available disk space: ${avail_gb}GB (low — may only fit 1-2 small VMs)"
        else
            check_fail "Available disk space: ${avail_gb}GB (too low — need at least 2GB for a minimal VM)"
        fi
    fi

    # /usr/local/bin writable (for nox binary)
    if [ -w /usr/local/bin ] || [ -n "$SUDO" ]; then
        check_pass "/usr/local/bin: writable (for nox command)"
    else
        check_fail "/usr/local/bin: NOT writable — cannot install nox command"
    fi

    # /usr/local/lib writable (for nox.ts)
    if [ -w /usr/local/lib ] || [ -n "$SUDO" ]; then
        check_pass "/usr/local/lib: writable (for nox library)"
    else
        check_fail "/usr/local/lib: NOT writable — cannot install nox library"
    fi

    # -----------------------------------------------------------------------
    section "10. SSH Configuration"
    # -----------------------------------------------------------------------

    # SSH key exists
    if [ -f "$HOME_DIR/.ssh/id_ed25519.pub" ]; then
        check_pass "SSH key: ed25519 key found"
    elif [ -f "$HOME_DIR/.ssh/id_rsa.pub" ]; then
        check_pass "SSH key: RSA key found"
    else
        check_warn "SSH key: no key found (will be generated on first VM creation)"
    fi

    # ~/.ssh directory permissions
    if [ -d "$HOME_DIR/.ssh" ]; then
        local ssh_perms=$(stat -c '%a' "$HOME_DIR/.ssh" 2>/dev/null || stat -f '%Lp' "$HOME_DIR/.ssh" 2>/dev/null)
        if [ "$ssh_perms" = "700" ]; then
            check_pass "~/.ssh directory permissions: $ssh_perms (correct)"
        else
            check_warn "~/.ssh directory permissions: $ssh_perms (should be 700)"
        fi
    fi

    # -----------------------------------------------------------------------
    section "11. System Resources"
    # -----------------------------------------------------------------------

    # RAM check
    if [ -f /proc/meminfo ]; then
        local total_ram_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
        local total_ram_mb=$((total_ram_kb / 1024))
        local total_ram_gb=$((total_ram_mb / 1024))
        if [ "$total_ram_mb" -ge 2048 ]; then
            check_pass "Total RAM: ${total_ram_mb}MB (${total_ram_gb}GB)"
        elif [ "$total_ram_mb" -ge 1024 ]; then
            check_warn "Total RAM: ${total_ram_mb}MB — limited, VMs will need small RAM allocations"
        else
            check_warn "Total RAM: ${total_ram_mb}MB — very limited for running VMs"
        fi

        local avail_ram_kb=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
        if [ -n "$avail_ram_kb" ]; then
            local avail_ram_mb=$((avail_ram_kb / 1024))
            check_pass "Available RAM: ${avail_ram_mb}MB"
        fi
    else
        check_warn "/proc/meminfo: not readable — RAM detection will use fallback"
    fi

    # CPU count
    local cpu_count=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo "?")
    check_pass "CPU cores: $cpu_count"

    # Architecture
    check_pass "Architecture: $(detect_arch) ($(uname -m))"

    # -----------------------------------------------------------------------
    section "12. Internet Connectivity"
    # -----------------------------------------------------------------------

    # DNS resolution
    if host cloud.debian.org >/dev/null 2>&1 || nslookup cloud.debian.org >/dev/null 2>&1 || ping -c1 -W2 cloud.debian.org >/dev/null 2>&1; then
        check_pass "DNS resolution: cloud.debian.org resolves"
    else
        check_warn "DNS resolution: cannot resolve cloud.debian.org — image downloads may fail"
    fi

    # HTTPS connectivity (needed for image downloads)
    if curl -fsSL --connect-timeout 5 -o /dev/null "https://cloud.debian.org" 2>/dev/null; then
        check_pass "HTTPS connectivity: cloud.debian.org reachable"
    else
        check_warn "HTTPS connectivity: cannot reach cloud.debian.org — image downloads may fail"
    fi

    # GitHub connectivity (needed for updates)
    if curl -fsSL --connect-timeout 5 -o /dev/null "https://raw.githubusercontent.com" 2>/dev/null; then
        check_pass "GitHub connectivity: raw.githubusercontent.com reachable"
    else
        check_warn "GitHub connectivity: cannot reach GitHub — 'nox update' will fail"
    fi

    # -----------------------------------------------------------------------
    section "13. Nox Installation Files"
    # -----------------------------------------------------------------------

    # nox binary
    if command -v nox >/dev/null 2>&1; then
        check_pass "nox binary: $(command -v nox)"

        if [ -x "$(command -v nox)" ]; then
            check_pass "nox binary: executable"
        else
            check_fail "nox binary: NOT executable"
        fi
    else
        check_warn "nox binary: not installed yet"
    fi

    # VERSION file
    if [ -f /usr/local/lib/nox/VERSION ]; then
        check_pass "VERSION file: $(cat /usr/local/lib/nox/VERSION)"
    else
        check_warn "VERSION file: not found"
    fi

    # -----------------------------------------------------------------------
    section "14. Functional Smoke Test"
    # -----------------------------------------------------------------------

    # Test virsh can list VMs
    if _virsh list --all >/dev/null 2>&1; then
        check_pass "virsh list: works"
    else
        check_fail "virsh list: FAILED — cannot manage VMs"
    fi

    # Test virsh can list networks
    if _virsh net-list --all >/dev/null 2>&1; then
        check_pass "virsh net-list: works"
    else
        check_fail "virsh net-list: FAILED — cannot manage networks"
    fi

    # Test qemu-img can be invoked
    if qemu-img --version >/dev/null 2>&1; then
        check_pass "qemu-img invocation: works"
    else
        check_fail "qemu-img invocation: FAILED"
    fi

    # Test genisoimage can be invoked
    if genisoimage --version >/dev/null 2>&1 || genisoimage 2>&1 | grep -qi "genisoimage"; then
        check_pass "genisoimage invocation: works"
    else
        check_fail "genisoimage invocation: FAILED"
    fi

    # Test cloud-init ISO creation (dry run)
    local test_dir=$(mktemp -d)
    echo "test" > "$test_dir/user-data"
    echo "test" > "$test_dir/meta-data"
    if genisoimage -output "$test_dir/test.iso" -volid cidata -joliet -rock "$test_dir/user-data" "$test_dir/meta-data" >/dev/null 2>&1; then
        check_pass "cloud-init ISO creation: works"
    else
        check_fail "cloud-init ISO creation: FAILED — VM provisioning will break"
    fi
    rm -rf "$test_dir"

    # Test nox --version if installed
    if command -v nox >/dev/null 2>&1; then
        local nox_ver=$(nox --version 2>&1)
        if [ $? -eq 0 ] && [ -n "$nox_ver" ]; then
            check_pass "nox --version: $nox_ver"
        else
            check_fail "nox --version: FAILED — nox may be broken"
        fi
    fi

    # -----------------------------------------------------------------------
    section "Verification Summary"
    # -----------------------------------------------------------------------

    echo ""
    if [ "$ERRORS" -eq 0 ] && [ "$WARNINGS" -eq 0 ]; then
        info "All checks passed. nox is fully operational."
    elif [ "$ERRORS" -eq 0 ]; then
        info "All critical checks passed. $WARNINGS warning(s) — review above for optional improvements."
    else
        error "$ERRORS critical error(s) and $WARNINGS warning(s) found."
        error "Fix the errors above before using nox."
    fi
    echo ""
}

# ===========================================================================
# Main
# ===========================================================================

main() {
    local distro arch
    distro=$(detect_distro)
    arch=$(detect_arch)
    info "Detected: distro=$distro arch=$arch user=$CURRENT_USER"

    if [ "$SKIP_DEPS" = false ]; then
        # Install system packages
        case "$distro" in
            debian|ubuntu|raspbian|linuxmint|pop)
                install_debian ;;
            alpine)
                install_alpine ;;
            *)
                error "Unsupported distro: $distro"
                error "Supported: Debian, Ubuntu, Raspbian, Alpine"
                error "Please install manually: qemu, libvirt, virt-install, genisoimage"
                exit 1 ;;
        esac

        # Setup KVM, networks, permissions
        enable_kvm
        configure_libvirt_network
        setup_permissions
    else
        info "Skipping dependency installation"
    fi

    # Download and install nox binary
    info ""
    info "Installing nox command..."

    local arch=$(detect_arch)
    local binary_name="nox-linux-${arch}"

    TEMP_DIR=$(mktemp -d)

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$GITHUB_RAW_URL/VERSION" -o "$TEMP_DIR/VERSION" || {
            error "Failed to download VERSION from GitHub"
            rm -rf "$TEMP_DIR"
            exit 1
        }
        curl -fsSL -L "$GITHUB_RELEASE_URL/$binary_name" -o "$TEMP_DIR/nox" || {
            error "Failed to download nox binary from GitHub Releases"
            rm -rf "$TEMP_DIR"
            exit 1
        }
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$GITHUB_RAW_URL/VERSION" -O "$TEMP_DIR/VERSION" || {
            error "Failed to download VERSION from GitHub"
            rm -rf "$TEMP_DIR"
            exit 1
        }
        wget -q "$GITHUB_RELEASE_URL/$binary_name" -O "$TEMP_DIR/nox" || {
            error "Failed to download nox binary from GitHub Releases"
            rm -rf "$TEMP_DIR"
            exit 1
        }
    else
        error "Neither curl nor wget found."
        rm -rf "$TEMP_DIR"
        exit 1
    fi

    INSTALL_VERSION=$(cat "$TEMP_DIR/VERSION")

    $SUDO mkdir -p /usr/local/lib/nox
    $SUDO cp "$TEMP_DIR/nox" /usr/local/bin/nox
    $SUDO chmod +x /usr/local/bin/nox
    $SUDO cp "$TEMP_DIR/VERSION" /usr/local/lib/nox/VERSION

    rm -rf "$TEMP_DIR"

    # Setup SSH key if not exists
    if [ ! -f "$HOME_DIR/.ssh/id_ed25519" ]; then
        info "Generating SSH key for passwordless VM access..."
        mkdir -p "$HOME_DIR/.ssh"
        chmod 700 "$HOME_DIR/.ssh"
        ssh-keygen -t ed25519 -f "$HOME_DIR/.ssh/id_ed25519" -N '' -q -C "nox@$(hostname 2>/dev/null || echo 'localhost')"
    fi

    info ""
    info "✓ nox installed successfully! (version $INSTALL_VERSION)"

    # Run comprehensive verification
    run_full_verification

    # Final instructions
    local needs_relogin=false
    for group in libvirt kvm libvirt-qemu; do
        if getent group "$group" >/dev/null 2>&1; then
            if ! id -nG "$CURRENT_USER" 2>/dev/null | grep -qw "$group"; then
                needs_relogin=true
                break
            fi
        fi
    done

    if [ "$needs_relogin" = true ]; then
        warn ""
        warn "IMPORTANT: Log out and log back in for group changes to take effect!"
        warn "Then run: nox --version"
    fi

    info ""
    info "Quick start:"
    info "  nox create myvm --os debian --cpus 2 --ram 1024 --disk 10"
    info "  nox list"
    info "  nox ssh myvm"
    info ""
    info "Update nox: nox update"
    info "For more: https://github.com/solosmith/nox"
}

main "$@"
