"""Shared music helpers: note/key parsing, key detection, pitch tracking.

Run directly to analyze a song:  python tools/music.py song.mp3
"""
import sys

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}
SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "chromatic": list(range(12)),
}

# Krumhansl-Kessler key profiles
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def parse_key(text):
    """'A minor', 'Am', 'F# major', 'Bb', 'chromatic' -> (tonic_pc, mode)."""
    t = text.strip()
    if t.lower() == "chromatic":
        return 0, "chromatic"
    parts = t.replace("_", " ").split()
    note = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    if len(parts) == 1 and note.endswith("m") and len(note) > 1:
        note, mode = note[:-1], "minor"
    mode = {"maj": "major", "min": "minor"}.get(mode, mode)
    note = note[0].upper() + note[1:]
    note = FLATS.get(note, note)
    if note not in NOTE_NAMES or mode not in SCALES:
        raise ValueError(f"Can't parse key {text!r}; try e.g. 'A minor', 'F# major', 'Bb'")
    return NOTE_NAMES.index(note), mode


def key_name(tonic, mode):
    return "chromatic" if mode == "chromatic" else f"{NOTE_NAMES[tonic]} {mode}"


def scale_pcs(tonic, mode):
    return sorted((tonic + d) % 12 for d in SCALES[mode])


def hz_to_midi(hz):
    return 69 + 12 * np.log2(hz / 440.0)


def midi_to_hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def midi_name(midi):
    n = int(round(midi))
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def detect_key(path):
    """Estimate key from audio. Returns (tonic_pc, mode, confidence 0-1)."""
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True)
    harmonic = librosa.effects.harmonic(y)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr).mean(axis=1)
    scores = []
    for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
        for tonic in range(12):
            scores.append((np.corrcoef(chroma, np.roll(profile, tonic))[0, 1], tonic, mode))
    scores.sort(reverse=True)
    best, runner_up = scores[0], scores[1]
    return best[1], best[2], float(best[0] - runner_up[0])


def detect_tempo(path):
    import librosa

    y, sr = librosa.load(path, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return float(np.atleast_1d(tempo)[0])


def load_sound(path):
    """Load audio as a mono parselmouth Sound (keeps the file's sample rate)."""
    import parselmouth
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return parselmouth.Sound(data.mean(axis=1), sampling_frequency=sr)


def track_pitch(sound, floor=70.0, ceiling=900.0, step=0.01):
    """Returns (times, f0_hz) with NaN where unvoiced."""
    pitch = sound.to_pitch_cc(time_step=step, pitch_floor=floor, pitch_ceiling=ceiling)
    f0 = pitch.selected_array["frequency"].astype(float)
    f0[f0 <= 0] = np.nan
    return pitch.xs(), f0


def psola(sound, times, new_f0, floor=70.0, ceiling=900.0, step=0.01):
    """Resynthesize `sound` with pitch `new_f0` (Hz, NaN = no point) at `times`.

    Praat overlap-add: timing, consonants and vowel character (formants) are kept.
    Returns a numpy array at the sound's sample rate.
    """
    from parselmouth.praat import call

    manipulation = call(sound, "To Manipulation", step, floor, ceiling)
    tier = call("Create PitchTier", "new", sound.xmin, sound.xmax)
    for t, f in zip(times, new_f0):
        if not np.isnan(f):
            call(tier, "Add point", float(t), float(f))
    call([manipulation, tier], "Replace pitch tier")
    return call(manipulation, "Get resynthesis (overlap-add)").values[0]


def voiced_segments(voiced):
    """Yield index arrays of consecutive voiced frames."""
    edges = np.flatnonzero(np.diff(np.r_[0, voiced.astype(int), 0]))
    for start, end in zip(edges[::2], edges[1::2]):
        yield np.arange(start, end)


def snap_to_scale(midi, pcs, prev, hysteresis=0.15):
    """Nearest in-scale note; sticks with the previous note unless clearly closer to another."""
    base = int(np.floor(midi))
    best = min((n for n in range(base - 2, base + 3) if n % 12 in pcs), key=lambda n: abs(n - midi))
    if prev is not None and abs(midi - prev) < abs(midi - best) + hysteresis:
        return prev
    return best


def key_targets(midi, voiced, pcs):
    """Per-frame in-scale note for each voiced frame (NaN elsewhere)."""
    target = np.full_like(midi, np.nan)
    for seg in voiced_segments(voiced):
        prev = None
        for i in seg:
            prev = target[i] = snap_to_scale(midi[i], pcs, prev)
    return target


def resolve_key(key=None, key_from=None):
    """Key from a name or detected from a file; prints what it detected."""
    if key:
        return parse_key(key)
    tonic, mode, conf = detect_key(key_from)
    print(f"Detected key: {key_name(tonic, mode)} (confidence margin {conf:.3f})")
    return tonic, mode


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tools/music.py <song file>")
    tonic, mode, conf = detect_key(sys.argv[1])
    print(f"Key:   {key_name(tonic, mode)}  (confidence margin {conf:.3f}; < 0.02 = unsure)")
    print(f"Tempo: {detect_tempo(sys.argv[1]):.1f} BPM")
