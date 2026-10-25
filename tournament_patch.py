"""Hash-scoped RPCS3 compatibility fixes for the retail cup results updater.

RPCS3 calloc allocates an instruction trampoline at each site and appends
the branch back. Rows contain only our short replacements, not game assets.
"""
import struct


SITES = (
    # The retail loop accepts an unplayed home fixture only. Preserve its
    # next comparison, supplying the away ID when the home ID did not match.
    (0x016f915c, 0x80990034, (
        0x80990034,  # lwz r4,0x34(r25): home team
        0x7c032000,  # cmpw r3,r4: local club ID vs home team
        0x41820008,  # beq return-to-original-comparison
        0x80990038,  # lwz r4,0x38(r25): away team
    )),
    # No pending local fixture (including a repeated finished-results
    # callback): release the existing list at the normal cleanup path.
    # Never call GetMatchData(null), which dereferences match+0x34.
    (0x016f9180, 0x807a044c, (
        0x2c1f0000,  # cmpwi r31,0: selected fixture
        0x40820008,  # bne original-load
        0x496f9612,  # ba 0x016f9610: existing cleanup
        0x807a044c,  # lwz r3,0x44c(r26): engine
    )),
)


def patch_rows(data=None):
    rows=[]
    for site,original,words in SITES:
        if data is not None and struct.unpack_from('>I',data,site-0x10000)[0]!=original:
            raise ValueError(f'Tournament results instruction {site:#x} differs from the researched executable')
        rows.append(f'      - [ calloc, 0x{site:08x}, {len(words)} ]\n')
        rows.extend(f'      - [ be32, 0x00000000, 0x{word:08x} ]\n' for word in words)
    return ''.join(rows)
