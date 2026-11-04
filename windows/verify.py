"""Checks a build byte for byte, because a machine under load can write wrong data without any error.

    verify.py record STAGE_SERVER          what pip installed, against the hashes in each package's RECORD
    verify.py manifest STAGE_SERVER FILE   writes the hash of every staged file
    verify.py installed FILE INSTALLED     everything in the manifest, as the installer actually put it down
Standard library only. Exits 1 on the first kind of failure it can report in full.
"""
import base64
import csv
import glob
import hashlib
import json
import os
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h


def check_record(server):
    site = os.path.join(server, ".venv", "Lib", "site-packages")
    bad, checked = [], 0
    for record in glob.glob(os.path.join(site, "*.dist-info", "RECORD")):
        with open(record, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 2 or not row[1].startswith("sha256="):
                    continue
                path = os.path.normpath(os.path.join(site, row[0]))
                if not os.path.exists(path):
                    continue  # the build removes tests and the like on purpose
                want = row[1][len("sha256="):]
                got = base64.urlsafe_b64encode(sha256(path).digest()).rstrip(b"=").decode()
                checked += 1
                if got != want:
                    bad.append(row[0])
    print(f"{checked} package files checked, {len(bad)} wrong")
    return bad


def write_manifest(server, target):
    hashes = {}
    for root, dirs, files in os.walk(server):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(root, name)
            hashes[os.path.relpath(path, server)] = sha256(path).hexdigest()
    with open(target, "w", encoding="utf-8") as f:
        json.dump(hashes, f)
    print(f"{len(hashes)} files in the manifest")


def check_installed(manifest, installed):
    with open(manifest, encoding="utf-8") as f:
        hashes = json.load(f)
    bad = [p for p, h in hashes.items()
           if not os.path.exists(os.path.join(installed, p)) or sha256(os.path.join(installed, p)).hexdigest() != h]
    print(f"{len(hashes)} files compared with what the installer put down, {len(bad)} wrong or missing")
    return bad


def main():
    kind = sys.argv[1]
    if kind == "manifest":
        return write_manifest(sys.argv[2], sys.argv[3])
    bad = check_record(sys.argv[2]) if kind == "record" else check_installed(sys.argv[2], sys.argv[3])
    for path in bad[:20]:
        print("  " + path)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
