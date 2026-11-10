#!/usr/bin/env python3
"""Real tar.xz preflight; pkgtools/network are never executed, even on failure."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php"
RULE = Path(__file__).with_name("plugin_hid2hci.rules").read_bytes()
PIN = "8d4ed90eae12d0c0cac3c65c1960fb90c3f3fde264696bbe30f590a8cd4aa2ed"
PAM = Path(__file__).with_name("plugin_screen.pam").read_bytes()
PAM_PIN = "4e631aaf91babb312ec39a09d6cfd6e0a57d8ac5a48eb58a9bade25af3a937aa"
PHP_HARNESS = r'''
require $argv[1];
$nativeCalls = 0;
$runner = function($arguments,$capture) use (&$nativeCalls) {
    if ($arguments[0] !== '/bin/tar') {
        $nativeCalls++;
        throw new RuntimeException('ReachedNativeInstall');
    }
    $arguments[0] = getenv('NERDTOOLS_TEST_TAR');
    $process = proc_open($arguments,[0=>['file','/dev/null','r'],1=>['pipe','w'],2=>['file','/dev/null','a']],$pipes,null,['LC_ALL'=>'C','PATH'=>getenv('PATH')]);
    if (!is_resource($process)) throw new RuntimeException('Cannot inspect fixture');
    $contents = stream_get_contents($pipes[1]); fclose($pipes[1]);
    if (proc_close($process) !== 0) throw new RuntimeException('Fixture tar read failed');
    return $capture === 'raw' ? $contents : explode("\n",rtrim($contents,"\n"));
};
$network = function() { throw new RuntimeException('Network must not be used'); };
$manager = new NerdTools($argv[2],$network,$runner);
try {
    $manager->apply(false,false);
    throw new RuntimeException('Unexpected install completion');
} catch (RuntimeException $error) {
    $expected = $argv[3] === 'allowed' ? 'ReachedNativeInstall' : $argv[4];
    $expectedCalls = $argv[3] === 'allowed' ? 1 : 0;
    if (strpos($error->getMessage(),$expected) === false || $nativeCalls !== $expectedCalls) {
        fwrite(STDERR,$error->getMessage()."; native calls=".$nativeCalls."\n"); exit(1);
    }
}
if (file_exists($argv[2].'/'.$argv[5]) || (substr($argv[5],-4) === '.new' && file_exists($argv[2].'/'.substr($argv[5],0,-4)))) {
    fwrite(STDERR,"Archive preflight wrote a host integration file\n"); exit(1);
}
'''

class UdevArchivePolicy(unittest.TestCase):
    def inspect(self, rule, allowed, tool="bluez", member_path="etc/udev/rules.d/97-hid2hci.rules", error="Unrecognized BlueZ hid2hci rule"):
        php = os.environ.get("NERDTOOLS_TEST_PHP") or shutil.which("php")
        tar = os.environ.get("NERDTOOLS_TEST_TAR") or shutil.which("gtar") or shutil.which("tar")
        self.assertTrue(php and tar, "PHP and GNU tar are required")
        version = subprocess.run([tar, "--version"], capture_output=True, text=True, check=True)
        self.assertIn("GNU tar", version.stdout, "Set NERDTOOLS_TEST_TAR to GNU tar")
        with tempfile.TemporaryDirectory(prefix="nerdtools-real-archive-") as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/unraid-version").write_text('version="7.3.4"\n')
            state = root / "boot/config/plugins/NerdTools"
            (state / "packages").mkdir(parents=True)
            version = "5.88" if tool == "bluez" else "5.0.2"
            filename = f"nerdtools-{tool}-{version}-x86_64-1.txz"
            archive = state / "packages" / filename
            payloads = [(member_path, rule, 0o644), (f"opt/nerdtools/bin/{tool}", b"#!/bin/sh\nexit 0\n", 0o755)]
            if member_path.endswith(".new"):
                handler = (b'config() {\n  NEW="$1"\n  OLD="${NEW%.new}"\n'
                           b'  if [ ! -e "$OLD" ] && [ ! -L "$OLD" ]; then mv "$NEW" "$OLD"; elif cmp -s "$OLD" "$NEW"; then rm "$NEW"; fi\n}\n'
                           + b"config " + member_path.encode() + b"\n")
                payloads.append(("install/doinst.sh", handler, 0o644))
            with tarfile.open(archive, "w:xz", format=tarfile.GNU_FORMAT) as package:
                for path, data, mode in payloads:
                    member = tarfile.TarInfo(path)
                    member.uid = member.gid = 0
                    member.mode = mode
                    member.mtime = 1767225600
                    member.size = len(data)
                    package.addfile(member, io.BytesIO(data))
            data = archive.read_bytes()
            record = {"name": "nerdtools-" + tool, "tool": tool, "version": version, "file": filename,
                      "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "dependencies": [], "commands": []}
            asset = {"name": filename, "size": len(data), "browser_download_url":
                     "https://github.com/andrebrait/unRAID-NerdTools/releases/download/test/" + filename}
            (state / "catalog-cache.json").write_text(json.dumps({"catalog": {"family": "7.3", "min_unraid": "7.3.4", "packages": [record]},
                                                                 "assets": [asset], "release": "test"}))
            (state / "NerdTools.cfg").write_text(f'nerdtools-{tool}="yes"\n')
            result = subprocess.run([php, "-r", PHP_HARNESS, str(HELPERS), str(root), "allowed" if allowed else "denied", error, member_path],
                                    capture_output=True, text=True, env={**os.environ, "NERDTOOLS_TEST_TAR": tar})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_known_pinned_rule_reaches_install_boundary_without_executing_pkgtools(self):
        self.assertEqual(hashlib.sha256(RULE).hexdigest(), PIN)
        self.inspect(RULE, allowed=True)

    def test_catalog_checksum_cannot_authorize_crafted_root_run_rule(self):
        self.inspect(RULE + b'\nACTION=="add", RUN+="/bin/sh -c touch /root/compromised"\n', allowed=False)

    def test_pinned_screen_pam_preserves_standard_host_authentication(self):
        self.assertEqual(hashlib.sha256(PAM).hexdigest(), PAM_PIN)
        self.inspect(PAM, allowed=True, tool="screen", member_path="etc/pam.d/screen.new")

    def test_catalog_checksum_cannot_authorize_screen_pam_bypass(self):
        self.inspect(b"auth sufficient pam_permit.so\n", allowed=False, tool="screen",
                     member_path="etc/pam.d/screen.new", error="Unrecognized Screen PAM policy")

if __name__ == "__main__":
    unittest.main()
