"""Bounded Heat2 TDF encoding and decoding for observed Blaze messages."""
import struct

MAX_ITEMS = 4096
MAX_DEPTH = 16


def header(tag, kind):
    if not 1 <= len(tag) <= 4 or any(not 33 <= ord(c) <= 95 for c in tag):
        raise ValueError('Invalid TDF tag')
    packed = 0
    for index, char in enumerate(tag):
        packed |= (ord(char) - 32) << (18 - 6 * index)
    return packed.to_bytes(3, 'big') + bytes([kind])


def integer(value):
    sign = 0x40 if value < 0 else 0
    value = abs(value)
    if value >= 1 << 64:
        raise ValueError('TDF integer exceeds 64 bits')
    first, value = (value & 0x3f) | sign, value >> 6
    out = bytearray([first | (0x80 if value else 0)])
    while value:
        part, value = value & 0x7f, value >> 7
        out.append(part | (0x80 if value else 0))
    return bytes(out)


def string(value):
    raw = value.encode('utf-8') + b'\0'
    return integer(len(raw)) + raw


def int_field(tag, value):
    return header(tag, 0) + integer(value)


def str_field(tag, value):
    return header(tag, 1) + string(value)


def blob_field(tag, value):
    return header(tag, 2) + integer(len(value)) + value


def object_id(tag, component, kind, value):
    return header(tag, 9) + integer(component) + integer(kind) + integer(value)


def group_list(tag, values):
    return header(tag, 4) + b'\3' + integer(len(values)) + b''.join(v + b'\0' for v in values)


def group(tag, fields):
    return header(tag, 3) + fields + b'\0'


def int_list(tag, values):
    return header(tag, 4) + b'\0' + integer(len(values)) + b''.join(integer(v) for v in values)


def string_list(tag, values):
    return header(tag, 4) + b'\1' + integer(len(values)) + b''.join(string(v) for v in values)


def string_map(tag, values):
    return header(tag, 5) + b'\1\1' + integer(len(values)) + b''.join(string(k) + string(v) for k, v in values.items())


def group_map(tag, values):
    return header(tag, 5) + b'\1\3' + integer(len(values)) + b''.join(string(k) + v + b'\0' for k, v in values.items())


class Reader:
    def __init__(self, raw, preserve_blobs=False):
        self.raw, self.pos = raw, 0
        self.preserve_blobs = preserve_blobs

    def take(self, count):
        if count < 0 or self.pos + count > len(self.raw):
            raise ValueError('Truncated TDF value')
        result = self.raw[self.pos:self.pos + count]
        self.pos += count
        return result

    def byte(self):
        return self.take(1)[0]

    def integer(self):
        first = self.byte()
        value, shift, part = first & 0x3f, 6, first
        while part & 0x80:
            if shift >= 64:
                raise ValueError('Oversized TDF integer')
            part = self.byte()
            value |= (part & 0x7f) << shift
            shift += 7
        if value >= 1 << 64:
            raise ValueError('Oversized TDF integer')
        return -value if first & 0x40 else value

    def count(self):
        count = self.integer()
        if not 0 <= count <= MAX_ITEMS:
            raise ValueError('Oversized TDF collection')
        return count

    def value(self, kind, depth):
        if depth > MAX_DEPTH:
            raise ValueError('TDF nesting limit')
        if kind == 0:
            return self.integer()
        if kind in (1, 2):
            raw = self.take(self.integer())
            if kind == 2:
                return raw if self.preserve_blobs else {'blob_bytes': len(raw)}
            if not raw or not raw.endswith(b'\0'):
                raise ValueError('TDF string lacks terminator')
            return raw[:-1].decode('utf-8')
        if kind == 3:
            return self.fields(depth + 1, nested=True)
        if kind == 4:
            item_type, count = self.byte(), self.count()
            return [self.value(item_type, depth + 1) for _ in range(count)]
        if kind == 5:
            key_type, value_type, count = self.byte(), self.byte(), self.count()
            result = {}
            for _ in range(count):
                key = self.value(key_type, depth + 1)
                result[str(key)] = self.value(value_type, depth + 1)
            return result
        if kind == 6:
            active = self.byte()
            return {'union': None} if active == 0x7f else {'union': active, 'field': self.field(depth + 1)}
        if kind == 7:
            # Heat2 variable TDF: presence, concrete type ID, tagged value,
            # terminator. This is not an integer list. FIFA's pre-kickoff
            # skill report carries a present GAME and an absent PRVT here.
            present = self.byte()
            if present == 0:
                return {'variable': None}
            if present != 1:
                raise ValueError('Invalid variable TDF presence')
            type_id = self.integer()
            if not 0 < type_id < 1 << 32:
                raise ValueError('Invalid variable TDF type ID')
            field = self.field(depth + 1)
            if self.byte() != 0:
                raise ValueError('Variable TDF lacks terminator')
            return {'variable': type_id, 'field': field}
        if kind in (8, 9):
            return [self.integer() for _ in range(2 if kind == 8 else 3)]
        if kind == 10:
            return struct.unpack('>f', self.take(4))[0]
        raise ValueError(f'Unsupported TDF type {kind}')

    def field(self, depth):
        packed = int.from_bytes(self.take(3), 'big')
        tag = ''.join(chr(((packed >> shift) & 63) + 32) for shift in (18, 12, 6, 0)).rstrip()
        kind = self.byte()
        return tag, self.value(kind, depth)

    def fields(self, depth=0, nested=False):
        result = {}
        if nested and self.pos < len(self.raw) and self.raw[self.pos] == 2:
            self.pos += 1
        while self.pos < len(self.raw):
            if self.raw[self.pos] == 0:
                self.pos += 1
                return result
            if len(result) >= MAX_ITEMS:
                raise ValueError('Too many TDF fields')
            tag, value = self.field(depth)
            if tag in result:
                raise ValueError('Duplicate TDF tag')
            result[tag] = value
        if nested:
            raise ValueError('TDF group lacks terminator')
        return result


def decode(raw, preserve_blobs=False):
    reader = Reader(raw, preserve_blobs=preserve_blobs)
    result = reader.fields()
    if reader.pos != len(raw):
        raise ValueError('Trailing TDF bytes')
    return result
