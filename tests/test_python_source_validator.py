import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.python_source_validator import validate_python_source


class TestPythonSourceValidator(unittest.TestCase):
    def test_valid_source_returns_ok(self):
        result = validate_python_source(
            "def solve(problem, data):\n    return {'stdout': ''}\n",
            filename="<unit_test>",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.formatted_error, "")
        self.assertEqual(result.error_type, "")

    def test_incomplete_if_returns_syntax_error(self):
        result = validate_python_source("if\n print()\n", filename="<unit_test>")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "SyntaxError")
        self.assertEqual(result.lineno, 1)
        self.assertIn("SyntaxError", result.formatted_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
