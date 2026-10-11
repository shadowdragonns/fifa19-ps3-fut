"""Read PS3 language strings and supply text for the local account screen."""
from functools import lru_cache
from pathlib import Path
import struct

LANGUAGES = {'en': 'eng_us', 'nl': 'dut_nl', 'fr': 'fre_fr', 'de': 'ger_de',
             'it': 'ita_it', 'pl': 'pol_pl', 'ru': 'rus_ru', 'es': 'spa_es'}
LOCAL_ACCOUNT_TEXT = {
    'SDB_ORIGIN_ACCT_WELCOME_BACK_HEADER': 'Local account',
    'SDB_ORIGIN_ACCT_WELCOME_BACK_BODY_PS3ONLY': 'Signed in to the local FIFA 19 server.',
}
BUTTON_FALLBACKS = {'SDB_CONTINUE': 'Continue', 'SDB_BACK': 'Back',
                    'SDB_EMAIL_ADD': 'Email address',
                    'SDB_ORIGIN_ACCT_UPDATE_EMAIL_PASS': 'Account details'}


class LanguageBank:
    """The LanguageStrings table uses byte-Huffman strings of two lengths.

    String IDs (kind 13) have a one-byte length. Display text (kind 14) has
    a two-byte big-endian length. Both share the table's Huffman tree.
    """
    def __init__(self, data):
        self.data = data
        if data[:8] != b'DB\0\x08\x01\0\0\0' or self.u32(8) != len(data):
            raise ValueError('Unsupported PS3 language database')
        if self.u32(16) != 1 or data[24:28] != b'GJCv':
            raise ValueError('Expected one LanguageStrings table')
        header = 36 + self.u32(28)
        if not 0 <= header <= len(data) - 36:
            raise ValueError('Language table header outside database')
        self.size = self.u32(header + 4)
        self.count = int.from_bytes(data[header + 18:header + 20], 'big')
        field_count = data[header + 24]
        self.start = header + 36 + 16 * field_count
        self.pool = self.start + self.size * self.count
        self.pool_end = self.pool + self.u32(header + 12)
        if not self.size or self.pool_end > len(data) or self.pool_end < self.pool:
            raise ValueError('Language table outside database')
        self.fields = {}
        for index in range(field_count):
            at = header + 36 + 16 * index
            kind, bit, tag, _ = struct.unpack_from('>II4sI', data, at)
            if tag in (b'VhAs', b'bYbZ'):
                if kind not in (13, 14) or bit % 8 or bit // 8 + 4 > self.size:
                    raise ValueError('Unsupported language string field')
                self.fields[tag] = kind, bit // 8
        if self.fields.keys() != {b'VhAs', b'bYbZ'}:
            raise ValueError('Missing language string fields')
        self.index = {self.read(index, b'VhAs'): index for index in range(self.count)}

    def u32(self, offset):
        if not 0 <= offset <= len(self.data) - 4:
            raise ValueError('Database integer outside bounds')
        return struct.unpack_from('>I', self.data, offset)[0]

    def read(self, index, field):
        kind, offset = self.fields[field]
        pointer = self.u32(self.start + index * self.size + offset)
        if pointer == 0xffffffff:
            return ''
        start = self.pool + pointer
        width = 1 if kind == 13 else 2
        if not self.pool <= start <= self.pool_end - width:
            raise ValueError('Language string pointer outside pool')
        length = int.from_bytes(self.data[start:start + width], 'big')
        if length > 4000:
            raise ValueError('Oversized language string')
        position, result, node, steps = (start + width) * 8, bytearray(), 0, 0
        while len(result) < length:
            if position >= self.pool_end * 8 or steps > max(256, length * 256):
                raise ValueError('Invalid language Huffman stream')
            bit = (self.data[position // 8] >> (7 - position % 8)) & 1
            position += 1
            steps += 1
            at = self.pool + 4 * node + 2 * bit
            if at + 2 > self.pool_end:
                raise ValueError('Huffman node outside pool')
            child, leaf = self.data[at:at + 2]
            if child:
                node = child
            else:
                result.append(leaf)
                node = 0
        return result.decode('utf-8')

    def get(self, name):
        index = self.index.get(name)
        return None if index is None else self.read(index, b'bYbZ')


@lru_cache(maxsize=8)
def language_bank(archive, name):
    with archive.open('rb') as stream:
        header = stream.read(16)
        if len(header) != 16 or header[:4] != b'BIG4':
            raise ValueError('Unsupported language archive')
        count, end = struct.unpack('>II', header[8:])
        if count > 256 or not 16 <= end <= 65536:
            raise ValueError('Invalid language archive directory')
        directory = stream.read(end - 16)
        position = 0
        for _ in range(count):
            offset, size = struct.unpack_from('>II', directory, position)
            position += 8
            stop = directory.index(b'\0', position)
            entry = directory[position:stop].decode('ascii')
            position = stop + 1
            if entry == f'data/loc/{name}.db':
                if not 0 < size <= 16 * 1024 * 1024:
                    raise ValueError('Invalid language database size')
                stream.seek(offset)
                data = stream.read(size)
                if len(data) != size:
                    raise ValueError('Truncated language database')
                return LanguageBank(data)
    raise ValueError('Language database missing from archive')


class Localizer:
    def __init__(self, archive=None):
        self.archive = Path(archive) if archive else Path(__file__).resolve().parents[2] / 'PS3_GAME/USRDIR/data/loc/locale.big'

    def strings(self, request):
        locale, identifiers = request.get('LANG', int.from_bytes(b'enUS', 'big')), request.get('LSID', [])
        if (type(locale) is not int or not 0 <= locale < 2**32
                or not isinstance(identifiers, list) or len(identifiers) > 4096
                or any(not isinstance(key, str) or not 1 <= len(key) <= 256 for key in identifiers)):
            raise ValueError('Invalid localization request')
        language = locale.to_bytes(4, 'big')[:2].decode('ascii', errors='ignore').lower()
        # Portable server bundles need no game archive for the six observed
        # account-screen labels. A supplied archive keeps all retail languages.
        bank = language_bank(self.archive, LANGUAGES.get(language, 'eng_us')) if self.archive.is_file() else None
        result = {}
        for key in identifiers:
            text = LOCAL_ACCOUNT_TEXT.get(key)
            if text is None:
                text = bank.get(key) if bank else None
            if text is None and bank and key.startswith('SDB_'):
                text = bank.get('OSDK_' + key[4:])
            if text is None:
                text = BUTTON_FALLBACKS.get(key, 'Text unavailable')
            result[key] = text
        return dict(sorted(result.items()))
