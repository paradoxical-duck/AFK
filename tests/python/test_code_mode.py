"""Code-mode spoken syntax conversion tests."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from afk_backend.code_mode import CodeProcessor  # noqa: E402


class TestCodeProcessor(unittest.TestCase):
    def setUp(self):
        self.processor = CodeProcessor()

    def code(self, text, language="javascript"):
        return self.processor.process(text, language=language)["text"]

    def test_javascript_function_syntax(self):
        self.assertEqual(
            self.code("function greet open paren name close paren open curly"),
            "function greet(name) {",
        )

    def test_comment_line(self):
        self.assertEqual(
            self.code("comment check whether the user is logged in"),
            "// Check whether the user is logged in",
        )

    def test_python_colon_and_comment(self):
        self.assertEqual(
            self.code("if user dot is admin colon new line indent comment allow access", language="python"),
            "if user.is_admin:\n    # Allow access",
        )

    def test_block_indentation(self):
        self.assertEqual(
            self.code("if ready open curly new line return true semicolon new line close curly"),
            "if ready {\n    return true;\n}",
        )


if __name__ == "__main__":
    unittest.main()
