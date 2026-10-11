"""Use the native rewards-unavailable boot path during local FUT sessions.

The navigation file routes downloadFailed to bootflowFinished. This one-word
patch makes only UpgradeRewardsViewModel's ONLINE_ACCESS read return false;
the normal state machine emits that event. It does not change global online
access, grant rewards, or complete any FUT service.
"""
import struct

ADDRESS = 0x005f9530
ORIGINAL = 0x4bf0ba0d  # bl 0x504f3c: read ONLINE_ACCESS
REPLACEMENT = 0x38600000  # li r3,0: rewards unavailable locally


def patch_rows(data):
    actual = struct.unpack_from('>I', data, ADDRESS - 0x10000)[0]
    if actual != ORIGINAL:
        raise ValueError('Upgrade rewards gate differs from the researched executable')
    return f'      - [ be32, 0x{ADDRESS:08x}, 0x{REPLACEMENT:08x} ]\n'
