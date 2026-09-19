from __future__ import annotations

import importlib.metadata
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import legal_rag
from legal_rag import cli


class M0CliTest(unittest.TestCase):
    def test_help_does_not_load_dotenv(self) -> None:
        output = io.StringIO()
        with patch.object(cli, "load_dotenv") as load_dotenv_mock:
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                cli.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("versioned Chinese law text snapshots", output.getvalue())
        load_dotenv_mock.assert_not_called()

    def test_module_version_comes_from_distribution_metadata(self) -> None:
        self.assertEqual(
            legal_rag.__version__,
            importlib.metadata.version("legal-rag-assistant"),
        )


if __name__ == "__main__":
    unittest.main()
