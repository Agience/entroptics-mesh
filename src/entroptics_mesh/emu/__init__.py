"""
emu -- an emulated air for many nodes, in lieu of many radios.

This is a model of the physical channel, not part of any receiver: it scales each transmitted burst
by the path loss to each listener, sums whatever overlaps there (so collisions are real), and adds
the listener's own noise.  The receivers on top of it are the real ones -- the same PHY adapter and
the same codec a radio node runs -- so what the emulation measures is the system, not a model of it.
"""
