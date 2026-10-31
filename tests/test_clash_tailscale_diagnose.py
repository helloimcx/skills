import os
import pty
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = (Path(__file__).parents[1] / "clash-tailscale-coexist/scripts/diagnose_and_fix.sh")


class DiagnoseFixSafetyTests(unittest.TestCase):
    def test_fix_requires_explicit_confirmation_and_prints_rollback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            self._stub(bin_dir, "tailscale", f'''#!/bin/sh
if [ "$1" = status ]; then exit 0; fi
echo "tailscale $*" >> "{calls}"
''')
            self._stub(bin_dir, "scutil", '''#!/bin/sh
cat <<'EOF'
resolver #1
  nameserver[0] : 100.100.100.100
EOF
''')
            self._stub(bin_dir, "networksetup", f'''#!/bin/sh
if [ "$1" = -getinfo ]; then echo "IPv6: Automatic"; exit 0; fi
echo "networksetup $*" >> "{calls}"
''')
            self._stub(bin_dir, "ifconfig", "#!/bin/sh\nexit 0\n")
            self._stub(bin_dir, "netstat", "#!/bin/sh\nexit 0\n")
            self._stub(bin_dir, "curl", "#!/bin/sh\necho 200\n")

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            process, stdout, stderr = self._run(env, "no\nno\n")

            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse(calls.exists(), "declined fixes must not run system changes")
            self.assertIn("恢复命令", stdout)
            self.assertIn("tailscale set --accept-dns=true", stdout)
            self.assertIn("networksetup -setv6automatic Wi-Fi", stdout)

            process, stdout, stderr = self._run(env, "yes\nyes\n")
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(calls.exists(), stdout)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                ["tailscale set --accept-dns=false", "networksetup -setv6off Wi-Fi"],
                stdout,
            )

    @staticmethod
    def _run(env, answers):
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["bash", str(SCRIPT), "--fix"], stdin=slave_fd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        os.close(slave_fd)
        time.sleep(0.1)
        os.write(master_fd, answers.encode())
        stdout, stderr = process.communicate(timeout=10)
        os.close(master_fd)
        return process, stdout, stderr

    @staticmethod
    def _stub(bin_dir, name, contents):
        path = bin_dir / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
