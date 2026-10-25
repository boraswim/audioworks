"""Per-song notes: songs/<name>/vocalworks.json.

One file per song folder holding everything specific to that song, so work can resume
without re-deriving it:
  song         title/artist/album/length (from analyze_song.py)
  analysis     measurements of the original (from analyze_song.py)
  recommended  settings analyze_song.py suggests
  plan         settings actually chosen, with the reason for any change
  status       "analyzed" | "in progress" | "awaiting feedback" | "approved" | "on hold"
  takes        per take: offset, timing, accuracy, settings used, results, verdict
  feedback     what the user said, newest last, with what was done about it
  log          short dated notes on decisions and fixes

Tools merge into the file (they never drop keys they don't own).

  python tools/songfile.py songs/<name>                 print it
  python tools/songfile.py songs/<name> status approved set a top-level text field
"""
import datetime
import json
import sys
from pathlib import Path

NAME = "vocalworks.json"
OLD_NAME = "audioworks.json"  # the app was called Audioworks until 2026-10-25


def path(folder):
    p = Path(folder) / NAME
    old = Path(folder) / OLD_NAME
    if not p.exists() and old.exists():
        old.rename(p)  # songs from before the rename keep their notes
    return p


def load(folder):
    p = path(folder)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(folder, data):
    data["updated"] = datetime.date.today().isoformat()
    path(folder).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge(folder, **sections):
    """Replace the given top-level sections, keep everything else."""
    data = load(folder)
    data.update(sections)
    save(folder, data)
    return data


if __name__ == "__main__":
    if len(sys.argv) == 2:
        print(json.dumps(load(sys.argv[1]), indent=2, ensure_ascii=False))
    elif len(sys.argv) == 4:
        merge(sys.argv[1], **{sys.argv[2]: sys.argv[3]})
        print(f"{sys.argv[2]} = {sys.argv[3]}")
    else:
        sys.exit(__doc__)
