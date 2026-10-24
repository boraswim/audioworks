"""Synced lyrics for in-app recording: songs/<name>/lyrics.lrc

Sources, in order: an existing lyrics.lrc, synced lyrics from LRCLIB (lrclib.net, a free
open database), plain lyrics timed automatically from the song's vocal track. Timings are
checked against where singing actually starts in song_vocals.wav and shifted to fit, which
fixes versions with a different intro (music videos). No AI involved: onset detection and
offset search only.
"""
import re
from pathlib import Path

import numpy as np
import soundfile as sf

import requests

LRC = "lyrics.lrc"
UA = {"User-Agent": "Audioworks (personal karaoke recorder)"}
TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


# ---------------------------------------------------------------- LRC text

def parse_lrc(text):
    """[(seconds, line)] sorted; handles several timestamps per line and an [offset:ms] tag."""
    offset = 0.0
    m = re.search(r"\[offset:\s*([+-]?\d+)\]", text, re.I)
    if m:
        offset = int(m.group(1)) / 1000
    out = []
    for raw in text.splitlines():
        stamps = TS.findall(raw)
        if not stamps:
            continue
        words = TS.sub("", raw).strip()
        for mm, ss in stamps:
            out.append((int(mm) * 60 + float(ss) - offset, words))
    return sorted(out)


def to_lrc(lines):
    return "\n".join(f"[{int(t // 60):02d}:{t % 60:05.2f}]{w}" for t, w in lines) + "\n"


def load(folder):
    p = Path(folder) / LRC
    return parse_lrc(p.read_text(encoding="utf-8")) if p.exists() else []


def save(folder, lines):
    (Path(folder) / LRC).write_text(to_lrc(lines), encoding="utf-8")


def line_at(lines, t):
    """Index of the line being sung at time t (-1 before the first)."""
    i = -1
    for k, (start, _) in enumerate(lines):
        if start <= t:
            i = k
        else:
            break
    return i


# ---------------------------------------------------------------- LRCLIB

def fetch_lrclib(artist, title, duration=None, album=None):
    """(synced_lrc_text or None, plain_text or None) from lrclib.net."""
    base = "https://lrclib.net/api"
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = round(duration)
    try:
        r = requests.get(f"{base}/get", params=params, headers=UA, timeout=15)
        if r.ok:
            d = r.json()
            if d.get("syncedLyrics") or d.get("plainLyrics"):
                return d.get("syncedLyrics"), d.get("plainLyrics")
        r = requests.get(f"{base}/search", params={"q": f"{artist} {title}"}, headers=UA, timeout=15)
        if r.ok and r.json():
            hits = r.json()
            if duration:  # prefer the version closest in length, synced first
                hits.sort(key=lambda h: (not h.get("syncedLyrics"), abs((h.get("duration") or 0) - duration)))
            h = hits[0]
            return h.get("syncedLyrics"), h.get("plainLyrics")
    except (requests.RequestException, ValueError):
        pass
    return None, None


# ---------------------------------------------------------------- timing from the vocal track

def phrase_onsets(folder, min_gap=0.15):
    """Times where a sung note starts after a pause, from the vocal track's pitch (10 ms frames).

    Pitch, not volume: a rock vocal track never goes quiet (instrument bleed), so a volume
    threshold found only 6 phrase starts in a whole song where pitch finds ~80.
    """
    sys_path = str(Path(__file__).resolve().parent.parent / "tools")
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import music

    t, f0 = music.track_pitch(music.load_sound(Path(folder) / "song_vocals.wav"))
    voiced = ~np.isnan(f0)
    onsets, quiet, last = [], int(min_gap / 0.01), -10 ** 9
    for i in range(1, len(voiced)):
        if voiced[i] and not voiced[i - 1] and i - last >= quiet:
            onsets.append(float(t[i]))
        if voiced[i]:
            last = i
    return np.array(onsets)


def auto_offset(lines, onsets, search=12.0, tol=0.4):
    """(shift s, match share, margin): the shift lining up the most lyric line starts with sung phrase
    starts, the share of lines it lines up, and how much better it is than any shift over 1 s away."""
    starts = np.array([t for t, w in lines if w])
    if len(starts) == 0 or len(onsets) == 0:
        return 0.0, 0.0, 0.0
    scores = []
    for shift in np.arange(-search, search + 0.001, 0.05):
        d = np.abs((starts + shift)[:, None] - onsets[None, :]).min(axis=1)
        scores.append((float(np.mean(d < tol)), -abs(shift), round(float(shift), 2)))
    share, _, shift = max(scores)
    other = max(s for s, _, sh in scores if abs(sh - shift) > 1.0)
    return shift, share, share - other


def confident_shift(lines, folder):
    """Apply the measured shift only when it clearly beats the alternatives; else keep the given timing."""
    shift, share, margin = auto_offset(lines, phrase_onsets(folder))
    if share >= 0.4 and margin >= 0.1:
        return shift, share, True
    return 0.0, share, False


def time_plain(text, folder):
    """Plain lyrics -> approximate timed lines: spread over the sung phrases by syllable count."""
    words = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("[")]
    onsets = phrase_onsets(folder)
    if not words or len(onsets) == 0:
        return []
    syll = np.array([max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for w in words], float)
    first, last = onsets[0], onsets[-1]
    starts = first + (last - first) * np.concatenate([[0], np.cumsum(syll)[:-1]]) / syll.sum()
    snapped = [float(onsets[np.argmin(np.abs(onsets - s))]) for s in starts]
    for i in range(1, len(snapped)):  # keep order; two lines never share a start
        if snapped[i] <= snapped[i - 1]:
            snapped[i] = max(starts[i], snapped[i - 1] + 0.5)
    return list(zip(snapped, words))


# ---------------------------------------------------------------- orchestration

def get(folder, artist, title, duration, album=None, log=print):
    """Find, time-check and save lyrics for a prepared song. Returns a notes dict (or None)."""
    folder = Path(folder)
    synced, plain = fetch_lrclib(artist, title, duration, album)
    if synced:
        lines = parse_lrc(synced)
        shift, share, sure = confident_shift(lines, folder)
        lines = [(max(0.0, t + shift), w) for t, w in lines]
        save(folder, lines)
        if sure:
            log(f"Lyrics: {len(lines)} synced lines from LRCLIB; shifted {shift:+.2f} s to fit this version "
                f"({100 * share:.0f}% of lines start where singing starts)")
        else:
            log(f"Lyrics: {len(lines)} synced lines from LRCLIB; kept their timing (couldn't confirm a better fit)")
        return {"source": "lrclib synced", "shift_s": shift, "match_pct": round(100 * share), "approximate": not sure}
    if plain:
        lines = time_plain(plain, folder)
        if lines:
            save(folder, lines)
            log(f"Lyrics: {len(lines)} lines from LRCLIB (not synced); timed automatically from the vocal track")
            return {"source": "lrclib plain, auto-timed", "approximate": True}
    log("Lyrics: none found online. You can load a .lrc file or paste lyrics in step 2.")
    return None


def from_user(folder, text, log=print):
    """User-supplied lyrics: LRC text keeps its timing (checked and shifted); plain text is auto-timed."""
    folder = Path(folder)
    if TS.search(text):
        lines = parse_lrc(text)
        shift, share, sure = confident_shift(lines, folder)
        lines = [(max(0.0, t + shift), w) for t, w in lines]
        info = {"source": "user lrc", "shift_s": shift, "match_pct": round(100 * share), "approximate": not sure}
        log(f"Lyrics: {len(lines)} timed lines; " + (f"shifted {shift:+.2f} s to fit ({100 * share:.0f}% match)"
                                                     if sure else "kept their timing"))
    else:
        lines = time_plain(text, folder)
        info = {"source": "user text, auto-timed", "approximate": True}
        log(f"Lyrics: {len(lines)} lines, timed automatically from the vocal track (approximate)")
    save(folder, lines)
    return info


def nudge(folder, seconds):
    lines = load(folder)
    save(folder, [(max(0.0, t + seconds), w) for t, w in lines])
