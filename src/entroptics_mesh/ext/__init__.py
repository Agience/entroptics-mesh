"""
ext -- working providers for the plug points in :mod:`entroptics_mesh.providers`.

The core imports nothing from here: it ships interfaces and null providers and stays a
networking SDK.  A deployment that wants sealing or interference detection selects these
explicitly::

    Providers(crypto=AeadCrypto(mesh_key), interference=EnergyInterference())

    aead        AeadCrypto      ChaCha20-Poly1305 (or AES-256-GCM) under a pre-shared mesh key; the
                                ``context`` is the associated data; a per-sender sliding window
                                refuses replays.  Needs the ``crypto`` extra (``cryptography``).
    interference   EnergyInterference   per-cell noise-floor reads of each capture; a cell whose floor
                                stands above the quietest cell's by more than noise alone reads is
                                reported as ``neighbors.InterferenceBand`` for the announcement.

Each module is imported on its own, so ``interference`` works without ``cryptography`` installed.
"""
