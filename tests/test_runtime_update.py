"""Updating an AppImage cannot keep launching an outdated cached RPCS3."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import launch


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.image=self.root/'rpcs3.AppImage';self.image.write_bytes(b'old image')
        self.calls=[]
        self.addCleanup(patch.stopall)
        patch.object(launch,'HERE',self.root).start()
        def extract(command,**kwargs):
            stage=Path(kwargs['cwd']);self.calls.append(stage)
            executable=stage/'squashfs-root/AppRun';executable.parent.mkdir()
            executable.write_bytes(self.image.read_bytes())
            return SimpleNamespace(returncode=0)
        self.extract=patch.object(launch.subprocess,'run',side_effect=extract).start()

    def test_new_image_gets_new_runtime_and_unchanged_image_reuses_cache(self):
        first=launch.runtime_executable(self.image)
        self.assertEqual(first.read_bytes(),b'old image')
        self.assertEqual(launch.runtime_executable(self.image),first)
        self.assertEqual(len(self.calls),1)
        self.image.write_bytes(b'new image with updated RPCN')
        second=launch.runtime_executable(self.image)
        self.assertNotEqual(first,second)
        self.assertEqual(second.read_bytes(),self.image.read_bytes())
        self.assertEqual(first.read_bytes(),b'old image')
        self.assertEqual(len(self.calls),2)
        self.assertFalse(list((self.root/'rpcs3-runtime').glob('.extract-*')))

    def test_legacy_cache_is_preserved_but_does_not_hide_an_appimage_update(self):
        base=self.root/'rpcs3-runtime';old=base/'squashfs-root/AppRun';old.parent.mkdir(parents=True);old.write_bytes(b'legacy binary')
        (base/'manifest.json').write_text(json.dumps({'sha256':hashlib.sha256(self.image.read_bytes()).hexdigest()}))
        self.assertEqual(launch.runtime_executable(self.image),old)
        self.assertFalse(self.calls)
        self.image.write_bytes(b'updated official image')
        result=launch.runtime_executable(self.image)
        self.assertNotEqual(result,old)
        self.assertEqual(old.read_bytes(),b'legacy binary')
        self.assertEqual(result.read_bytes(),self.image.read_bytes())

    def test_failed_extraction_keeps_log_and_never_publishes_partial_runtime(self):
        self.extract.side_effect=lambda *args,**kwargs:SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(RuntimeError,'extraction failed'):launch.runtime_executable(self.image)
        base=self.root/'rpcs3-runtime'
        self.assertTrue(list(base.glob('extract-failed-*.log')))
        self.assertFalse(list(base.glob('.extract-*')))
        self.assertFalse((base/hashlib.sha256(self.image.read_bytes()).hexdigest()).exists())

    def test_image_changed_while_extracting_is_rejected(self):
        original=self.extract.side_effect
        def extract(*args,**kwargs):
            result=original(*args,**kwargs);self.image.write_bytes(b'changed during extraction');return result
        self.extract.side_effect=extract
        with self.assertRaisesRegex(RuntimeError,'changed during extraction'):launch.runtime_executable(self.image)

    def test_windows_executable_is_returned_without_extraction(self):
        exe=self.root/'rpcs3.exe';self.assertEqual(launch.runtime_executable(exe),exe);self.extract.assert_not_called()


if __name__=='__main__':unittest.main()
