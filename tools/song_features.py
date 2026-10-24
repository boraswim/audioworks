"""Check a song for production features that measuring the vocal alone can't settle, such as a
vocoder or talk box, and decide whether the cover uses them.

Two sources:
  - what's written about the song: its Wikipedia article (or, failing that, sentences about it
    in its album's article), scanned for vocal production terms in sentences about the vocals
  - the vocal stem itself: a classic vocoder's oscillator carrier is unnaturally periodic and
    steady (high cepstral peak, almost no jitter)

Hard autotune, distortion, width and hiss are measured by analyze_song.py; mentions of those
here are reported alongside the measurement for comparison, not used to override it.

Saves a "features" section into the song's audioworks.json (see songfile.py).

  python tools/song_features.py songs/<name> [--offline]
"""
import argparse
import re
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402
import songfile  # noqa: E402

warnings.filterwarnings("ignore")

API = "https://en.wikipedia.org/w/api.php"
# Wikimedia rate-limits anonymous clients that don't say who they are
UA = {"User-Agent": "Audioworks/1.0 (https://github.com/boraswim/audioworks)"}
TERMS = {
    "vocoder": r"vocoder|vocoded",
    "talk box": r"talk ?box",
    "autotune": r"auto-?tune[ds]?\b|pitch[- ]correct\w*",
    "distorted vocals": r"distort\w*|overdriven|megaphone|bullhorn",
    "lo-fi": r"lo-?fi\b|four-track|cassette|demo recording",
}
VOCAL = r"vocal|voice|sing|sung|singer|lyric|verse|chorus"
VOCAL_CREDIT = r"(?<!backing )\bvocals?\b"  # in a personnel line: the lead singer, not a backing part
CREDIT_LINE = r"^[^.\n]{2,60}\s[–—-]\s"  # "Name - guitar, talk box, backing vocals"
NEGATED = r"\b(without|no|not|instead of|rather than|avoid\w*|claim\w*|rumou?r\w*|mistaken\w*)\b[^.]{0,40}$"
# the oscillator-carrier fingerprint: our own vocoder.py output measured 13.5 dB / 0.32 %, natural and
# autotuned vocals 7.1-10.5 dB / 0.6-1.5 %
CARRIER_CPPS_DB, CARRIER_JITTER_PCT = 12.0, 0.45


def wiki(params):
    import time

    import requests

    for attempt in range(3):
        r = requests.get(API, params={**params, "format": "json"}, headers=UA, timeout=15)
        if r.status_code != 429:
            break
        time.sleep(min(10, int(r.headers.get("retry-after", 2))))
    r.raise_for_status()
    return r.json()


def extract(page_title):
    pages = wiki({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": page_title})
    return next(iter(pages["query"]["pages"].values())).get("extract", "")


def plain(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", "and")).strip()


def core(title):
    """The song's own name: without "(feat. ...)", "(demo)", "[Remastered]" or " - B-Side" notes."""
    return re.sub(r"\s*[(\[].*?[)\]]|\s+-\s+.*$", "", title).strip()


def main_artist(artist):
    return re.split(r",|&| feat\.?| ft\.| x ", artist, flags=re.I)[0].strip()


def find_article(artist, title, album=None):
    """(article title, text) about the song, or about its album with only sentences naming the song."""
    artist, title = main_artist(artist), core(title)
    t, a = plain(title), plain(artist)
    for query in (f'"{title}" {artist} song', f"{title} {artist}"):
        hits = wiki({"action": "query", "list": "search", "srsearch": query, "srlimit": 5})["query"]["search"]
        for h in hits:
            name = plain(re.sub(r"\(.*?\)", "", h["title"]))
            if name == t:
                text = extract(h["title"])
                # the right song: the article names the artist early on
                if not a or a in plain(text[:1500]):
                    return h["title"], text
    if album:
        hits = wiki({"action": "query", "list": "search", "srsearch": f'"{album}" {artist} album', "srlimit": 3})
        for h in sorted(hits["query"]["search"], key=lambda h: plain(h["title"]) != plain(album)):
            if plain(re.sub(r"\(.*?\)", "", h["title"])) == plain(core(album)):
                text = extract(h["title"])
                about = [s for s in sentences(text) if t and t in plain(s)]
                if about:
                    return f"{h['title']} (album, {len(about)} sentence(s) about the song)", " ".join(about)
    return None, ""


def sentences(text):
    return [s for line in text.splitlines() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]


def mentions(text, artist):
    """{feature: number of sentences about the vocals that mention it}"""
    singer = "|".join(re.escape(w) for w in plain(artist).split() if len(w) > 3)
    vocal = VOCAL + (f"|{singer}" if singer else "")
    found = {}
    for s in sentences(text):
        for name, pat in TERMS.items():
            m = re.search(pat, s, re.I)
            about_vocals = re.search(VOCAL_CREDIT if re.match(CREDIT_LINE, s) else vocal, s, re.I)
            if m and about_vocals and not re.search(NEGATED, s[:m.start()], re.I):
                found[name] = found.get(name, 0) + 1
    return found


def carrier(vocals):
    """Cepstral peak prominence (dB) and jitter (%) of the vocal stem."""
    from parselmouth.praat import call

    snd = music.load_sound(vocals)
    cpps = call(call(snd, "To PowerCepstrogram", 60, 0.002, 5000, 50), "Get CPPS", True, 0.02, 0.0005, 60, 330,
                0.05, "Parabolic", 0.001, 0, "Straight", "Robust")
    pp = call(snd, "To PointProcess (periodic, cc)", 75, 700)
    jitter = call(pp, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3) * 100
    return float(cpps), float(jitter)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--offline", action="store_true", help="skip the Wikipedia lookup")
    args = ap.parse_args()
    d = Path(args.folder)
    notes = songfile.load(d)
    s, src, an = notes.get("song", {}), notes.get("source", {}), notes.get("analysis", {})
    artist, title = s.get("artist") or src.get("artist") or "", s.get("title") or src.get("title") or ""

    feats = {"written": None}
    if args.offline or not title:
        print("Production notes: skipped" + ("" if title else " (no song title known)"))
    else:
        try:
            article, text = find_article(artist, title, s.get("album"))
            found = mentions(text, artist) if text else {}
            feats["written"] = {"source": f"Wikipedia: {article}" if article else None, "mentions": found}
            if not article:
                print(f"Production notes: no Wikipedia article found for {artist} - {title}")
            else:
                print(f"Production notes: Wikipedia '{article}'")
                for name, n in found.items():
                    print(f"   {name}: {n} sentence(s) about the vocals")
                if not found:
                    print("   nothing about vocal effects")
        except Exception as e:  # noqa: BLE001 - offline or the API changed: the audio check still runs
            print(f"Production notes: lookup failed ({e})")

    cpps, jitter = carrier(d / "song_vocals.wav")
    is_carrier = cpps > CARRIER_CPPS_DB and jitter < CARRIER_JITTER_PCT
    feats["carrier"] = {"cpps_db": round(cpps, 2), "jitter_pct": round(jitter, 2), "vocoder_like": is_carrier}
    print(f"Vocal stem: periodicity {cpps:.1f} dB, jitter {jitter:.2f}% -> "
          + ("oscillator-like, as a vocoder makes" if is_carrier else "a human voice (sung, possibly tuned)"))

    written = (feats["written"] or {}).get("mentions", {})
    effect = "vocoder" if written.get("vocoder") else "talk box" if written.get("talk box") else None
    # a song famous for Auto-Tune is often described as "vocoded" too: the Auto-Tune mentions win
    if effect and written.get("autotune", 0) > written.get(effect, 0):
        effect = None
    # a vocoder over the whole lead makes it step-like and steady, which the pitch analysis sees as tuned;
    # a natural-measuring lead means the vocoder is on a part (an outro, a phrase, backing voices)
    lead_tuned = not an.get("pitch_style", "natural").startswith("natural")
    if effect and lead_tuned:
        why = f"{feats['written']['source']} describes a {effect} on the vocals, and the lead measures tuned"
    elif is_carrier:
        why = "the vocal stem measures like a vocoder carrier"
    else:
        why = None
    feats["vocoder"] = bool(why)
    feats["why"] = why or (f"a {effect} is written about, but the lead measures natural, so it's on a part only"
                           if effect else "no sign of a vocoder in what's written or in the audio")
    # what's written vs what analyze_song measured
    agree = {"autotune": an.get("pitch_style", "").lower().startswith(("hard", "tuned")),
             "distorted vocals": an.get("distorted"), "lo-fi": "--hiss" in notes.get("recommended", {}).get("match_flags", [])}
    feats["checks"] = {k: {"written": True, "measured": bool(agree[k])} for k in agree if written.get(k)}
    for k, v in feats["checks"].items():
        print(f"   {k}: written about, {'and' if v['measured'] else 'but not'} measured in the vocal")
    print(f"Vocoder: {'yes' if feats['vocoder'] else 'no'} ({feats['why']})")
    songfile.merge(d, features=feats)
    print(f"Saved to {songfile.path(d)}")


if __name__ == "__main__":
    main()
