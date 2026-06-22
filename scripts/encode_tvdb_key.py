"""Regenerate the bundled TVDB key strands -- run offline when rotating the key.

Usage (pass the key as an argument, or pipe it on stdin to keep it out of shell
history):
    uv run python scripts/encode_tvdb_key.py <tvdb-api-key>
    uv run python scripts/encode_tvdb_key.py < key.txt

Paste the two printed lines over ``_K1`` / ``_K2`` in ``nalanda/clients/tvdb.py``.
This is **obfuscation, not encryption** (the running app must reproduce the plaintext
key) -- it only keeps the key out of plaintext grep and automated secret-scanners. See
the note in that module.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent)
)  # project root on the path

from nalanda.clients.tvdb import _strand, _unstrand


def main() -> int:
    if len(sys.argv) > 2:
        print(
            "usage: python scripts/encode_tvdb_key.py [<tvdb-api-key>]"
            "   (or pipe it on stdin)",
            file=sys.stderr,
        )
        return 1
    # Argument if given, else read one line from stdin (so `... < key.txt` works,
    # history-free).
    key = (sys.argv[1] if len(sys.argv) == 2 else sys.stdin.readline()).strip()
    if not key:
        print(
            "error: no key provided (pass as an argument or pipe it on stdin)",
            file=sys.stderr,
        )
        return 1
    k1, k2 = _strand(key)
    if _unstrand(k1, k2) != key:  # round-trip sanity
        print("error: round-trip mismatch", file=sys.stderr)
        return 2
    print("# paste these over _K1 / _K2 in nalanda/clients/tvdb.py:")
    print(f'_K1 = "{k1}"')
    print(f'_K2 = "{k2}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
