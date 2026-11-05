"""Print every Mach-O file under a folder, NUL-separated. Found by their first bytes, not their names:
torch ships programs with no extension in torch/bin, and Apple rejects any binary left unsigned."""
import os
import sys

MAGIC = {bytes.fromhex(m) for m in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca")}

for folder, _, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(folder, name)
        if not os.path.islink(path):
            with open(path, "rb") as f:
                if f.read(4) in MAGIC:
                    print(path, end="\0")
