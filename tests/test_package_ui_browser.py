import os
import pathlib
import shutil
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
        result = subprocess.run([
            browser,
            '--headless=new',
            '--no-sandbox',
            '--disable-gpu',
            '--allow-file-access-from-files',
            '--virtual-time-budget=1000',
            '--dump-dom',
            FIXTURE.as_uri(),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('<title>PASS</title>', result.stdout, result.stdout)
        self.assertIn('<pre id="result">PASS</pre>', result.stdout, result.stdout)


if __name__ == '__main__':
    unittest.main()
