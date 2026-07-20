import ast
import unittest
from pathlib import Path


IMPORT_ENTITY_PATH = Path(__file__).resolve().parents[1] / "witcher3_tools" / "importers" / "import_entity.py"


class ImportEntityPolicyTests(unittest.TestCase):
    def test_error_placeholder_bypasses_armature_constraints(self):
        tree = ast.parse(IMPORT_ENTITY_PATH.read_text(encoding="utf-8"))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "process_constraints"
        )
        placeholder_guard = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If) and "witcher_import_error" in ast.unparse(node.test)
        )
        create_constraints = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "CreateConstraints2"
        )

        self.assertLess(placeholder_guard.lineno, create_constraints.lineno)
        self.assertIn("_set_parent_keep_world", ast.unparse(placeholder_guard))
        self.assertTrue(any(isinstance(node, ast.Continue) for node in placeholder_guard.body))


if __name__ == "__main__":
    unittest.main()
