# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Keep repository documentation references valid as guides move."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class DocumentationTests(unittest.TestCase):
    def test_repository_links_and_sections_exist(self):
        script = Path(__file__).resolve().parent.parent / 'bin/check-docs'
        check = runpy.run_path(str(script))['main']
        with redirect_stdout(io.StringIO()) as output:
            self.assertFalse(check(), output.getvalue())

    def test_check_reports_missing_files_and_sections_but_ignores_code_samples(self):
        script = Path(__file__).resolve().parent.parent / 'bin/check-docs'
        check = runpy.run_path(str(script))['main']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('# Setup\n[missing](gone.md)\n[section](#gone)\n'
                                          '```md\n[example](not-a-real-file.md)\n```\n')
            with patch.dict(check.__globals__, ROOT=root), redirect_stdout(io.StringIO()) as output:
                self.assertTrue(check())
            self.assertIn('missing gone.md', output.getvalue())
            self.assertIn('missing section #gone', output.getvalue())
            self.assertNotIn('not-a-real-file', output.getvalue())


if __name__ == '__main__':
    unittest.main()
