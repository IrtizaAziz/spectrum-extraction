import importlib.util
import json
from pathlib import Path
from unittest import TestCase

class PluginTest(TestCase):
    def test_register_and_prepare(self):
        path = Path(__file__).resolve().parents[1] / '__init__.py'
        spec = importlib.util.spec_from_file_location('spectrum_test_plugin', path)
        self.assertIsNotNone(spec, 'plugin entrypoint must exist')
        self.assertTrue(path.is_file(), 'plugin entrypoint must exist')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class Registry:
            def register_tool(self, **row): self.__dict__.setdefault('tools', {})[row['name']] = row
            def register_skill(self, name, path): self.skill = (name, path)
        context = Registry()
        module.register(context)
        self.assertEqual(set(context.tools), {'spectrum_extraction_prepare', 'spectrum_extraction_scan'})
        tool = context.tools['spectrum_extraction_prepare']
        out = json.loads(tool['handler']({}))
        self.assertEqual(out['origin'], 'https://spectrum.um.edu.my')
        self.assertIn('visible_page_only', out['expression'])
        self.assertEqual(json.loads(tool['handler']({'url':'https://elsewhere.invalid'}))['status'], 'invalid_arguments')
        scan = context.tools['spectrum_extraction_scan']['handler']
        self.assertEqual(json.loads(scan({'action':'scan'}))['reason'], 'invalid_target')
        self.assertTrue(context.skill[1].is_file())
