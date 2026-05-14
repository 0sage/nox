#!/usr/bin/env bun
/**
 * nox - Lightweight VM Manager using libvirt/KVM
 * Rewritten in TypeScript for Bun runtime
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, rmSync } from "fs";
import { join, dirname } from "path";
import { homedir, arch as osArch } from "os";
import { parseArgs } from "util";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const VERSION = readVersion();
const NOX_DIR = join(homedir(), ".nox");
const VMS_DIR = join(NOX_DIR, "vms");
const IMAGES_DIR = join(NOX_DIR, "images");
const CONFIG_FILE = join(NOX_DIR, "config.json");
const GITHUB_RAW_URL = "https://raw.githubusercontent.com/solosmith/nox/main";

const DEFAULT_CONFIG = {
  defaults: { os: "debian", cpus: 1, ram: 512, disk: 5 },
};

const OS_IMAGES: Record<string, { url: string; url_amd64: string }> = {
  debian: {
    url: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
    url_amd64: "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function readVersion(): string {
  const locations = [
    join(dirname(Bun.main), "VERSION"),
    "/usr/local/lib/nox/VERSION",
    "/usr/local/bin/VERSION",
  ];
  for (const loc of locations) {
    try {
      return readFileSync(loc, "utf-8").trim();
    } catch {}
  }
  return "unknown";
}

function ensureDirs() {
  mkdirSync(VMS_DIR, { recursive: true });
  mkdirSync(IMAGES_DIR, { recursive: true });
}

function loadConfig(): typeof DEFAULT_CONFIG & { s3?: Record<string, any> } {
  try {
    return JSON.parse(readFileSync(CONFIG_FILE, "utf-8"));
  } catch {
    return { ...DEFAULT_CONFIG };
  }
}

function hostArch(): "arm64" | "amd64" {
  const m = osArch();
  return m === "arm64" ? "arm64" : "amd64";
}

function hostRamMb(): number {
  try {
    const meminfo = readFileSync("/proc/meminfo", "utf-8");
    const match = meminfo.match(/MemTotal:\s+(\d+)/);
    return match ? Math.floor(parseInt(match[1]) / 1024) : 1024;
  } catch {
    return 1024;
  }
}



function resolveCpus(value: number): { vcpus: number; cpuFraction: number } {
  const v = value <= 0 ? 1 : value;
  return { vcpus: Math.max(1, Math.ceil(v)), cpuFraction: v };
}

interface RunResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

function run(cmd: string, check = true): RunResult {
  const result = Bun.spawnSync(["bash", "-c", cmd]);
  const stdout = result.stdout.toString();
  const stderr = result.stderr.toString();
  const exitCode = result.exitCode;
  if (check && exitCode !== 0) {
    throw new Error(`Command failed: ${cmd}\n${stderr}`);
  }
  return { stdout, stderr, exitCode };
}

function virsh(cmd: string, check = true): RunResult {
  return run(`virsh --connect qemu:///system ${cmd}`, check);
}

function vmExists(name: string): boolean {
  return virsh(`dominfo ${name}`, false).exitCode === 0;
}

function vmState(name: string): string | null {
  const result = virsh(`domstate ${name}`, false);
  return result.exitCode === 0 ? result.stdout.trim() : null;
}

function vmDir(name: string): string {
  return join(VMS_DIR, name);
}

function metaPath(name: string): string {
  return join(vmDir(name), "meta.json");
}

function loadMeta(name: string): Record<string, any> | null {
  try {
    return JSON.parse(readFileSync(metaPath(name), "utf-8"));
  } catch {
    return null;
  }
}

function saveMeta(name: string, meta: Record<string, any>) {
  mkdirSync(vmDir(name), { recursive: true });
  writeFileSync(metaPath(name), JSON.stringify(meta, null, 2));
}

function vmIp(name: string, timeout = 60): string | null {
  const ips = vmIps(name, timeout);
  return ips[0] ?? null;
}

function vmIps(name: string, timeout = 60): string[] {
  const deadline = Date.now() + timeout * 1000;
  while (Date.now() < deadline) {
    const result = virsh(`domifaddr ${name} --source agent`, false);
    if (result.exitCode === 0) {
      const ips: string[] = [];
      for (const line of result.stdout.split("\n")) {
        if (line.toLowerCase().includes("ipv4") && !line.includes("127.0.0.1")) {
          const parts = line.trim().split(/\s+/);
          for (const part of parts) {
            if (part.includes("/") && !part.startsWith("127.")) {
              ips.push(part.split("/")[0]);
            }
          }
        }
      }
      if (ips.length > 0) return ips;
    }
    Bun.sleepSync(2000);
  }
  return [];
}

function applyCpuLimit(name: string, cpuFraction: number) {
  const period = 100000;
  const quota = Math.max(1000, Math.floor(cpuFraction * period));
  const params = `--set vcpu_quota=${quota} --set vcpu_period=${period} --set emulator_quota=${quota} --set emulator_period=${period} --set global_quota=${quota} --set global_period=${period}`;

  const state = vmState(name);
  if (state === "running") {
    virsh(`schedinfo ${name} ${params} --config --live`, false);
  } else {
    virsh(`schedinfo ${name} ${params} --config`, false);
  }
  console.log(`✓ CPU limit applied: ${cpuFraction} CPU(s) max (${quota}/${period} quota/period)`);
}

function generatePassword(): string {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

function findSshKey(): string | null {
  for (const keyfile of ["id_ed25519.pub", "id_rsa.pub"]) {
    const p = join(homedir(), ".ssh", keyfile);
    try {
      return readFileSync(p, "utf-8").trim();
    } catch {}
  }
  return null;
}

function ensureSshKey() {
  const sshDir = join(homedir(), ".ssh");
  const keyPath = join(sshDir, "id_ed25519");
  if (existsSync(`${keyPath}.pub`)) return;
  mkdirSync(sshDir, { recursive: true, mode: 0o700 });
  Bun.spawnSync(["ssh-keygen", "-t", "ed25519", "-f", keyPath, "-N", "", "-C", `nox@${run("hostname", false).stdout.trim()}`]);
}

function detectActiveNetwork(): string {
  for (const net of ["nox-nat", "default"]) {
    const result = virsh(`net-info ${net}`, false);
    if (result.exitCode === 0 && result.stdout.split("Active:")[1]?.split("\n")[0]?.includes("yes")) {
      return net;
    }
  }
  return "default";
}

// ---------------------------------------------------------------------------
// Cloud-init
// ---------------------------------------------------------------------------

function generateCloudInit(name: string, password: string, osName = "debian"): { userData: string; metaData: string } {
  const sshKey = findSshKey() ?? "";

  const userData = `#cloud-config
hostname: ${name}
fqdn: ${name}.local
manage_etc_hosts: true

users:
  - name: nox
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
    lock_passwd: false
    passwd: ${password}
    ssh_authorized_keys:
      - ${sshKey}

ssh_pwauth: true

package_update: true
package_upgrade: false
packages:
  - qemu-guest-agent

runcmd:
  - echo 'nox:${password}' | chpasswd
  - systemctl enable qemu-guest-agent
  - systemctl start qemu-guest-agent
  - systemctl enable ssh
  - systemctl start ssh
`;

  const metaData = `instance-id: ${name}
local-hostname: ${name}
`;

  return { userData, metaData };
}

// ---------------------------------------------------------------------------
// VM Creation
// ---------------------------------------------------------------------------

interface CreateOptions {
  name: string;
  osName?: string;
  cpus?: number;
  ram?: number;
  disk?: number;
  autostart?: boolean;
  password?: string;
  start?: boolean;
  network?: string;
}

function createVm(opts: CreateOptions): { success: boolean; password: string | null } {
  const { name } = opts;

  if (vmExists(name)) {
    console.log(`VM '${name}' already exists.`);
    return { success: false, password: null };
  }

  const cfg = loadConfig();
  const defaults = cfg.defaults ?? DEFAULT_CONFIG.defaults;

  const osName = opts.osName ?? defaults.os ?? "debian";
  const cpusRaw = opts.cpus ?? defaults.cpus ?? 1;
  const ramRaw = opts.ram ?? defaults.ram ?? 512;
  const diskRaw = opts.disk ?? defaults.disk ?? 5;
  const autostart = opts.autostart ?? true;
  const startVm = opts.start ?? true;
  const network = opts.network ?? detectActiveNetwork();
  const password = opts.password ?? generatePassword();

  const arch = hostArch();
  const { vcpus, cpuFraction } = resolveCpus(cpusRaw);
  const ramMb = Math.max(1, Math.floor(ramRaw));
  const diskGb = Math.max(1, Math.floor(diskRaw));

  console.log(`Creating VM '${name}': os=${osName} vcpus=${vcpus} ram=${ramMb}MB disk=${diskGb}GB network=${network}`);

  const vmPath = vmDir(name);
  mkdirSync(vmPath, { recursive: true });

  // Download cloud image
  const osInfo = OS_IMAGES[osName];
  if (!osInfo) throw new Error(`Unknown OS: ${osName}`);

  const imageUrl = arch === "arm64" ? osInfo.url : osInfo.url_amd64;
  if (!imageUrl) throw new Error(`No image URL for ${osName} on ${arch}`);

  const imageFilename = `${osName}-${arch}.qcow2`;
  const baseImage = join(IMAGES_DIR, imageFilename);

  if (!existsSync(baseImage)) {
    console.log(`Downloading ${osName} cloud image...`);
    run(`curl -fsSL ${imageUrl} -o ${baseImage}`);
  } else {
    console.log(`Using cached ${osName} cloud image`);
  }

  // Create disk
  const diskPath = join(vmPath, `${name}.qcow2`);
  run(`qemu-img create -f qcow2 -F qcow2 -b ${baseImage} ${diskPath} ${diskGb}G`);

  // Cloud-init
  const { userData, metaData } = generateCloudInit(name, password, osName);
  const userDataPath = join(vmPath, "user-data");
  const metaDataPath = join(vmPath, "meta-data");
  writeFileSync(userDataPath, userData);
  writeFileSync(metaDataPath, metaData);

  const cloudInitIso = join(vmPath, "cloud-init.iso");
  run(`genisoimage -output ${cloudInitIso} -volid cidata -joliet -rock ${userDataPath} ${metaDataPath}`);

  // virt-install
  const cmdParts = [
    "virt-install",
    "--connect qemu:///system",
    `--name ${name}`,
    `--memory ${ramMb}`,
    `--vcpus ${vcpus}`,
    "--cpu host-passthrough",
    `--disk ${diskPath},format=qcow2,bus=virtio,cache=writeback,io=threads`,
    `--disk ${cloudInitIso},device=cdrom`,
    "--os-variant generic",
    "--boot firmware.feature0.name=secure-boot,firmware.feature0.enabled=no",
    `--network network=${network},model=virtio`,
    "--graphics none",
    "--console pty,target_type=serial",
    "--import",
    "--noautoconsole",
  ];
  if (!startVm) cmdParts.push("--noreboot");

  try {
    run(cmdParts.join(" "));
  } catch (e) {
    console.error(`Failed to create VM: ${e}`);
    rmSync(vmPath, { recursive: true, force: true });
    return { success: false, password: null };
  }

  if (autostart) virsh(`autostart ${name}`);
  applyCpuLimit(name, cpuFraction);

  saveMeta(name, {
    name, os: osName, arch, vcpus, cpu_fraction: cpuFraction,
    ram_mb: ramMb, disk_gb: diskGb, autostart, network,
  });

  console.log(startVm ? `VM '${name}' created and starting...` : `VM '${name}' created (not started).`);
  return { success: true, password };
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

function cmdCreate(args: string[]) {
  const { values, positionals } = parseArgs({
    args,
    options: {
      os: { type: "string", default: undefined },
      cpus: { type: "string", default: undefined },
      ram: { type: "string", default: undefined },
      disk: { type: "string", default: undefined },
      network: { type: "string", default: undefined },
      passwd: { type: "string", default: undefined },
      "no-autostart": { type: "boolean", default: false },
      "no-start": { type: "boolean", default: false },
    },
    allowPositionals: true,
  });

  const name = positionals[0];
  if (!name) { console.error("Usage: nox create <name> [options]"); process.exit(1); }

  const { success, password } = createVm({
    name,
    osName: values.os as string | undefined,
    cpus: values.cpus ? parseFloat(values.cpus as string) : undefined,
    ram: values.ram ? parseFloat(values.ram as string) : undefined,
    disk: values.disk ? parseFloat(values.disk as string) : undefined,
    network: values.network as string | undefined,
    password: values.passwd as string | undefined,
    autostart: !values["no-autostart"],
    start: !values["no-start"],
  });

  if (!success) return;

  if (!values["no-start"]) {
    console.log("\nWaiting for VM to boot and get IP address...");
    const ip = vmIp(name, 180);

    console.log(`\n${"=".repeat(60)}`);
    console.log(`VM '${name}' is ready!`);
    console.log(`${"=".repeat(60)}`);
    console.log(`\nSSH Access (password shown once):`);
    console.log(`  Password: ${password}`);
    if (ip) console.log(`  IP:       ${ip}`);
    else console.log(`  IP not detected yet. Use 'nox list' to find it.`);
    console.log(`\nConnect via SSH:`);
    console.log(`  nox ssh ${name}`);
    console.log(`\nIMPORTANT: Save this password - it won't be shown again!`);
    console.log(`${"=".repeat(60)}`);
  }
}

function cmdStart(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }
  virsh(`start ${name}`);
  const meta = loadMeta(name);
  if (meta) applyCpuLimit(name, meta.cpu_fraction ?? meta.vcpus ?? 1);
  console.log(`VM '${name}' started.`);
}

function cmdStop(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }
  virsh(`shutdown ${name}`);
  console.log(`VM '${name}' shutting down.`);
}

function cmdRestart(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }
  virsh(`reboot ${name}`);
  console.log(`VM '${name}' restarted.`);
}

function cmdDelete(args: string[]) {
  const name = args[0];
  if (!name) { console.error("Usage: nox delete <name>"); process.exit(1); }

  if (!vmExists(name)) {
    const d = vmDir(name);
    if (existsSync(d)) {
      rmSync(d, { recursive: true, force: true });
      console.log(`Cleaned up local files for '${name}'.`);
    } else {
      console.error(`VM '${name}' does not exist.`);
    }
    return;
  }

  if (vmState(name) === "running") virsh(`destroy ${name}`);

  const delMeta = loadMeta(name);
  if (delMeta?.forwards?.length) {
    const ip = vmIp(name, 5);
    if (ip) {
      for (const f of delMeta.forwards) iptablesForwardRemove(ip, f.host_port, f.vm_port, f.proto);
    }
  }

  virsh(`undefine ${name} --nvram --remove-all-storage`, false);
  const d = vmDir(name);
  if (existsSync(d)) rmSync(d, { recursive: true, force: true });
  console.log(`VM '${name}' deleted.`);
}

function cmdList() {
  const result = virsh("list --all", false);
  if (result.exitCode !== 0) { console.error("Could not list VMs. Is libvirt installed?"); process.exit(1); }

  const vmNames: string[] = [];
  for (const line of result.stdout.split("\n").slice(2)) {
    const parts = line.trim().split(/\s+/);
    if (parts.length >= 2 && parts[1]) vmNames.push(parts[1]);
  }

  if (vmNames.length === 0) { console.log("No VMs found."); return; }

  console.log(
    `${"NAME".padEnd(20)} ${"STATE".padEnd(15)} ${"OS".padEnd(10)} ${"CPUS".padEnd(6)} ${"RAM".padEnd(8)} ${"DISK".padEnd(8)} ${"AUTOSTART".padEnd(10)} IP`
  );
  console.log("-".repeat(95));

  for (const name of vmNames) {
    const state = vmState(name) ?? "UNKNOWN";
    const meta = loadMeta(name) ?? {};
    const os = meta.os ?? "?";
    const vcpus = meta.vcpus ?? "?";
    const ram = meta.ram_mb != null ? `${meta.ram_mb}MB` : "?";
    const disk = meta.disk_gb != null ? `${meta.disk_gb}GB` : "?";
    const auto = meta.autostart ? "yes" : "no";
    const ip = state === "running" ? vmIps(name, 1).join(", ") : "";

    console.log(
      `${name.padEnd(20)} ${state.padEnd(15)} ${os.padEnd(10)} ${String(vcpus).padEnd(6)} ${ram.padEnd(8)} ${disk.padEnd(8)} ${auto.padEnd(10)} ${ip}`
    );
  }
}

async function cmdSsh(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  if (state !== "running") {
    console.error(`VM '${name}' is not running. Start it with: nox start ${name}`);
    process.exit(1);
  }

  ensureSshKey();

  console.log(`Getting IP address for '${name}'...`);
  const ip = vmIp(name, 30);
  if (!ip) {
    console.error(`Failed to get IP address for VM '${name}'.`);
    console.error("Make sure the VM has networking configured and qemu-guest-agent is running.");
    process.exit(1);
  }

  const sshOpts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"];
  const sshCommand = args.slice(1);

  // Check for -- separator and get command after it
  const dashIdx = args.indexOf("--");
  const remoteCmd = dashIdx >= 0 ? args.slice(dashIdx + 1) : (sshCommand.length > 0 ? sshCommand : []);

  if (remoteCmd.length > 0) {
    const proc = Bun.spawnSync(["ssh", ...sshOpts, `nox@${ip}`, ...remoteCmd], {
      stdin: "inherit",
      stdout: "inherit",
      stderr: "inherit",
    });
    process.exit(proc.exitCode);
  } else {
    console.log(`Connecting to '${name}' at ${ip}...`);
    const proc = Bun.spawn(["ssh", ...sshOpts, `nox@${ip}`], {
      stdin: "inherit",
      stdout: "inherit",
      stderr: "inherit",
    });
    const result = await proc.exited;
    process.exit(result);
  }
}

async function cmdSerial(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  if (state !== "running") {
    console.error(`VM '${name}' is not running. Start it with: nox start ${name}`);
    process.exit(1);
  }

  console.log(`Connecting to serial console of '${name}'... (use Ctrl+] to exit)`);
  const proc = Bun.spawn(["virsh", "-c", "qemu:///system", "console", name], {
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
  const result = await proc.exited;
  process.exit(result);
}

function cmdRun(args: string[]) {
  const name = args[0];
  const cmd = args.slice(1).join(" ");
  if (!name || !cmd) { console.error("Usage: nox run <name> <command>"); process.exit(1); }
  if (!vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  if (state !== "running") {
    console.error(`VM '${name}' is not running. Start it with: nox start ${name}`);
    process.exit(1);
  }

  ensureSshKey();

  const ip = vmIp(name, 30);
  if (!ip) {
    console.error(`Failed to get IP address for VM '${name}'.`);
    process.exit(1);
  }

  const sshOpts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"];
  const proc = Bun.spawnSync(["ssh", ...sshOpts, `nox@${ip}`, cmd], {
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
  process.exit(proc.exitCode);
}

function cmdStatus(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }
  const result = virsh(`dominfo ${name}`);
  console.log(result.stdout);
}

function cmdStats(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  if (state !== "running") {
    console.error(`VM '${name}' is not running.`);
    process.exit(1);
  }

  const meta = loadMeta(name);

  // CPU usage via domstats
  const cpuStats = virsh(`domstats ${name} --cpu-total`, false);
  let cpuTime = 0;
  for (const line of cpuStats.stdout.split("\n")) {
    const m = line.match(/cpu\.time=(\d+)/);
    if (m) cpuTime = parseInt(m[1]);
  }

  // Memory usage
  virsh(`dommemstat ${name} --period 1 --live`, false);
  // small delay to let the stat collect
  Bun.sleepSync(1500);
  const memStats = virsh(`dommemstat ${name}`, false);
  let memTotal = 0, memAvailable = 0, memUsed = 0;
  for (const line of memStats.stdout.split("\n")) {
    const parts = line.trim().split(/\s+/);
    if (parts[0] === "actual") memTotal = parseInt(parts[1]) || 0;
    if (parts[0] === "available") memAvailable = parseInt(parts[1]) || 0;
    if (parts[0] === "unused") memUsed = memTotal - (parseInt(parts[1]) || 0);
  }
  if (!memUsed && memAvailable) memUsed = memTotal - memAvailable;

  // Disk usage via domblkinfo
  const blkList = virsh(`domblklist ${name}`, false);
  let diskTotal = 0, diskUsed = 0;
  for (const line of blkList.stdout.split("\n")) {
    const parts = line.trim().split(/\s+/);
    if (parts[1] && parts[1].endsWith(".qcow2")) {
      const blkInfo = virsh(`domblkinfo ${name} ${parts[0]}`, false);
      for (const bl of blkInfo.stdout.split("\n")) {
        const m = bl.match(/^Capacity:\s+(\d+)/);
        if (m) diskTotal = parseInt(m[1]);
        const m2 = bl.match(/^Allocation:\s+(\d+)/);
        if (m2) diskUsed = parseInt(m2[1]);
      }
    }
  }

  // Uptime via qemu-guest-agent
  let uptime = "N/A";
  const ip = vmIp(name, 5);
  if (ip) {
    const sshOpts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"];
    const uptimeProc = Bun.spawnSync(["ssh", ...sshOpts, `nox@${ip}`, "uptime -p"], {
      stdout: "pipe", stderr: "pipe",
    });
    if (uptimeProc.exitCode === 0) {
      uptime = uptimeProc.stdout.toString().trim();
    }
  }

  const fmtMb = (kb: number) => `${(kb / 1024).toFixed(0)} MB`;
  const fmtGb = (bytes: number) => `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;

  console.log(`\nVM '${name}' stats:`);
  console.log(`${"=".repeat(40)}`);
  console.log(`  CPU time:    ${(cpuTime / 1e9).toFixed(1)}s`);
  console.log(`  RAM:         ${fmtMb(memUsed)} / ${fmtMb(memTotal)} used`);
  console.log(`  Disk:        ${fmtGb(diskUsed)} / ${fmtGb(diskTotal)} used`);
  console.log(`  Uptime:      ${uptime}`);
  if (meta) {
    console.log(`  vCPUs:       ${meta.vcpus ?? "N/A"}`);
    console.log(`  OS:          ${meta.os ?? "N/A"}`);
    console.log(`  Network:     ${meta.network ?? "N/A"}`);
  }
  if (ip) console.log(`  IP:          ${ip}`);
  console.log(`${"=".repeat(40)}`);
}

function cmdPasswd(args: string[]) {
  const name = args[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  if (state !== "running") {
    console.error(`VM '${name}' is not running. Start it with: nox start ${name}`);
    process.exit(1);
  }

  console.log(`Generating new password for VM '${name}'...`);
  const newPassword = generatePassword();

  const pwB64 = btoa(`nox:${newPassword}`);
  const cmdStr = `echo $(echo ${pwB64} | base64 -d) | chpasswd`;
  const gaCmd = JSON.stringify({
    execute: "guest-exec",
    arguments: { path: "/bin/bash", arg: ["-c", cmdStr], "capture-output": true },
  });

  try {
    const result = virsh(`qemu-agent-command ${name} '${gaCmd}'`);
    const data = JSON.parse(result.stdout);
    const pid = data?.return?.pid;
    if (pid == null) throw new Error("guest-exec returned no pid");

    for (let i = 0; i < 10; i++) {
      Bun.sleepSync(1000);
      const statusCmd = JSON.stringify({ execute: "guest-exec-status", arguments: { pid } });
      const res2 = virsh(`qemu-agent-command ${name} '${statusCmd}'`);
      const status = JSON.parse(res2.stdout)?.return ?? {};
      if (status.exited) {
        if ((status.exitcode ?? 0) !== 0) {
          const err = atob(status["err-data"] ?? "");
          throw new Error(`chpasswd failed: ${err}`);
        }
        break;
      }
    }

    console.log(`\n${"=".repeat(60)}`);
    console.log(`Password changed for VM '${name}'!`);
    console.log(`${"=".repeat(60)}`);
    console.log(`\nNew password (shown once):`);
    console.log(`  Password: ${newPassword}`);
    console.log(`\nConnect: nox ssh ${name}`);
    console.log(`${"=".repeat(60)}`);
  } catch (e) {
    console.error(`Failed to change password: ${e}`);
    process.exit(1);
  }
}

function iptablesSave() {
  run("sudo /sbin/iptables-save > /etc/iptables/rules.v4", false);
}

function iptablesForwardAdd(vmIp: string, hostPort: number, vmPort: number, proto: string) {
  run(`sudo /sbin/iptables -t nat -A PREROUTING -p ${proto} --dport ${hostPort} -j DNAT --to-destination ${vmIp}:${vmPort}`);
  run(`sudo /sbin/iptables -A FORWARD -p ${proto} -d ${vmIp} --dport ${vmPort} -j ACCEPT`);
  run(`sudo /sbin/iptables -t nat -A OUTPUT -p ${proto} --dport ${hostPort} -j DNAT --to-destination ${vmIp}:${vmPort}`);
  iptablesSave();
}

function iptablesForwardRemove(vmIp: string, hostPort: number, vmPort: number, proto: string) {
  run(`sudo /sbin/iptables -t nat -D PREROUTING -p ${proto} --dport ${hostPort} -j DNAT --to-destination ${vmIp}:${vmPort}`, false);
  run(`sudo /sbin/iptables -D FORWARD -p ${proto} -d ${vmIp} --dport ${vmPort} -j ACCEPT`, false);
  run(`sudo /sbin/iptables -t nat -D OUTPUT -p ${proto} --dport ${hostPort} -j DNAT --to-destination ${vmIp}:${vmPort}`, false);
  iptablesSave();
}

function cmdForward(args: string[]) {
  const { values, positionals } = parseArgs({
    args,
    options: {
      proto: { type: "string", default: "tcp" },
      remove: { type: "boolean", default: false },
      list: { type: "boolean", default: false },
    },
    allowPositionals: true,
  });

  const name = positionals[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const meta = loadMeta(name) ?? {};
  const forwards: Array<{ host_port: number; vm_port: number; proto: string }> = meta.forwards ?? [];

  if (values.list) {
    if (forwards.length === 0) { console.log(`No port forwards for '${name}'.`); return; }
    console.log(`Port forwards for '${name}':`);
    for (const f of forwards) console.log(`  ${f.proto.toUpperCase()}  host:${f.host_port} → vm:${f.vm_port}`);
    return;
  }

  const mapping = positionals[1];
  if (!mapping) { console.error("Usage: nox forward <name> <host_port>:<vm_port> [--proto tcp|udp] [--remove]"); process.exit(1); }

  const [hostPortStr, vmPortStr] = mapping.split(":");
  const hostPort = parseInt(hostPortStr);
  const vmPort = parseInt(vmPortStr ?? hostPortStr);
  const proto = (values.proto as string).toLowerCase();

  if (isNaN(hostPort) || isNaN(vmPort)) { console.error("Invalid port mapping. Use format: 8080:80"); process.exit(1); }

  if (values.remove) {
    const ip = vmIp(name, 5);
    if (ip) iptablesForwardRemove(ip, hostPort, vmPort, proto);
    meta.forwards = forwards.filter(f => !(f.host_port === hostPort && f.vm_port === vmPort && f.proto === proto));
    saveMeta(name, meta);
    console.log(`✓ Removed forward ${proto.toUpperCase()} host:${hostPort} → vm:${vmPort}`);
    return;
  }

  if (vmState(name) !== "running") { console.error(`VM '${name}' is not running.`); process.exit(1); }

  const ip = vmIp(name, 30);
  if (!ip) { console.error(`Could not get IP for VM '${name}'.`); process.exit(1); }

  if (forwards.some(f => f.host_port === hostPort && f.proto === proto)) {
    console.error(`Host port ${hostPort}/${proto} is already forwarded for this VM.`);
    process.exit(1);
  }

  iptablesForwardAdd(ip, hostPort, vmPort, proto);
  forwards.push({ host_port: hostPort, vm_port: vmPort, proto });
  meta.forwards = forwards;
  saveMeta(name, meta);

  console.log(`✓ Forwarding host:${hostPort} → ${ip}:${vmPort} (${proto.toUpperCase()})`);
  console.log(`  Access via: http://<host-ip>:${hostPort}`);
}

function cmdResize(args: string[]) {
  const { values, positionals } = parseArgs({
    args,
    options: {
      cpus: { type: "string", default: undefined },
      ram: { type: "string", default: undefined },
      disk: { type: "string", default: undefined },
    },
    allowPositionals: true,
  });

  const name = positionals[0];
  if (!name || !vmExists(name)) { console.error(`VM '${name}' does not exist.`); process.exit(1); }

  const state = vmState(name);
  const meta = loadMeta(name);
  if (!meta) { console.error(`Could not load metadata for VM '${name}'`); process.exit(1); return; }

  const vmPath = vmDir(name);
  const diskPath = join(vmPath, `${name}.qcow2`);

  const cpusVal = values.cpus ? parseFloat(values.cpus as string) : undefined;
  const ramVal = values.ram ? parseFloat(values.ram as string) : undefined;
  const diskVal = values.disk ? parseFloat(values.disk as string) : undefined;

  const needsShutdown = (cpusVal != null || ramVal != null || diskVal != null) && state === "running";

  if (needsShutdown) {
    console.log("Note: Resize requires VM shutdown. Stopping VM...");
    virsh(`shutdown ${name}`);
    let stopped = false;
    for (let i = 0; i < 30; i++) {
      if (vmState(name) === "shut off") { stopped = true; break; }
      Bun.sleepSync(1000);
    }
    if (!stopped) {
      console.log("Graceful shutdown timed out, forcing stop...");
      virsh(`destroy ${name}`, false);
      Bun.sleepSync(2000);
    }
    if (vmState(name) !== "shut off") {
      console.error("Failed to stop VM. Cannot resize while running.");
      process.exit(1);
    }
  }

  if (cpusVal != null) {
    const { vcpus, cpuFraction } = resolveCpus(cpusVal);
    console.log(`Resizing CPUs to ${vcpus} vCPU(s) (limit: ${cpuFraction} cores)...`);
    virsh(`setvcpus ${name} ${vcpus} --maximum --config`);
    virsh(`setvcpus ${name} ${vcpus} --config`);
    meta.vcpus = vcpus;
    meta.cpu_fraction = cpuFraction;
    console.log(`✓ CPUs updated to ${vcpus} vCPU(s)`);
  }

  if (ramVal != null) {
    const ramMb = Math.max(1, Math.floor(ramVal));
    const ramKb = ramMb * 1024;
    console.log(`Resizing RAM to ${ramMb}MB...`);
    virsh(`setmaxmem ${name} ${ramKb} --config`);
    virsh(`setmem ${name} ${ramKb} --config`);
    meta.ram_mb = ramMb;
    console.log(`✓ RAM updated to ${ramMb}MB`);
  }

  if (diskVal != null) {
    const diskGb = Math.max(1, Math.floor(diskVal));
    const currentDisk = meta.disk_gb ?? 0;
    if (diskGb <= currentDisk) {
      console.error(`Error: New disk size (${diskGb}GB) must be larger than current size (${currentDisk}GB)`);
      console.error("Disk shrinking is not supported.");
      process.exit(1);
    }
    console.log(`Expanding disk from ${currentDisk}GB to ${diskGb}GB...`);
    run(`qemu-img resize ${diskPath} ${diskGb}G`);
    if (vmState(name) === "running") virsh(`blockresize ${name} ${diskPath} ${diskGb}G`);
    meta.disk_gb = diskGb;
    console.log(`✓ Disk expanded to ${diskGb}GB`);
    console.log("Note: You may need to resize the filesystem inside the VM:");
    console.log("  sudo growpart /dev/vda 1");
    console.log("  sudo resize2fs /dev/vda1");
  }

  saveMeta(name, meta);

  if (needsShutdown) {
    console.log("Restarting VM...");
    virsh(`start ${name}`);
  }

  if (cpusVal != null) applyCpuLimit(name, meta.cpu_fraction);

  console.log(`\n✓ VM '${name}' resized successfully!`);
  if (needsShutdown) console.log("VM state: running");
  else if (state === "shut off") console.log(`VM state: shut off (use 'nox start ${name}' to start)`);
}

// ---------------------------------------------------------------------------
// Doctor - comprehensive system verification
// ---------------------------------------------------------------------------

function cmdDoctor() {
  let errors = 0;
  let warnings = 0;

  const pass = (msg: string) => console.log(`  ✓ ${msg}`);
  const fail = (msg: string) => { console.error(`  ✗ ${msg}`); errors++; };
  const softWarn = (msg: string) => { console.log(`  ⚠ ${msg}`); warnings++; };

  console.log("\nnox doctor — comprehensive system check\n");

  // -- 1. Runtime & Core Binaries --
  console.log("── Runtime & Core Binaries ──");

  // bash
  if (run("command -v bash", false).exitCode === 0) pass("bash");
  else fail("bash: NOT FOUND");

  // curl
  if (run("command -v curl", false).exitCode === 0) pass("curl");
  else fail("curl: NOT FOUND — needed for image downloads and updates");

  // -- 2. Virtualization Binaries --
  console.log("\n── Virtualization Binaries ──");

  for (const bin of ["virsh", "qemu-img", "virt-install", "genisoimage"]) {
    const r = run(`command -v ${bin}`, false);
    if (r.exitCode === 0) pass(`${bin}: ${r.stdout.trim()}`);
    else fail(`${bin}: NOT FOUND`);
  }

  // QEMU system emulator for this arch
  const arch = hostArch();
  const qemuBin = arch === "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64";
  if (run(`command -v ${qemuBin}`, false).exitCode === 0) pass(`${qemuBin}`);
  else fail(`${qemuBin}: NOT FOUND — cannot create VMs on this architecture`);

  // UEFI firmware
  if (arch === "arm64") {
    if (existsSync("/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd")) pass("UEFI firmware: AAVMF (no-secboot)");
    else if (existsSync("/usr/share/AAVMF/AAVMF_CODE.fd")) pass("UEFI firmware: AAVMF");
    else fail("UEFI firmware: AAVMF NOT FOUND — install qemu-efi-aarch64");
    if (existsSync("/usr/share/AAVMF/AAVMF_VARS.fd")) pass("UEFI vars template: found");
    else fail("UEFI vars template: NOT FOUND");
  } else {
    if (existsSync("/usr/share/OVMF/OVMF_CODE_4M.fd")) pass("UEFI firmware: OVMF");
    else fail("UEFI firmware: OVMF NOT FOUND — install ovmf");
    if (existsSync("/usr/share/OVMF/OVMF_VARS_4M.fd")) pass("UEFI vars template: found");
    else fail("UEFI vars template: NOT FOUND");
  }
  softWarn("Secure Boot is disabled by nox for cloud image compatibility");

  // -- 3. SSH --
  console.log("\n── SSH ──");

  if (run("command -v ssh", false).exitCode === 0) pass("ssh client");
  else fail("ssh: NOT FOUND");

  if (run("command -v ssh-keygen", false).exitCode === 0) pass("ssh-keygen");
  else fail("ssh-keygen: NOT FOUND");

  const sshKey = findSshKey();
  if (sshKey) pass("SSH public key found");
  else softWarn("No SSH key — will be generated on first VM creation");

  // -- 4. KVM --
  console.log("\n── KVM / Hardware Virtualization ──");

  const kvmCheck = run("test -c /dev/kvm", false);
  if (kvmCheck.exitCode === 0) {
    pass("/dev/kvm device exists");
    const kvmPerms = run("test -r /dev/kvm -a -w /dev/kvm", false);
    if (kvmPerms.exitCode === 0) pass("/dev/kvm: readable and writable");
    else softWarn("/dev/kvm: not accessible by current user — check group membership");
  } else {
    softWarn("/dev/kvm not found — VMs will use emulation (slow)");
  }

  // -- 5. Libvirt Service --
  console.log("\n── Libvirt Service ──");

  const libvirtActive = run("systemctl is-active libvirtd 2>/dev/null || service libvirtd status 2>/dev/null", false);
  if (libvirtActive.stdout.trim() === "active" || libvirtActive.exitCode === 0) pass("libvirtd: running");
  else fail("libvirtd: NOT RUNNING — start with: sudo systemctl start libvirtd");

  const libvirtEnabled = run("systemctl is-enabled libvirtd 2>/dev/null", false);
  if (libvirtEnabled.stdout.trim() === "enabled") pass("libvirtd: enabled on boot");
  else softWarn("libvirtd: not enabled on boot");

  // qemu:///system connection
  const virshConn = virsh("version", false);
  if (virshConn.exitCode === 0) pass("virsh qemu:///system connection: OK");
  else fail("virsh qemu:///system connection: FAILED");

  // -- 6. Networks --
  console.log("\n── Libvirt Networks ──");

  let hasActiveNetwork = false;

  // nox-nat
  const noxNatInfo = virsh("net-info nox-nat", false);
  if (noxNatInfo.exitCode === 0) {
    const active = noxNatInfo.stdout.split("Active:")[1]?.split("\n")[0]?.includes("yes");
    const autostart = noxNatInfo.stdout.split("Autostart:")[1]?.split("\n")[0]?.includes("yes");
    if (active) { pass("nox-nat: active"); hasActiveNetwork = true; }
    else softWarn("nox-nat: exists but inactive — start with: virsh net-start nox-nat");
    if (autostart) pass("nox-nat: autostart enabled");
    else softWarn("nox-nat: autostart disabled");
    const netXml = virsh("net-dumpxml nox-nat", false);
    if (netXml.stdout.includes("mode='nat'") || netXml.stdout.includes("<nat>") || netXml.stdout.includes("<forward mode='nat'")) {
      pass("nox-nat: NAT mode configured");
    } else {
      softWarn("nox-nat: unexpected network mode");
    }
  } else {
    softWarn("nox-nat: not defined — run installer to create it");
    // fall back to default
    const defInfo = virsh("net-info default", false);
    if (defInfo.exitCode === 0 && defInfo.stdout.split("Active:")[1]?.split("\n")[0]?.includes("yes")) {
      pass("default network: active (fallback)");
      hasActiveNetwork = true;
    }
  }

  if (!hasActiveNetwork) fail("No active libvirt network — VMs cannot get connectivity");

  // IP forwarding
  try {
    const ipFwd = readFileSync("/proc/sys/net/ipv4/ip_forward", "utf-8").trim();
    if (ipFwd === "1") pass("IPv4 forwarding: enabled");
    else softWarn("IPv4 forwarding: disabled");
  } catch {
    softWarn("Cannot check IPv4 forwarding");
  }

  // -- 7. Permissions --
  console.log("\n── Permissions ──");

  // User groups
  const groups = run("id -nG", false).stdout.trim().split(/\s+/);
  for (const g of ["libvirt", "kvm"]) {
    if (groups.includes(g)) pass(`User in '${g}' group`);
    else softWarn(`User NOT in '${g}' group`);
  }

  // Home dir traversable
  const homePerms = run(`stat -c '%a' ${homedir()}`, false);
  if (homePerms.exitCode === 0) {
    const perms = parseInt(homePerms.stdout.trim());
    const otherX = perms % 10;
    if (otherX & 1) pass(`Home directory: traversable (mode ${perms})`);
    else fail(`Home directory: NOT traversable (mode ${perms}) — run: chmod 711 ${homedir()}`);
  }

  // polkit
  if (existsSync("/etc/polkit-1/rules.d/50-libvirt.rules")) pass("polkit libvirt rule: configured");
  else softWarn("polkit rule: not found — may need password for virsh");

  // -- 8. Storage --
  console.log("\n── Storage ──");

  if (existsSync(VMS_DIR)) pass(`VMs directory: ${VMS_DIR}`);
  else softWarn("VMs directory: will be created on first run");

  if (existsSync(IMAGES_DIR)) pass(`Images cache: ${IMAGES_DIR}`);
  else softWarn("Images cache: will be created on first run");

  // Disk space
  const dfResult = run(`df --output=avail -BG ${homedir()} 2>/dev/null | tail -1`, false);
  const availGb = parseInt(dfResult.stdout.trim());
  if (!isNaN(availGb)) {
    if (availGb >= 10) pass(`Available disk space: ${availGb}GB`);
    else if (availGb >= 2) softWarn(`Available disk space: ${availGb}GB (low)`);
    else fail(`Available disk space: ${availGb}GB (too low for VMs)`);
  }

  // -- 9. System Resources --
  console.log("\n── System Resources ──");

  const ramMb = hostRamMb();
  if (ramMb >= 2048) pass(`Total RAM: ${ramMb}MB`);
  else if (ramMb >= 1024) softWarn(`Total RAM: ${ramMb}MB (limited)`);
  else softWarn(`Total RAM: ${ramMb}MB (very limited)`);

  const cpuCount = run("nproc 2>/dev/null || echo 1", false).stdout.trim();
  pass(`CPU cores: ${cpuCount}`);
  pass(`Architecture: ${arch}`);

  // -- 10. Internet --
  console.log("\n── Internet Connectivity ──");

  const debianReach = run("curl -fsSL --connect-timeout 5 -o /dev/null https://cloud.debian.org 2>&1", false);
  if (debianReach.exitCode === 0) pass("cloud.debian.org: reachable (image downloads)");
  else softWarn("cloud.debian.org: unreachable — image downloads may fail");

  const ghReach = run("curl -fsSL --connect-timeout 5 -o /dev/null https://raw.githubusercontent.com 2>&1", false);
  if (ghReach.exitCode === 0) pass("GitHub: reachable (updates)");
  else softWarn("GitHub: unreachable — 'nox update' will fail");

  // -- 11. Smoke Tests --
  console.log("\n── Functional Smoke Tests ──");

  if (virsh("list --all", false).exitCode === 0) pass("virsh list: works");
  else fail("virsh list: FAILED");

  if (virsh("net-list --all", false).exitCode === 0) pass("virsh net-list: works");
  else fail("virsh net-list: FAILED");

  if (run("qemu-img --version", false).exitCode === 0) pass("qemu-img: works");
  else fail("qemu-img: FAILED");

  // Test cloud-init ISO creation
  const testDir = run("mktemp -d").stdout.trim();
  try {
    writeFileSync(join(testDir, "user-data"), "test");
    writeFileSync(join(testDir, "meta-data"), "test");
    const isoTest = run(`genisoimage -output ${testDir}/test.iso -volid cidata -joliet -rock ${testDir}/user-data ${testDir}/meta-data`, false);
    if (isoTest.exitCode === 0) pass("cloud-init ISO creation: works");
    else fail("cloud-init ISO creation: FAILED");
  } finally {
    rmSync(testDir, { recursive: true, force: true });
  }

  // -- Summary --
  console.log(`\n${"─".repeat(50)}`);
  if (errors === 0 && warnings === 0) {
    console.log("✓ All checks passed. nox is fully operational.");
  } else if (errors === 0) {
    console.log(`✓ All critical checks passed. ${warnings} warning(s) — review above.`);
  } else {
    console.error(`✗ ${errors} error(s) and ${warnings} warning(s). Fix errors before using nox.`);
  }
  console.log("");
}

function cmdUpdate() {
  console.log(`Current version: ${VERSION}`);
  console.log("Checking for updates from GitHub...");

  const arch = hostArch();
  const binaryName = `nox-linux-${arch}`;
  const tmpDir = run("mktemp -d").stdout.trim();
  try {
    const ts = Date.now();
    run(`curl -H 'Cache-Control: no-cache' -fsSL '${GITHUB_RAW_URL}/VERSION?t=${ts}' -o ${tmpDir}/VERSION`);

    const remoteVersion = readFileSync(`${tmpDir}/VERSION`, "utf-8").trim();
    console.log(`Latest version: ${remoteVersion}`);

    if (remoteVersion === VERSION) {
      console.log("✓ Already up to date!");
      cmdDoctor();
      return;
    }

    const releaseUrl = `https://github.com/solosmith/nox/releases/download/v${remoteVersion}`;
    console.log("Downloading updated binary...");
    run(`curl -fsSL -L '${releaseUrl}/${binaryName}' -o ${tmpDir}/nox`);

    console.log("Installing...");
    run(`sudo mkdir -p /usr/local/lib/nox`);
    run(`sudo rm -f /usr/local/bin/nox`);
    run(`sudo cp ${tmpDir}/nox /usr/local/bin/nox`);
    run(`sudo chmod +x /usr/local/bin/nox`);
    run(`sudo cp ${tmpDir}/VERSION /usr/local/lib/nox/VERSION`);

    console.log(`✓ nox updated successfully! (${VERSION} → ${remoteVersion})`);
    console.log("");
    cmdDoctor();
  } catch (e) {
    console.error(`Error updating nox: ${e}`);
    process.exit(1);
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function printHelp() {
  console.log(`nox ${VERSION} - Lightweight VM Manager

Usage: nox <command> [options]

Commands:
  create <name> [opts]   Create a new VM
  start <name>           Start a VM
  stop <name>            Stop a VM
  restart <name>         Restart a VM
  delete|rm <name>       Delete a VM
  list|ls                List all VMs
  status <name>          Show VM details
  stats <name>           Show CPU, RAM, disk usage & uptime
  ssh <name> [-- cmd]    SSH into VM
  serial <name>          Connect to VM serial console
  run <name> <command>   Run a command inside VM via SSH
  passwd <name>          Change SSH password
  resize <name> [opts]   Resize VM resources
  forward <name> <hp:vp> Forward host port to VM port
  update|up              Update nox
  doctor                 Run comprehensive system health checks

Create options:
  --os debian            Operating system (default: debian)
  --cpus N               CPU count, fractional or absolute (default: 1)
  --ram N                RAM in MB, fractional or absolute (default: 512)
  --disk N               Disk size in GB (default: 5)
  --network NAME         Libvirt network (default: auto-detect)
  --passwd PASSWORD       Set user password (default: auto-generated)
  --no-autostart         Disable autostart on boot
  --no-start             Create but don't start VM

Resize options:
  --cpus N               New CPU count
  --ram N                New RAM in MB
  --disk N               New disk size in GB (expand only)

Forward options:
  --proto tcp|udp        Protocol (default: tcp)
  --remove               Remove the forward rule
  --list                 List all forwards for a VM`);
}

async function main() {
  ensureDirs();

  const args = process.argv.slice(2);
  const command = args[0];
  const rest = args.slice(1);

  if (!command || command === "-h" || command === "--help") {
    printHelp();
    process.exit(command ? 0 : 1);
  }

  if (command === "--version" || command === "-v") {
    console.log(`nox ${VERSION}`);
    process.exit(0);
  }

  switch (command) {
    case "create":   cmdCreate(rest); break;
    case "start":    cmdStart(rest); break;
    case "stop":     cmdStop(rest); break;
    case "restart":  cmdRestart(rest); break;
    case "delete":
    case "rm":       cmdDelete(rest); break;
    case "list":
    case "ls":       cmdList(); break;
    case "status":   cmdStatus(rest); break;
    case "stats":    cmdStats(rest); break;
    case "ssh":      await cmdSsh(rest); break;
    case "serial":   await cmdSerial(rest); break;
    case "run":      cmdRun(rest); break;
    case "passwd":   cmdPasswd(rest); break;
    case "resize":   cmdResize(rest); break;
    case "forward":  cmdForward(rest); break;
    case "update":
    case "up":       cmdUpdate(); break;
    case "doctor":   cmdDoctor(); break;
    default:
      console.error(`Unknown command: ${command}`);
      process.exit(1);
  }
}

main();
