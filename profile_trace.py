"""Temporary PPC64 tracepoints for native startup and FUT store completion.

RPCS3 bb5dc93b supports `calloc`: the following be32 rows fill its allocated
code, and RPCS3 supplies a return branch. No fixed code cave is overwritten.
Sources: https://github.com/RPCS3/rpcs3/blob/bb5dc93b/Utilities/bin_patch.cpp
and rpcs3/Emu/Cell/lv2/{sys_tty.cpp,lv2.cpp} at the same revision.
"""
import struct


def d(op, target, source, immediate):
    return op << 26 | target << 21 | source << 16 | (immediate & 0xffff)


def spr(op, register, special):
    return (31 << 26 | register << 21 | (special & 31) << 16 |
            (special >> 5) << 11 | op << 1)


def trace_words(marker, fields, original, busy_only=False):
    """Log integer fields, preserve registers/flags, execute replaced word.

    fields are (label, source-register, offset) tuples. A None offset reads
    the incoming register; ('byte', offset) reads one byte rather than a word.
    The guard can also select a finite set of incoming event IDs.
    No response bodies, credentials or save data are printed.
    """
    words = [d(62, 1, 1, -0x180) | 1, d(62, 0, 1, 0x10)]  # stdu; std
    for special, offset in ((8, 0x18), (1, 0x28), (9, 0x30)):
        words += [spr(339, 0, special), d(62, 0, 1, offset)]
    words += [0x7c000026, d(62, 0, 1, 0x20)]  # mfcr r0
    for register in range(2, 13):
        words.append(d(62, register, 1, 0x38 + 8 * (register - 2)))
    guard = None
    if busy_only:
        if busy_only is True:
            words += [d(34, 7, 30, 0), d(11, 0, 7, 0)]  # lbz busy; cmpwi
            guard = len(words)
            words.append(0)  # beq restore
        else:
            register, allowed = busy_only
            matches = []
            for event in allowed:
                words.append(d(11, 0, register, event))
                matches.append(len(words))
                words.append(0)  # beq message
            guard = len(words)
            words.append(0)  # b restore (uninteresting event)
            for branch in matches:
                words[branch] = 0x41820000 | ((len(words) - branch) * 4)
    text = ('FUTPRAN ' + marker + ''.join(' ' + label + '=00000000' for label, _, _ in fields) + '\n').encode()
    padded = text + b'\0' * (-len(text) % 4)
    if 0xa0 + len(padded) >= 0x170:
        raise ValueError('Trace message exceeds reserved stack buffer')
    for offset in range(0, len(padded), 4):
        value = int.from_bytes(padded[offset:offset + 4], 'big')
        words += [d(15, 8, 0, value >> 16), d(24, 8, 8, value),
                  d(36, 8, 1, 0xa0 + offset)]
    position = len('FUTPRAN ' + marker)
    for label, register, offset in fields:
        position += len(' ' + label + '=')
        words.append(d(58, 9, 1, 0x38 + 8 * (register - 2)) if register <= 12
                     else d(24, register, 9, 0))
        if offset is not None:
            byte = isinstance(offset, tuple)
            if byte:
                offset = offset[1]
            words.append(d(34 if byte else 32, 9, 9, offset))
        for digit in range(8):
            # rlwinm r7,r9,rotate,28,31: select one low-word nibble.
            rotate = ((digit + 1) * 4) % 32
            words += [(21 << 26 | 9 << 21 | 7 << 16 | rotate << 11 | 28 << 6 | 31 << 1),
                      d(14, 7, 7, 48), d(11, 0, 7, 57),
                      0x40810008, d(14, 7, 7, 7),  # ble +8; digit A..F
                      d(38, 7, 1, 0xa0 + position + digit)]
        position += 8
    words += [d(14, 3, 0, 3), d(14, 4, 1, 0xa0), d(14, 5, 0, len(text)),
              d(14, 6, 1, 0x170), d(14, 11, 0, 403), 0x44000002]
    restore = len(words)
    if guard is not None:
        words[guard] = (0x41820000 if busy_only is True else 0x48000000) | ((restore - guard) * 4)
    for special, offset in ((8, 0x18), (1, 0x28), (9, 0x30)):
        words += [d(58, 0, 1, offset), spr(467, 0, special)]
    words += [d(58, 0, 1, 0x20), 0x7c0ff120]  # mtcrf 255,r0
    for register in range(2, 13):
        words.append(d(58, register, 1, 0x38 + 8 * (register - 2)))
    words += [d(58, 0, 1, 0x10), d(14, 1, 1, 0x180), original]
    return words


SITES = (
    # Squad names are never printed. Trace whether the UI reaches the native
    # service, then the final write's numeric ID/mode and transport status.
    (0xb45660, 0x7c0802a6, 'squad-create-ui', (), False),
    (0xb45dbc, 0x7c0802a6, 'squad-rename-ui', (), False),
    (0x1671324, 0x7c0802a6, 'squad-create-service', (), False),
    (0x16731f0, 0x7c0802a6, 'squad-rename-service', (('id', 4, None),), False),
    (0x16dda98, 0x7c0802a6, 'squad-save-submit', (('id', 4, 0x10), ('mode', 4, 0x1d0)), False),
    (0x16dd1f8, 0x7c0802a6, 'squad-rename-submit', (('id', 4, 0x10),), False),
    (0x2119fc, 0x7c0802a6, 'success', (('kind', 3, 4), ('type', 3, 8), ('job', 3, 12)), False),
    (0x211a20, 0x2c9d0000, 'job-cleared', (('kind', 31, 4), ('type', 31, 8), ('job', 31, 12)), False),
    (0x211974, 0xf821ff91, 'failure', (('kind', 3, 4), ('type', 3, 8), ('code', 4, None)), False),
    (0x2101fc, 0x9bfe0000, 'jobs-cleared', (('pending', 30, ('byte', 1)),), True),
    (0x211424, 0x7c0802a6, 'versions-enter', (('busy', 3, ('byte', 0)), ('pending', 3, ('byte', 1))), False),
    (0x2115c4, 0x2c190000, 'versions-decided', (('changed', 25, None),), False),
    (0x211628, 0x38800032, 'versions-match', (('pending', 30, ('byte', 1)),), False),
    (0x69cccc, 0x7c0802a6, 'view-enter', (), False),
    (0x69c888, 0x7c0802a6, 'view-event', (('event', 4, None), ('upload', 3, ('byte', 0x8c))), (4, (0x32, 0x33, 0x3f))),
    (0x5f9aac, 0x7c0802a6, 'rewards-enter', (), False),
    (0x5f94ac, 0x7c0802a6, 'rewards-step', (('state', 3, 0xb4), ('accepted', 4, None)), False),
    (0x5f976c, 0x38600005, 'rewards-skip', (), False),
    # These run at store decisions, rather than every pending-state poll.
    # The parser's odds flag is separate from the frontend availability check.
    (0xb6516c, 0x2c030000, 'store-enabled', (('enabled', 3, None),), False),
    (0x16e1474, 0x63e30000, 'store-parsed', (('success', 31, None), ('maintenance', 27, None)), False),
    (0xb64e54, 0x3880754f, 'store-quantities-failed', (), False),
    (0xb64ef0, 0x3880754f, 'store-cache-failed', (), False),
    (0xb64dec, 0x38600001, 'store-ready', (), False),
    (0x16a0fb0, 0x7c0802a6, 'store-content-state', (('state', 4, None),), False),
    (0x16a2754, 0x7c0802a6, 'store-loc-parse', (), False),
    (0x169c4b8, 0x2c070000, 'store-download-init', (('service', 7, None),), False),
    (0x16b426c, 0x7c0802a6, 'match-create-parse', (), False),
    (0x16b4394, 0x38600001, 'match-create-parsed', (), False),
    # CreateMatch callback after its null-result guard. Integer status only.
    (0x16b48dc, 0x807f001c, 'match-create-callback', (('code', 31, 0x1c),), False),
    # Integer venue IDs only: cached home equipment and final home/away choice.
    (0x140cac8, 0x63e30000, 'home-stadium', (('stadium', 31, None),), False),
    (0xb508dc, 0x63a30000, 'match-stadium', (('stadium', 29, None),), False),
    (0x16d0c44, 0x7c0802a6, 'match-submit', (), False),
    (0x16b62a4, 0x7c0802a6, 'match-end-parse', (), False),
    (0x16b65fc, 0x63830000, 'match-end-parsed', (), False),
    # After the callback's null-result guard, before its status check.
    (0x16b6e7c, 0x807e001c, 'match-end-callback', (('code', 30, 0x1c),), False),
    # Shared action callback: op 0x34 is PlayGame. Log only operation/status.
    (0x16b2a1c, 0xf821ff91, 'sdk-action-result', (('op', 3, 0x28), ('code', 4, 0x1c)), False),
    (0x16d8e04, 0x7c0802a6, 'season-list-parse', (), False),
    (0x16d914c, 0x38600001, 'season-list-parsed', (), False),
    (0x16d9438, 0x7c0802a6, 'season-progress-parse', (), False),
    (0x16d96f0, 0x38600001, 'season-progress-parsed', (), False),
    (0x16ea43c, 0x7c0802a6, 'season-data-load', (), False),
    (0x16ea6fc, 0x38600001, 'season-data-loaded', (), False),
    (0xb5de08, 0x7c0802a6, 'season-ui-progress', (), False),
    (0xb5dfb8, 0x2c030000, 'season-selected', (('success', 3, None),), False),
    (0xb5dff8, 0x2c030000, 'season-loaded', (('success', 3, None),), False),
)


def patch_rows(data):
    rows = []
    for address, original, marker, fields, guard in SITES:
        actual = struct.unpack_from('>I', data, address - 0x10000)[0]
        if actual != original:
            raise ValueError(f'Native tracepoint {address:#x} differs from researched executable')
        words = trace_words(marker, fields, original, guard)
        rows.append(f'      - [ calloc, 0x{address:08x}, {len(words)} ]\n')
        rows.extend(f'      - [ be32, 0x00000000, 0x{word:08x} ]\n' for word in words)
    return ''.join(rows)
