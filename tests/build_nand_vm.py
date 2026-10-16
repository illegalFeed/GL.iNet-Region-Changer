#!/usr/bin/env python3
"""Build a disposable VM using an official Debian kernel, not GL.iNet firmware."""
import gzip
import hashlib
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.nand-vm'
DEBIAN = 'https://deb.debian.org/debian/'


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def main():
    if os.geteuid() != 0:
        raise SystemExit('root is required to create guest device nodes')
    BASE.mkdir(exist_ok=True)
    index = lzma.decompress(fetch(DEBIAN + 'dists/bookworm/main/binary-amd64/Packages.xz')).decode()
    packages = {}
    for stanza in index.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in stanza.splitlines() if ': ' in line and not line.startswith(' '))
        if fields.get('Package', '').startswith('linux-image-'):
            packages[fields['Package']] = fields
    meta = packages['linux-image-amd64']
    name = re.search(r'linux-image-[0-9][a-zA-Z0-9.+~-]*-amd64', meta['Depends']).group()
    package = packages[name]
    print('Official Debian kernel package:', name, package['Version'], flush=True)
    data = fetch(DEBIAN + package['Filename'])
    assert hashlib.sha256(data).hexdigest() == package['SHA256'], 'Debian kernel package SHA-256 mismatch'
    deb = BASE / 'kernel.deb'
    deb.write_bytes(data)
    unpack = BASE / 'kernel-package'
    run('dpkg-deb', '-x', str(deb), str(unpack))
    version = next((unpack / 'lib/modules').iterdir()).name
    modules = unpack / 'lib/modules' / version
    assert list(modules.rglob('nandsim.ko*')), 'The generic kernel has no nandsim module'
    run('depmod', '-b', str(unpack), version)
    root = BASE / 'root'
    root.mkdir(exist_ok=True)
    for part in ('bin', 'usr/bin', 'dev', 'dev/pts', 'proc', 'sys', 'tmp', 'tests'):
        (root / part).mkdir(parents=True, exist_ok=True)
    busybox = shutil.which('busybox')
    shutil.copyfile(busybox, root / 'bin/busybox')
    (root / 'bin/busybox').chmod(0o755)
    for app in run(busybox, '--list').splitlines():
        if app != 'busybox':
            (root / 'bin' / app).symlink_to('busybox')
    for dev, mode, major, minor in (('console', 0o600, 5, 1), ('null', 0o666, 1, 3)):
        os.mknod(root / 'dev' / dev, stat.S_IFCHR | mode, os.makedev(major, minor))
    python = Path('/usr/bin/python3').resolve()
    shutil.copyfile(python, root / 'usr/bin' / python.name)
    (root / 'usr/bin' / python.name).chmod(0o755)
    (root / 'usr/bin/python3').symlink_to(python.name)
    py_version = run(str(python), '-c', 'import sys;print("%d.%d"%sys.version_info[:2])').strip()
    stdlib = Path('/usr/lib/python' + py_version)
    shutil.copytree(stdlib, root / stdlib.relative_to('/'), ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
    for binary in [python, *stdlib.rglob('*.so')]:
        result = subprocess.run(['ldd', str(binary)], text=True, capture_output=True)
        if result.returncode != 0:
            continue
        for lib in re.findall(r'(?:=>\s*)?(/[^\s()]+)', result.stdout):
            source = Path(lib)
            dest = root / source.relative_to('/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(source, dest)
    for target in ('nandsim', 'mtdblock'):
        deps = run('modprobe', '-d', str(unpack), '-S', version, '--show-depends', target)
        for path in re.findall(r'^insmod (\S+)', deps, re.M):
            src = Path(path)
            dest = root / src.relative_to(unpack)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix == '.xz':
                dest.with_suffix('').write_bytes(lzma.decompress(src.read_bytes()))
            elif src.suffix == '.zst':
                dest.with_suffix('').write_bytes(subprocess.check_output(['zstd', '-dc', str(src)]))
            else:
                shutil.copyfile(src, dest)
    guest_modules = root / 'lib/modules' / version
    for name in ('modules.builtin', 'modules.builtin.modinfo', 'modules.order'):
        if (modules / name).exists():
            shutil.copyfile(modules / name, guest_modules / name)
    run('depmod', '-b', str(root), version)
    shutil.copyfile(next((unpack / 'boot').glob('vmlinuz-*')), BASE / 'vmlinuz')
    shutil.copyfile(ROOT / 'beryl-region.sh', root / 'beryl-region.sh')
    shutil.copyfile(ROOT / 'tests/nand_integration.py', root / 'tests/nand_integration.py')
    (root / 'init').write_text('''#!/bin/sh
export PATH=/bin:/usr/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
export BERYL_EPHEMERAL_NAND_TEST=1
if /usr/bin/python3 /tests/nand_integration.py; then
    echo BERYL_NAND_SUITE_SUCCESS
else
    echo BERYL_NAND_SUITE_FAILURE
fi
sync
poweroff -f
''')
    (root / 'init').chmod(0o755)
    names = ['.', *[str(p.relative_to(root)) for p in root.rglob('*')]]
    archive = subprocess.run(['cpio', '--null', '-o', '-H', 'newc', '--owner=0:0'], cwd=root, input=('\0'.join(names)+'\0').encode(), capture_output=True, check=True).stdout
    (BASE / 'initramfs.cpio.gz').write_bytes(gzip.compress(archive, compresslevel=1))
    print('Guest kernel:', version, 'initramfs bytes:', (BASE/'initramfs.cpio.gz').stat().st_size, flush=True)


if __name__ == '__main__':
    main()
