"""Minimal `zstandard` shim over Python 3.14's stdlib compression.zstd (local scratch use only)."""
from compression import zstd


class ZstdDecompressor:
    def decompress(self, data, max_output_size=0):
        return zstd.decompress(data)
