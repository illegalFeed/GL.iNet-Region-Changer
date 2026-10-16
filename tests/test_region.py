"""No router, network, root, or external Python packages required.

Source production functions without the entry point. Main-flow tests replace
only hardware paths/checks, then run the real workflow in an isolated PTY.
"""
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import tempfile
import time
import unittest
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'beryl-region.sh').read_text()
FUNCTIONS = SOURCE.rsplit('\nmain "$@"', 1)[0]
SHELL = shlex.split(os.environ.get('TEST_SHELL', 'sh'))


def image(prefix=255, offsets=(300,), size=65536):
    data = bytearray(b'\xa5' * size)
    data[40:49] = b'firsttest'
    data[70:80] = b'secondtest'
    for offset in offsets:
        data[offset-1:offset+2] = bytes([prefix]) + b'CN'
    return bytes(data)


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.original = self.work / 'original.img'
        self.device = self.base / 'device.img'
        self.original.write_bytes(image())
        self.device.write_bytes(image())
        self.backup = self.base / 'persistent-backup.img'
        self.backup.write_bytes(image())

    def run_core(self, body, country='RU'):
        q = shlex.quote
        code = FUNCTIONS + '\n' + f'''
WORK={q(str(self.work))}
DEVICE={q(str(self.device))}
EXPECTED_SIZE=65536
COUNTRY={q(country)}
BACKUP={q(str(self.backup))}
''' + body
        return subprocess.run(SHELL, input=code, text=True, capture_output=True)

    def test_exact_two_bytes_for_both_prefixes_all_countries(self):
        for prefix in (0, 255):
            for country in ('RU', 'US', 'DE', 'GB'):
                with self.subTest(prefix=prefix, country=country):
                    original = image(prefix=prefix)
                    self.original.write_bytes(original)
                    self.device.write_bytes(original)
                    r = self.run_core('plan_image "$WORK/original.img"; prepare_image "$WORK/original.img"; write_region', country)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    expected = original[:300] + country.encode() + original[302:]
                    self.assertEqual(self.device.read_bytes(), expected)
                    self.assertEqual(self.original.read_bytes(), original)

    def test_duplicate_same_and_mixed_prefixes_refused(self):
        for mixed in (False, True):
            data = bytearray(image(offsets=(300, 600)))
            if mixed:
                data[599] = 0
            self.original.write_bytes(data)
            r = self.run_core('plan_image "$WORK/original.img"')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('несколько', r.stderr)

    def test_missing_cn_and_already_changed_refused(self):
        for value in (b'US', b'RU', b'XX'):
            data = bytearray(image())
            data[300:302] = value
            self.original.write_bytes(data)
            r = self.run_core('plan_image "$WORK/original.img"')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('не найдена', r.stderr)

    def test_hex_half_byte_false_positive_refused(self):
        data = bytearray(image(offsets=()))
        # Hex contains FF434E at an odd nibble boundary, not at a byte boundary.
        data[299:303] = bytes.fromhex('aff434e5')
        self.original.write_bytes(data)
        r = self.run_core('plan_image "$WORK/original.img"')
        self.assertNotEqual(r.returncode, 0)

    def test_missing_factory_markers_refused(self):
        for span in ((40,49), (70,80)):
            data = bytearray(image())
            data[span[0]:span[1]] = b'x' * (span[1]-span[0])
            self.original.write_bytes(data)
            self.assertNotEqual(self.run_core('plan_image "$WORK/original.img"').returncode, 0)

    def test_beginning_end_and_od_line_boundary(self):
        for offset in (1, 16, 17, 65534):
            self.original.write_bytes(image(offsets=(offset,)))
            r = self.run_core('plan_image "$WORK/original.img"; printf "OFFSET=%s" "$OFFSET"')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f'OFFSET={offset}', r.stdout)

    def test_truncated_snapshot_refused(self):
        self.device.write_bytes(b'short')
        r = self.run_core('snapshot "$WORK/read.img"')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('не полностью', r.stderr)

    def test_changed_factory_before_write_refused(self):
        data = bytearray(image()); data[1000] = 0
        self.device.write_bytes(data)
        r = self.run_core('plan_image "$WORK/original.img"; prepare_image "$WORK/original.img"; write_region')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.device.read_bytes(), data)
        self.assertIn('Запись отменена', r.stderr)

    def test_write_failure_preserves_backup_no_reset(self):
        r = self.run_core('''
trap cleanup 0
plan_image "$WORK/original.img"
prepare_image "$WORK/original.img"
dd() {
    case "$*" in *"of=$DEVICE"*) return 1 ;; *) command dd "$@" ;; esac
}
write_region
''')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Не сбрасывайте', r.stderr)
        self.assertEqual(self.device.read_bytes(), image())
        self.assertEqual(self.backup.read_bytes(), image())

    def test_silent_write_failure_detected(self):
        r = self.run_core('''
plan_image "$WORK/original.img"
prepare_image "$WORK/original.img"
dd() {
    case "$*" in *"of=$DEVICE"*) return 0 ;; *) command dd "$@" ;; esac
}
write_region
''')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('отличается', r.stderr)

    def test_bad_arguments_refused_before_hardware(self):
        for arg in ('--country=XX', '--country=ru', '--country=RUS', '--yes', '--reset'):
            r = subprocess.run(SHELL + [str(ROOT / 'beryl-region.sh'), arg], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Ошибка', r.stderr)

    def workflow(self, responses, check=False, reset_fails=False):
        # All router-specific filesystem paths point inside this test's tempdir.
        functions = FUNCTIONS.replace('/root/beryl-region-backups', str(self.base / 'backups'))
        functions = functions.replace('/tmp/beryl-region.lock', str(self.base / 'lock'))
        functions = functions.replace('/tmp/beryl-region.XXXXXX', str(self.base / 'runtime.XXXXXX'))
        code = functions + f'''
hardware() {{ DEVICE={shlex.quote(str(self.device))}; EXPECTED_SIZE=65536; FIRMWARE=test; }}
firstboot() {{ printf reset >> {shlex.quote(str(self.base / 'reset.log'))}; return {1 if reset_fails else 0}; }}
reboot() {{ printf reboot >> {shlex.quote(str(self.base / 'reset.log'))}; }}
main {'--check' if check else '--country=RU'}
'''
        entry = self.base / 'test-entry.sh'
        entry.write_text(code)
        master, slave = pty.openpty()
        proc = subprocess.Popen(SHELL + [str(entry)], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        out = b''
        sent = 0
        deadline = time.monotonic() + 10
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
                    out += block
                    if b'Enter: ' in block and sent < len(responses):
                        os.write(master, responses[sent].encode() + b'\n')
                        sent += 1
                if proc.poll() is not None and not ready:
                    break
            proc.wait(timeout=1)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            os.close(master)
        return proc.returncode, out.decode(errors='replace')

    def test_workflow_cancel_before_backup(self):
        rc, out = self.workflow([''])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.device.read_bytes(), image())
        self.assertFalse((self.base / 'backups').exists())
        self.assertFalse((self.base / 'reset.log').exists())

    def test_workflow_cancel_after_backup(self):
        rc, out = self.workflow(['START', ''])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.device.read_bytes(), image())
        copies = list((self.base / 'backups').glob('*/factory-original.img'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), image())
        self.assertFalse((self.base / 'reset.log').exists())

    def test_workflow_change_then_defer_reset(self):
        rc, out = self.workflow(['START', 'SAVED', ''])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.device.read_bytes()[300:302], b'RU')
        self.assertIn('Сброс отложен', out)
        self.assertFalse((self.base / 'reset.log').exists())
        self.assertFalse((self.base / 'lock').exists())

    def test_workflow_reset_only_after_explicit_confirmation(self):
        rc, out = self.workflow(['START', 'SAVED', 'RESET'])
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.base / 'reset.log').read_text(), 'resetreboot')

    def test_workflow_check_does_not_write_or_backup(self):
        rc, out = self.workflow([], check=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.device.read_bytes(), image())
        self.assertFalse((self.base / 'backups').exists())
        self.assertIn('Ничего не изменено', out)

    def test_reset_failure_never_reboots(self):
        rc, out = self.workflow(['START', 'SAVED', 'RESET'], reset_fails=True)
        self.assertNotEqual(rc, 0, out)
        self.assertEqual((self.base / 'reset.log').read_text(), 'reset')
        self.assertEqual(self.device.read_bytes()[300:302], b'RU')

    def test_hardware_aliases_and_partition_table(self):
        fixture = self.base / 'hardware'
        fixture.mkdir()
        board = fixture / 'board'
        firmware = fixture / 'glversion'
        mtd = fixture / 'mtd'
        firmware.write_text('4.8.3\n')
        mtd.write_text('dev: size erasesize name\nmtd0: 00040000 00020000 "BL2"\nmtd2: 00200000 00020000 "Factory"\n')
        # Only substitute hardware paths and the kernel block-device predicate.
        functions = FUNCTIONS.replace('/tmp/sysinfo/board_name', str(board)).replace('/etc/glversion', str(firmware)).replace('/proc/mtd', str(mtd))
        functions = functions.replace('[ -b "$DEVICE" ] && [ -r "$DEVICE" ]', 'true')
        def run():
            return subprocess.run(SHELL, input=functions + '\nid() { printf 0; }\nhardware\nprintf "DEVICE=%s SIZE=%s" "$DEVICE" "$EXPECTED_SIZE"', text=True, capture_output=True)
        for alias in ('glinet,gl-mt3000', 'glinet,mt3000-snand', 'gl-mt3000'):
            board.write_text(alias)
            r = run()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('DEVICE=/dev/mtdblock2 SIZE=2097152', r.stdout)
        board.write_text('glinet,gl-mt6000')
        self.assertNotEqual(run().returncode, 0)
        board.write_text('glinet,mt3000-snand')
        mtd.write_text('mtd2: 00200000 00020000 "Factory"\nmtd3: 00200000 00020000 "factory"\n')
        self.assertNotEqual(run().returncode, 0)
        mtd.write_text('mtd2: 00200000 00020000 "NotFactory"\n')
        self.assertNotEqual(run().returncode, 0)


class DownloadCommandTests(unittest.TestCase):
    def test_success_bad_checksum_and_http_failure(self):
        readme = (ROOT / 'README.md').read_text()
        command = next(x for x in re.findall(r'```sh\n(.*?)\n```', readme, re.S) if x.startswith('d=$'))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / 'bin'
            bindir.mkdir()
            # The downloader is simulated; checksum verification is real.
            (bindir / 'curl').write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
if os.environ.get('SIMULATE_HTTP_FAILURE') == '1': sys.exit(22)
Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(Path(os.environ['PAYLOAD']).read_bytes())
''')
            (bindir / 'sh').write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ['LAUNCHED']).write_text('yes')
''')
            for p in bindir.iterdir():
                p.chmod(0o700)
            wrong = base / 'wrong.sh'
            wrong.write_text('exit 0\n')
            for payload, http_failure, should_launch in ((ROOT / 'beryl-region.sh', False, True), (wrong, False, False), (ROOT / 'beryl-region.sh', True, False)):
                launched = base / 'launched'
                launched.unlink(missing_ok=True)
                env = dict(os.environ, PATH=str(bindir)+os.pathsep+os.environ['PATH'], PAYLOAD=str(payload), LAUNCHED=str(launched), SIMULATE_HTTP_FAILURE=str(int(http_failure)))
                local_command = command.replace('/tmp/beryl-start.XXXXXX', str(base / 'download.XXXXXX'))
                r = subprocess.run(['/bin/sh', '-c', local_command], env=env, capture_output=True, text=True)
                self.assertEqual(launched.exists(), should_launch, r.stderr)
                self.assertEqual(r.returncode == 0, should_launch)


if __name__ == '__main__':
    unittest.main(verbosity=2)
