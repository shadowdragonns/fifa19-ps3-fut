"""UTF-8 names must not depend on the Windows system's legacy code page."""
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fut_items
from web_app import WebApp

HERE = Path(__file__).resolve().parents[1]


class CatalogueEncodingTests(unittest.TestCase):
    def test_web_labels_and_manager_names_load_under_a_cp1252_default(self):
        original = io.open
        def cp1252_open(file, mode='r', buffering=-1, encoding=None, errors=None,
                        newline=None, closefd=True, opener=None):
            if 'b' not in mode and encoding in (None,'locale'): encoding = 'cp1252'
            return original(file,mode,buffering,encoding,errors,newline,closefd,opener)
        labels = HERE/'data/web-pack-labels.json'
        expected_labels = json.loads(labels.read_bytes())
        expected_managers = json.loads((HERE/'data/support-items.json').read_bytes())['managers']
        # Force the simulated default even when this interpreter was launched
        # in UTF-8 mode (where pathlib resolves None to UTF-8 before io.open).
        def text_encoding(encoding, *args):
            return 'cp1252' if encoding in (None,'locale') else encoding
        with patch.object(io,'text_encoding',side_effect=text_encoding), patch.object(io,'open',side_effect=cp1252_open):
            # Reproduce the original Windows failure, then exercise both fixed
            # loaders against the real non-ASCII catalogue data.
            with self.assertRaises(UnicodeDecodeError): labels.read_text()
            app = WebApp(SimpleNamespace(root=object()),accounts=object())
            importlib.reload(fut_items)
            self.assertEqual(app.labels,expected_labels)
            self.assertEqual(fut_items.MANAGERS,expected_managers)


if __name__=='__main__': unittest.main()
