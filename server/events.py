"""One-way events for the desktop UI: JSON lines on stdout, marked so they stand out from the log."""
import json
import sys

enabled = False


def emit(kind, **data):
    if enabled:
        sys.stdout.write("@event " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
