#!/usr/bin/env python3
"""Run the UNMODIFIED client script on Linux nandsim, only on disposable CI.

Requires root, a static BusyBox, nandsim/mtdblock kernel modules and no existing
MTD devices. This is NOT a Beryl firmware emulator. Hardware identity files and
the Factory label are fixtures; all shell utilities and NAND I/O are real.
"""
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SIZE = 2 * 1024 * 1024
OFFSET = 0x88


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def fixture(prefix=255, duplicate=False, markers=True):
    data = bytearray(b'\xa5' * SIZE)
    if markers:
        data[96:105] = b'firsttest'
        data[105:115] = b'secondtest'
    data[OFFSET-1:OFFSET+2] = bytes([prefix]) + b'CN'
    if duplicate:
        data[499:502] = b'\x00CN'
    return bytes(data)


class Stand:
    def __init__(self, directory, mtd):
        self.base = Path(directory)
        self.root = self.base / 'root'
        self.mtd = mtd
        self.block = Path('/dev/mtdblock' + mtd.name[3:])
        self.char = Path('/dev/' + mtd.name)
        self.results = []
        self.script = (ROOT / 'beryl-region.sh').read_bytes()
        for name in ('bin', 'tmp/sysinfo', 'etc', 'proc', 'dev', 'root'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        busybox = shutil.which('busybox')
        shutil.copyfile(busybox, self.root / 'bin/busybox')
        (self.root / 'bin/busybox').chmod(0o755)
        # Relative links resolve to the static binary inside the chroot.
        for app in run(busybox, '--list').splitlines():
            if app != 'busybox':
                (self.root / 'bin' / app).symlink_to('busybox')
        # Do not make reboot/poweroff available in the test environment.
        for app in ('reboot', 'poweroff', 'halt'):
            (self.root / 'bin' / app).unlink(missing_ok=True)
        os.mknod(self.root / 'dev/null', stat.S_IFCHR | 0o666, os.makedev(1, 3))
        os.mknod(self.root / 'dev' / self.block.name, stat.S_IFBLK | 0o600, self.block.stat().st_rdev)
        (self.root / 'beryl-region.sh').write_bytes(self.script)
        assert (self.root / 'beryl-region.sh').read_bytes() == self.script
        self.board('glinet,mt3000-snand')
        (self.root / 'etc/glversion').write_text('SIMULATED-ENVIRONMENT-NOT-GLINET-FIRMWARE\n')
        # nandsim's generated partition name is replaced by a router-like label;
        # its real index, size, erasesize and device major/minor are preserved.
        self.table()

    def table(self, duplicates=False):
        size = int((self.mtd / 'size').read_text())
        erase = int((self.mtd / 'erasesize').read_text())
        line = f'{self.mtd.name}: {size:08x} {erase:08x} "Factory"\n'
        (self.root / 'proc/mtd').write_text('dev: size erasesize name\n' + line * (2 if duplicates else 1))

    def board(self, name):
        (self.root / 'tmp/sysinfo/board_name').write_text(name + '\n')

    def read_nand(self):
        # Character MTD interface, independent of mtdblock's read cache.
        with self.char.open('rb', buffering=0) as f:
            data = f.read(SIZE)
        assert len(data) == SIZE
        return data

    def seed(self, data):
        assert len(data) == SIZE
        p = self.base / 'seed.img'
        p.write_bytes(data)
        run('dd', 'if=' + str(p), 'of=' + str(self.block), 'bs=131072', 'conv=fsync')
        run('blockdev', '--flushbufs', str(self.block))
        assert self.read_nand() == data, 'NAND initialization did not persist'
        shutil.rmtree(self.root / 'root/beryl-region-backups', ignore_errors=True)

    def invoke(self, responses=(), check=False):
        master, slave = pty.openpty()
        cmd = ['chroot', str(self.root), '/bin/sh', '/beryl-region.sh', '--check' if check else '--country=US']
        env = dict(os.environ, PATH='/bin', LC_ALL='C', SSH_CONNECTION='192.0.2.2 50000 192.0.2.1 22')
        # Resolve the host's chroot before PATH is restricted for the guest.
        cmd[0] = shutil.which('chroot')
        proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        output = b''
        sent = 0
        prompts = 0
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        block = os.read(master, 65536)
                    except OSError:
                        break
                    if not block:
                        break
                    output += block
                    # Count across read boundaries; never depend on PTY chunking.
                    seen = output.count(b'Enter: ')
                    while prompts < seen:
                        if sent < len(responses):
                            os.write(master, responses[sent].encode() + b'\n')
                            sent += 1
                        prompts += 1
                elif proc.poll() is not None:
                    break
            proc.wait(timeout=2)
        except BaseException:
            print(output.decode(errors='replace'), flush=True)
            raise
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            os.close(master)
        text = output.decode(errors='replace')
        if proc.returncode != 0:
            print(text, flush=True)
        return proc.returncode, text

    def record(self, name):
        assert (self.root / 'beryl-region.sh').read_bytes() == self.script
        self.results.append(name)
        print('PASS: ' + name, flush=True)

    def test(self):
        for prefix in (0, 255):
            original = fixture(prefix)
            self.seed(original)
            rc, text = self.invoke(check=True)
            assert rc == 0 and self.read_nand() == original, text
            self.record(f'check does not write, prefix {prefix}')

            rc, text = self.invoke([''])
            assert rc == 0 and self.read_nand() == original, text
            assert not list((self.root / 'root').glob('beryl-region-backups/*/factory-original.img'))
            self.record(f'cancel before backup, prefix {prefix}')

            rc, text = self.invoke(['START', ''])
            assert rc == 0 and self.read_nand() == original, text
            copies = list((self.root / 'root').glob('beryl-region-backups/*/factory-original.img'))
            assert len(copies) == 1 and copies[0].read_bytes() == original, text
            assert oct(copies[0].stat().st_mode & 0o777) == '0o600'
            self.record(f'cancel after verified backup, prefix {prefix}')

            rc, text = self.invoke(['START', 'SAVED', ''])
            expected = original[:OFFSET] + b'US' + original[OFFSET+2:]
            assert rc == 0 and 'Сброс отложен' in text, text
            # Flush/invalidate the block device and verify through char MTD.
            run('blockdev', '--flushbufs', str(self.block))
            assert self.read_nand() == expected, 'NAND differs outside expected two bytes'
            assert copies[0].read_bytes() == original
            assert not (self.root / 'tmp/beryl-region.lock').exists()
            self.record(f'exact two-byte persistent NAND change, prefix {prefix}')

            rc, text = self.invoke(check=True)
            assert rc != 0 and 'не найдена' in text and self.read_nand() == expected, text
            self.record(f'repeated run refuses without writes, prefix {prefix}')

        for data, expected_message, name in (
            (fixture(duplicate=True), 'несколько', 'ambiguous country signatures'),
            (fixture(markers=False), 'не распознана', 'unknown factory structure'),
        ):
            self.seed(data)
            rc, text = self.invoke(check=True)
            assert rc != 0 and expected_message in text and self.read_nand() == data, text
            self.record(name)

        original = fixture()
        self.seed(original)
        self.board('glinet,gl-mt6000')
        rc, text = self.invoke(check=True)
        assert rc != 0 and 'не поддерживается' in text and self.read_nand() == original, text
        self.record('other model refuses before writes')
        self.board('glinet,mt3000-snand')
        self.table(duplicates=True)
        rc, text = self.invoke(check=True)
        assert rc != 0 and 'несколько разделов' in text and self.read_nand() == original, text
        self.record('ambiguous Factory partition refuses')
        return {'passed': len(self.results), 'cases': self.results, 'script_sha256': hashlib.sha256(self.script).hexdigest(), 'kernel': os.uname().release, 'nand_size': SIZE, 'erase_size': int((self.mtd/'erasesize').read_text()), 'limits': 'Synthetic Factory, simulated identity, upstream Linux NAND, native x86 BusyBox; no GL.iNet firmware, real reset, Wi-Fi or MT7981 emulation.'}


def main():
    if os.environ.get('BERYL_EPHEMERAL_NAND_TEST') != '1' or os.geteuid() != 0:
        raise SystemExit('Run only as root in a disposable CI VM with BERYL_EPHEMERAL_NAND_TEST=1')
    if list(Path('/sys/class/mtd').glob('mtd[0-9]*')):
        raise SystemExit('Refusing: pre-existing MTD devices are present')
    loaded = False
    try:
        run('modprobe', 'mtdblock')
        run('modprobe', 'nandsim', 'first_id_byte=0x20', 'second_id_byte=0xaa', 'third_id_byte=0x00', 'fourth_id_byte=0x15', 'parts=16')
        loaded = True
        if shutil.which('udevadm'):
            run('udevadm', 'settle')
        candidates = [p for p in Path('/sys/class/mtd').glob('mtd[0-9]*') if not p.name.endswith('ro') and (p/'name').exists() and 'NAND simulator' in (p/'name').read_text() and int((p/'size').read_text()) == SIZE]
        assert len(candidates) == 1, f'Expected exactly one 2 MiB nandsim partition: {candidates}'
        with tempfile.TemporaryDirectory(prefix='beryl-nandsim-') as temp:
            stand = Stand(temp, candidates[0])
            report = stand.test()
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        if loaded:
            # Detach the block translation layer first. Use leaf rmmod calls:
            # BusyBox modprobe -r also tries shared dependencies still in use.
            run('rmmod', 'mtdblock')
            run('rmmod', 'nandsim')


if __name__ == '__main__':
    main()
