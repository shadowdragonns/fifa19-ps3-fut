"""Local static FUT files, separate from SID-authenticated inventory routes.

Retail 0x16a283c builds packs/loc/storepackdescriptions.en_us.xml; 0x16a2754
reads XLIFF trans-unit nodes, and 0x16a1f3c reads resname/source. These files
contain public labels only; they neither authenticate nor grant inventory.
"""
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PACK_DESCRIPTIONS_PATH = '/fut/packs/loc/storepackdescriptions.en_us.xml'


def response(method, path, settings=None):
    parsed = urlsplit(path)
    if parsed.path != PACK_DESCRIPTIONS_PATH:
        return None
    headers = [('Cache-Control', 'no-store')]
    if method != 'GET':
        return ('405 Method Not Allowed', b'GET required', 'text/plain',
                headers+[('Allow', 'GET')], 'FUT.PackDescriptions.WrongMethod')
    if parsed.query:
        return ('400 Bad Request', b'Unexpected query', 'text/plain', headers,
                'FUT.PackDescriptions.InvalidQuery')
    if settings is None:
        data = (HERE/'data/storepackdescriptions.en_us.xml').read_bytes()
    else:
        import xml.etree.ElementTree as ET
        from fut_packs import PACKS, description
        root = ET.Element('xliff', version='1.2')
        file = ET.SubElement(root, 'file', {'original': 'storepackdescriptions',
            'source-language': 'en-US', 'datatype': 'plaintext'})
        body = ET.SubElement(file, 'body')
        for pack_id in PACKS:
            spec = settings.pack(pack_id)
            if spec.get('retired'): continue
            for suffix, source in (('NAME', spec['name']), ('DESC', description(spec))):
                key = f'FUT_STORE_PACK_{pack_id}_{suffix}'
                unit = ET.SubElement(body, 'trans-unit', id=key, resname=key)
                ET.SubElement(unit, 'source').text = source
        data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    return ('200 OK', data,
            'application/xml; charset=utf-8', headers, 'FUT.PackDescriptions')
