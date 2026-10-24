"""Pitch-correct a vocal take, either to a key/scale or to the original song's melody.

Uses Praat's PSOLA resynthesis, so only the pitch changes; timing, breaths and
consonants are left alone.

  Key mode:     python tools/autotune.py take.wav --key "A minor"
  Auto key:     python tools/autotune.py take.wav --key-from original.mp3
  Melody mode:  python tools/autotune.py take.wav --melody original_vocals.wav --key-from original.mp3
  Check only:   python tools/autotune.py take.wav --key "A minor" --analyze
"""
import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402
from music import voiced_segments  # noqa: E402

# style -> (strength, retune time in ms)
STYLES = {"natural": (1.0, 50.0), "medium": (1.0, 20.0), "hard": (1.0, 0.0)}
STEP = 0.01


def nan_median_filter(x, width):
    padded = np.pad(x, width // 2, constant_values=np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out = np.nanmedian(sliding_window_view(padded, width), axis=1)
    out[np.isnan(x)] = np.nan
    return out


def melody_targets(midi, times, voiced, ref_path, offset, floor, ceiling, max_shift):
    """Follow the reference singer's notes (rounded to semitones), in the take's own octave."""
    ref_t, ref_f0 = music.track_pitch(music.load_sound(ref_path), floor, ceiling, STEP)
    ref_note = np.round(nan_median_filter(music.hz_to_midi(ref_f0), 5))
    idx = np.round((times + offset - ref_t[0]) / STEP).astype(int)
    inside = (idx >= 0) & (idx < len(ref_t))
    ref = np.full_like(midi, np.nan)
    ref[inside] = ref_note[idx[inside]]

    target = np.full_like(midi, np.nan)
    for seg in voiced_segments(voiced):
        diff = midi[seg] - ref[seg]
        if np.all(np.isnan(diff)):
            continue
        octave = 12 * np.round(np.nanmedian(diff) / 12)
        t = ref[seg] + octave
        t[np.abs(t - midi[seg]) > max_shift] = np.nan  # probably misaligned or a deliberate different note
        target[seg] = t
    return target


def smooth_correction(correction, voiced, retune_ms):
    if retune_ms <= 0:
        return correction
    alpha = 1 - np.exp(-STEP / (retune_ms / 1000))
    out = correction.copy()
    for seg in voiced_segments(voiced):
        s = correction[seg[0]]
        for i in seg:
            s += alpha * (correction[i] - s)
            out[i] = s
    return out


def report(label, error_semitones):
    cents = np.abs(error_semitones) * 100
    print(f"  {label:<7} avg {np.mean(cents):5.1f} cents off, {np.mean(cents <= 25) * 100:5.1f}% of sung frames within 25 cents")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("take", type=Path, help="your vocal recording (dry, vocal only)")
    p.add_argument("-o", "--output", type=Path, help="default: <take>_tuned.wav")
    p.add_argument("--key", help="e.g. 'A minor', 'F# major', 'Bb', 'chromatic'")
    p.add_argument("--key-from", type=Path, help="detect the key from this file (original song or instrumental)")
    p.add_argument("--melody", type=Path, help="original lead vocal (from separate.py) to follow note by note")
    p.add_argument("--offset", type=float, default=0.0, help="seconds to add to take time to line up with --melody")
    p.add_argument("--max-shift", type=float, default=5.0, help="melody mode: skip corrections bigger than this (semitones)")
    p.add_argument("--style", choices=STYLES, default="natural")
    p.add_argument("--strength", type=float, help="0-1, overrides the style")
    p.add_argument("--retune-ms", type=float, help="0 = instant/robotic, 50+ = gentle; overrides the style")
    p.add_argument("--floor", type=float, default=70.0, help="lowest pitch you sing, Hz")
    p.add_argument("--ceiling", type=float, default=900.0, help="highest pitch you sing, Hz")
    p.add_argument("--analyze", action="store_true", help="report pitch accuracy only, don't write a file")
    a = p.parse_args()

    strength, retune_ms = STYLES[a.style]
    strength = a.strength if a.strength is not None else strength
    retune_ms = a.retune_ms if a.retune_ms is not None else retune_ms

    if a.key or a.key_from:
        tonic, mode = music.resolve_key(a.key, a.key_from)
    elif a.melody:
        tonic, mode = 0, "chromatic"
    else:
        sys.exit("Give --key, --key-from, or --melody.")
    pcs = music.scale_pcs(tonic, mode)

    sound = music.load_sound(a.take)
    times, f0 = music.track_pitch(sound, a.floor, a.ceiling, STEP)
    voiced = ~np.isnan(f0)
    if not voiced.any():
        sys.exit("No singing detected; check --floor/--ceiling or the recording.")
    midi = np.where(voiced, music.hz_to_midi(np.where(voiced, f0, 1.0)), np.nan)

    target = music.key_targets(midi, voiced, pcs)
    if a.melody:
        mel = melody_targets(midi, times, voiced, a.melody, a.offset, a.floor, a.ceiling, a.max_shift)
        followed = ~np.isnan(mel)
        print(f"Following the melody on {followed.sum() / voiced.sum() * 100:.0f}% of sung frames"
              f" (rest snapped to {music.key_name(tonic, mode)})")
        target = np.where(followed, mel, target)

    correction = np.where(voiced, target - midi, 0.0)
    applied = strength * smooth_correction(correction, voiced, retune_ms)

    lo, hi = np.nanpercentile(midi, [2, 98])
    print(f"Take: {sound.xmax:.1f}s, {voiced.mean() * 100:.0f}% sung, range ~{music.midi_name(lo)}-{music.midi_name(hi)}")
    print(f"Key/scale: {music.key_name(tonic, mode)}; style strength={strength}, retune={retune_ms:.0f}ms")
    report("before", correction[voiced])
    report("after", (correction - applied)[voiced])
    if a.analyze:
        return

    tuned = music.psola(sound, times, f0 * 2 ** (applied / 12), a.floor, a.ceiling, STEP)
    out = a.output or a.take.with_name(f"{a.take.stem}_tuned.wav")
    sf.write(str(out), tuned, int(sound.sampling_frequency), subtype="FLOAT")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
