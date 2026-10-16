#!/usr/bin/env python3
"""Boot QEMU guest with no disks, network, or host filesystem mounts."""
from pathlib import Path
import os
import subprocess

BASE = Path(__file__).resolve().parents[1] / '.nand-vm'
accel = 'kvm' if os.access('/dev/kvm', os.R_OK | os.W_OK) else 'tcg'
print('QEMU acceleration:', accel, flush=True)
command = ['qemu-system-x86_64', '-machine', 'q35,accel='+accel, '-cpu', 'host' if accel == 'kvm' else 'max', '-m', '768', '-smp', '2', '-nographic', '-no-reboot', '-nic', 'none', '-kernel', str(BASE/'vmlinuz'), '-initrd', str(BASE/'initramfs.cpio.gz'), '-append', 'console=ttyS0 quiet panic=-1 random.trust_cpu=on rdinit=/init']
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
text = result.stdout.decode(errors='replace')
print(text)
if result.returncode != 0 or 'BERYL_NAND_SUITE_SUCCESS' not in text or 'BERYL_NAND_SUITE_FAILURE' in text:
    raise SystemExit('NAND VM validation failed')
