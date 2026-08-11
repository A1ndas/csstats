"""Decompress Valve .dem.bz2 replays and sanity-check them."""
import bz2
import hashlib
import shutil
import sys
from pathlib import Path

DATA = Path(r"C:\Users\willi\Projects\csstats-data")
COMPRESSED = DATA / "demos"
EXTRACTED = DATA / "extracted"

CS2_MAGIC = b"PBDEMS2"


def decompress(src: Path) -> Path:
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    dst = EXTRACTED / src.name.removesuffix(".bz2")
    tmp = dst.with_name(dst.name + ".part")

    if dst.exists():
        print(f"  already extracted: {dst.name}")
        return dst

    with bz2.open(src, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    tmp.replace(dst)
    return dst


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    archives = sorted(COMPRESSED.glob("*.dem.bz2"))
    if not archives:
        print(f"No .dem.bz2 files found in {COMPRESSED}")
        return 1

    for src in archives:
        print(f"\n{src.name}  ({src.stat().st_size / 1e6:.1f} MB compressed)")
        print(f"  sha256(bz2) = {sha256(src)}")
        dem = decompress(src)
        with open(dem, "rb") as f:
            magic = f.read(8)
        ok = magic.startswith(CS2_MAGIC)
        print(f"  -> {dem.name}  ({dem.stat().st_size / 1e6:.1f} MB)")
        print(f"  magic = {magic!r}  {'OK' if ok else 'UNEXPECTED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())