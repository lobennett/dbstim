import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def test_one_jupyter_notebook_uses_direct_calls_and_has_no_saved_state(self):
        notebooks = sorted(path.name for path in (ROOT / 'notebooks').glob('*.*') if path.is_file())
        self.assertEqual(notebooks, ['api.ipynb'])
        path = ROOT / 'notebooks' / 'api.ipynb'
        notebook = json.loads(path.read_text())
        self.assertEqual(notebook['nbformat'], 4)
        code = []
        for cell in notebook['cells']:
            source = ''.join(cell['source'])
            if cell['cell_type'] == 'code':
                self.assertIsNone(cell['execution_count'])
                self.assertEqual(cell['outputs'], [])
                tree = ast.parse(source)
                self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in ast.walk(tree)))
                calls = [node.func.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
                self.assertFalse({'record', 'start', 'finish'} & set(calls), 'Preview cells must not report presentation')
                self.assertNotIn('with session', source)
                code.append(source)
        self.assertLessEqual(len('\n'.join(code).splitlines()), 50)
        self.assertNotIn('password123', path.read_text())

    def test_markdown_limit(self):
        paths = [ROOT / 'README.md', *(ROOT / 'docs').rglob('*.md')]
        for path in paths:
            self.assertLessEqual(len(path.read_text().split()), 500, str(path))


if __name__ == '__main__':
    unittest.main()
