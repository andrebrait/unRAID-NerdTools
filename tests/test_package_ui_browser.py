import os
import pathlib
import shutil
import signal
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'package_ui_browser.html'


class PackageUIBrowserTests(unittest.TestCase):
    def test_selector_driven_styles_in_chrome(self):
        browser = os.environ.get('CHROME_BIN')
        if not browser:
            browser = next((path for name in ('google-chrome', 'chromium', 'chromium-browser')
                            if (path := shutil.which(name))), None)
        self.assertIsNotNone(browser, 'Chrome or Chromium is required for the package UI browser gate')
        process = subprocess.Popen([
            browser,
            '--headless=new',
            '--no-sandbox',
            '--disable-gpu',
            '--allow-file-access-from-files',
            '--virtual-time-budget=1000',
            '--dump-dom',
            FIXTURE.as_uri(),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
        self.assertIn('<title>PASS</title>', stdout,
                      f'Chrome timed out: {timed_out}\\nSTDERR:\\n{stderr}\\nDOM:\\n{stdout}')
        self.assertIn('<pre id=\"result\">PASS</pre>', stdout, stdout)
        if not timed_out:
            self.assertEqual(process.returncode, 0, stderr)


if __name__ == '__main__':
    unittest.main()
