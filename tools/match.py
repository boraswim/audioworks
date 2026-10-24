"""Make a vocal sound produced like a reference vocal (e.g. the original singer's stem).

Measures the reference, then applies to your vocal:
  1. matching EQ (smoothed, boosts capped so it never amplifies what isn't there)
  2. a leveler (plus a lift for soft individual words) and compression, chosen to reach
     the reference's loudness evenness
  3. room: if your take is drier than the reference, a reverb with the reference's own decay
     time, as quiet as matches its room tail (none when what follows the reference's phrases
     doesn't decay, i.e. it's bleed or noise); an expander between phrases if your take is
     roomier. A stereo reverb in the reference becomes a stereo room (stereo output)
  4. perceived loudness (LUFS), matched to the reference (--gain to sit louder/quieter)
  5. a peak limiter matching the reference's peak-to-average ratio (never above -1 dBFS)
  6. with --hiss: steady noise matching the reference's noise floor (tape hiss on old recordings)

  python tools/match.py take_tuned.wav --ref original_vocals.wav
  python tools/match.py take_tuned.wav --ref original_vocals.wav --hiss --max-cut 40
  python tools/match.py take_tuned.wav --ref original_vocals.wav --analyze
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import binary_dilation, minimum_filter1d, uniform_filter1d
from scipy.signal import firwin2, oaconvolve, welch

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402

BLOCK = 0.05


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))


def block_rms(m, sr, block=BLOCK):
    n = int(block * sr)
    k = len(m) // n
    return np.sqrt((m[: k * n].reshape(k, n) ** 2).mean(axis=1)), n


def active_blocks(m, sr):
    r, n = block_rms(m, sr)
    rd = db(r)
    return rd > np.percentile(rd, 95) - 20, r, n


def active_signal(m, sr):
    act, _, n = active_blocks(m, sr)
    return m[: len(act) * n].reshape(len(act), n)[act].ravel()


def sung_mask(m, sr):
    """Per-sample mask of pitched (sung) moments. Processing never moves audio in time, so it's computed once."""
    import parselmouth

    times, f0 = music.track_pitch(parselmouth.Sound(m, sampling_frequency=sr))
    idx = np.clip(np.round((np.arange(len(m)) / sr - times[0]) / (times[1] - times[0])).astype(int), 0, len(f0) - 1)
    return ~np.isnan(f0)[idx]


def metrics(m, sr, sung=None):
    """Level and loudness spread are measured on sung moments when a mask is given
    (breaths and room noise would otherwise count as quiet singing)."""
    act, r, n = active_blocks(m, sr)
    sig = m[sung] if sung is not None else m[: len(act) * n].reshape(len(act), n)[act].ravel()
    w = int(0.4 * sr)
    lv = db(np.sqrt((sig[: len(sig) // w * w].reshape(-1, w) ** 2).mean(axis=1)))
    tails = []
    for e in np.flatnonzero(act[:-1] & ~act[1:]):
        post = r[e + 3:e + 8]
        if len(post) == 5 and not act[e + 1:e + 8].any():
            tails.append(db(post.mean() / r[max(e - 6, 0):e + 1].mean()))
    level = db(np.sqrt(np.mean(sig ** 2)))
    return {
        "level": level,
        "spread": np.percentile(lv, 90) - np.percentile(lv, 10),
        "tail": np.median(tails) if tails else float("nan"),
        "crest": db(np.abs(m).max()) - level,
    }


def ltas_db(m, sr):
    f, p = welch(active_signal(m, sr), sr, nperseg=4096)
    return f, 10 * np.log10(p + 1e-20)


def eq_curve(take, ref, sr, max_boost, max_cut):
    f, t = ltas_db(take, sr)
    _, r = ltas_db(ref, sr)
    mid = (f >= 600) & (f < 1200)
    diff = (r - r[mid].mean()) - (t - t[mid].mean())
    # smooth over 1/3 octave
    smooth = np.array([diff[(f >= fc * 2 ** (-1 / 6)) & (f <= fc * 2 ** (1 / 6))].mean() if fc > 0 else 0 for fc in f])
    gain = np.clip(smooth, -max_cut, max_boost)
    weak = t < t.max() - 50  # nothing there worth boosting
    gain[weak] = np.minimum(gain[weak], 0)
    gain[(f < 60) | (f > 16000)] = np.minimum(gain[(f < 60) | (f > 16000)], 0)
    return f, gain


def apply_eq(m, sr, f, gain_db):
    h = firwin2(4097, f / (sr / 2), 10 ** (gain_db / 20))
    return oaconvolve(m, h, mode="same")


def smooth_gain(g, block, attack, release):
    """One-pole smoothing of a per-block gain (dB); attack applies when gain goes down."""
    a_att, a_rel = 1 - np.exp(-block / attack), 1 - np.exp(-block / release)
    out = np.empty_like(g)
    s = g[0]
    for i, x in enumerate(g):
        s += (a_att if x < s else a_rel) * (x - s)
        out[i] = s
    return out


def apply_block_gain(m, sr, gain_db, block):
    centers = (np.arange(len(gain_db)) + 0.5) * block * sr
    return m * 10 ** (np.interp(np.arange(len(m)), centers, gain_db) / 20)


def sung_blocks(sung, sr, block=BLOCK):
    n = int(block * sr)
    k = len(sung) // n
    return sung[: k * n].reshape(k, n).mean(axis=1) > 0.5


def level_ride(m, sr, sung, max_gain=12.0, amount=1.0):
    """Ride sung phrases toward their median level (lifts soft notes, tames loud ones).

    `amount` below 1 applies only part of the correction, for references that keep more dynamics.
    """
    r, _ = block_rms(m, sr)
    act = sung_blocks(sung, sr)[: len(r)]
    rd = db(r)
    known = np.flatnonzero(act)
    # level reading from sung blocks only, so gaps between words don't drag it down
    env = smooth_gain(np.interp(np.arange(len(rd)), known, rd[known]), BLOCK, 0.25, 0.25)
    g = np.clip(np.median(rd[act]) - env, -max_gain, max_gain) * amount
    return apply_block_gain(m, sr, smooth_gain(g, BLOCK, 0.1, 0.1), BLOCK)


def lift_dips(m, sr, sung, allowed_db=3.0, max_gain=10.0):
    """Fast upward-only leveler: lifts sung moments more than `allowed_db` below the typical sung level.

    Reacts in ~150 ms, so it catches soft words inside long phrases; never turns anything down.
    """
    block = 0.01
    act = sung_blocks(sung, sr, block)
    r, _ = block_rms(m, sr, block)
    act = act[: len(r)]
    known = np.flatnonzero(act)
    # level from sung blocks only (gaps hold the neighbouring level), smoothed over ~150 ms
    level = np.interp(np.arange(len(r)), known, db(r)[known])
    env = 10 * np.log10(uniform_filter1d(10 ** (level / 10), 15))
    g = np.clip(np.median(level[act]) - allowed_db - env, 0, max_gain)
    g = uniform_filter1d(g, 5)  # ~50 ms fades
    return apply_block_gain(m, sr, g, block)


def lufs(m, sr):
    import pyloudnorm

    return pyloudnorm.Meter(sr).integrated_loudness(m)


def limit(m, sr, ceiling_db):
    """Brickwall peak limiter: no sample above the ceiling, gain changes smoothed over ~3 ms."""
    w = int(0.003 * sr)
    peak = np.abs(m).max(axis=1) if m.ndim == 2 else np.abs(m)  # stereo: both channels get the same gain
    need = np.minimum(1.0, 10 ** (ceiling_db / 20) / np.maximum(peak, 1e-12))
    # min over +-w then mean over w keeps every sample's gain at or below what it needs
    g = uniform_filter1d(minimum_filter1d(need, 2 * w + 1), w + 1)
    return m * (g[:, None] if m.ndim == 2 else g)


def compress(m, sr, ratio, threshold_db):
    block = 0.005
    r, _ = block_rms(m, sr, block)
    over = np.maximum(db(r) - threshold_db, 0)
    g = smooth_gain(-over * (1 - 1 / ratio), block, 0.005, 0.12)
    return apply_block_gain(m, sr, g, block)


def expand(m, sr, range_db, threshold_db, sung=None):
    """Turn down quiet stretches (room tail, breaths). Never touches sung moments when a mask is given."""
    block = 0.01
    r, _ = block_rms(m, sr, block)
    env = smooth_gain(db(r), block, 0.03, 0.03)  # steadier level reading
    g = -range_db * np.clip((threshold_db - env) / 6, 0, 1)
    if sung is not None:
        protect = np.convolve(sung_blocks(sung, sr, block)[: len(g)], np.ones(5), mode="same") > 0  # +-20 ms
        g[protect] = 0
    g = smooth_gain(g, block, 0.04, 0.01)  # close gently, open fast
    return apply_block_gain(m, sr, g, block)


def add_room(m, sr, wet_db, rt60=0.6, seed=3):
    """Small synthetic room: an exponentially decaying noise impulse response mixed in at `wet_db`."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(rt60 * 1.5 * sr)) / sr
    ir = rng.standard_normal(len(t)) * 10 ** (-3 * t / rt60)  # -60 dB at rt60
    ir[: int(0.008 * sr)] = 0  # ~8 ms before the first reflections
    ir /= np.sqrt(np.sum(ir ** 2))
    return m + oaconvolve(m, ir)[: len(m)] * 10 ** (wet_db / 20)


def side_mid_db(side, mid, mask):
    return db(np.sqrt(np.mean(side[mask] ** 2)) / max(np.sqrt(np.mean(mid[mask] ** 2)), 1e-12))


def reverb_profile(ref, sr):
    """The reference's reverb, read from what follows its phrases.

    rt60: decay time in seconds, or None when the gaps hold steady sound (instrument bleed, noise)
          instead of something decaying: then there's no reverb to copy, whatever the tail level says
    stereo: the gaps are as wide as they are loud (side ~ mid), so the reverb is a stereo one; a vocal
          that is wide only through its reverb gets a stereo room, not double-tracking
    """
    m = ref.mean(axis=1)
    act, r, _ = active_blocks(m, sr)
    floor = np.percentile(db(r), 5)
    slopes, gaps = [], []
    for e in np.flatnonzero(act[:-1] & ~act[1:]):
        if e + 16 > len(act) or act[e + 1:e + 8].any():
            continue
        gaps.extend(range(e + 3, e + 8))
        seg = db(r[e + 1:e + 16])  # 50-800 ms after the phrase, read only above the noise floor
        ok = seg > floor + 6
        if ok.sum() >= 4:
            slopes.append(np.polyfit(np.flatnonzero(ok) * BLOCK, seg[ok], 1)[0])
    decay = float(np.median(slopes)) if len(slopes) >= 5 else 0.0
    # vocal reverbs run ~0.4-2.5 s; slower than -8 dB/s (RT60 > 7.5 s) isn't a decay at all
    rt60 = float(np.clip(-60 / decay, 0.4, 2.5)) if decay < -8 else None
    tail_sm = None
    if ref.shape[1] == 2 and gaps:
        rs, _ = block_rms((ref[:, 0] - ref[:, 1]) / 2, sr)
        tail_sm = float(side_mid_db(rs, r, np.array(gaps)))
    return {"rt60": rt60, "decay_db_per_s": round(decay, 1), "tail_side_mid_db": tail_sm,
            "stereo": bool(rt60 and tail_sm is not None and tail_sm > -6)}


def stereo_room(y, sr, rt60, target_side_mid, sung, seed=11):
    """Spread a mono vocal's room into stereo: a decorrelated reverb on the side channel only, so the
    middle (everything the other stages matched) stays exactly as it is."""
    side = add_room(y, sr, 0.0, rt60, seed) - y  # the wet part alone, from its own impulse response
    side *= 10 ** ((target_side_mid - side_mid_db(side, y, sung)) / 20)
    return np.stack([y + side, y - side], axis=1)


def noise_floor(m, sr, sung):
    """Quietest stretches well away from any singing: the recording's own noise. None if too little."""
    far = ~binary_dilation(sung, np.ones(int(0.6 * sr)))
    r, n = block_rms(m, sr)
    blocks = m[: len(r) * n].reshape(len(r), n)
    cand = np.flatnonzero(far[: len(r) * n].reshape(len(r), n).all(axis=1))
    if len(cand) * BLOCK < 2:
        return None
    quiet = cand[r[cand] <= np.percentile(r[cand], 30)]
    return blocks[quiet].ravel()


def quiet_gaps(m, sr, sung, target_floor_db, before=0.4, after=0.4):
    """Turn your background noise down to `target_floor_db` between phrases.

    Only stretches already within 6 dB of your own noise floor are touched, so a room's echo
    fades out naturally instead of being chopped. Leaves `before` seconds ahead of singing
    (breaths) and `after` seconds behind it alone.
    """
    floor = noise_floor(m, sr, sung)
    if floor is None:
        return m, 0.0
    floor_db = db(np.sqrt(np.mean(floor ** 2)))
    cut = floor_db - target_floor_db
    if cut <= 0:
        return m, 0.0
    block = 0.01
    s = np.r_[0, np.cumsum(sung_blocks(sung, sr, block))]
    i = np.arange(len(s) - 1)
    lo, hi = np.clip(i - int(after / block), 0, len(i)), np.clip(i + int(before / block) + 1, 0, len(i))
    far = (s[hi] - s[lo]) == 0  # no singing from `after` s ago to `before` s ahead
    r, _ = block_rms(m, sr, block)
    level = 10 * np.log10(uniform_filter1d(r ** 2, 3) + 1e-24)  # ~30 ms reading
    n = min(len(far), len(level))
    # full cut at the noise floor, easing to none 6 dB above it
    depth = np.clip((floor_db + 6 - level[:n]) / 6, 0, 1)
    g = np.where(far[:n], -cut * depth, 0.0)
    g = uniform_filter1d(g, 5)  # ~50 ms fades
    return apply_block_gain(m, sr, g, block), cut


def make_hiss(floor, n, sr, seed=5):
    """Steady noise with the spectrum and level of `floor`."""
    f, p = welch(floor, sr, nperseg=2048)
    shape = firwin2(2049, f / (sr / 2), np.sqrt(p / p.max()))
    hiss = oaconvolve(np.random.default_rng(seed).standard_normal(n), shape, mode="same")
    return hiss * np.sqrt(np.mean(floor ** 2) / np.mean(hiss ** 2))


def show(name, mt):
    print(f"  {name:<10} level {mt['level']:6.1f} dBFS   loudness spread {mt['spread']:4.1f} dB"
          f"   room tail {mt['tail']:6.1f} dB   peak-to-average {mt['crest']:4.1f} dB")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vocal", type=Path)
    p.add_argument("--ref", type=Path, required=True, help="reference vocal, e.g. song_vocals.wav from separate.py")
    p.add_argument("-o", "--output", type=Path, help="default: <vocal>_matched.wav")
    p.add_argument("--max-boost", type=float, default=6.0, help="dB")
    p.add_argument("--max-cut", type=float, default=12.0, help="dB")
    p.add_argument("--gain", type=float, default=0.0, help="dB louder (or quieter) than the reference vocal")
    p.add_argument("--hiss", action="store_true", help="add noise matching the reference's noise floor")
    p.add_argument("--sung-from", type=Path,
                   help="take the sung moments from this file instead (same timing), e.g. the tuned take when "
                        "matching a vocoded version, where pitch detection misses some moments")
    p.add_argument("--analyze", action="store_true")
    a = p.parse_args()

    x, sr = sf.read(str(a.vocal), always_2d=True)
    ref, ref_sr = sf.read(str(a.ref), always_2d=True)
    if ref_sr != sr:
        import soxr
        ref = soxr.resample(ref, ref_sr, sr)
    m, rm = x.mean(axis=1), ref.mean(axis=1)
    if a.sung_from:
        src, _ = sf.read(str(a.sung_from), always_2d=True)
        sung = sung_mask(src.mean(axis=1)[: len(m)], sr)
        sung = np.pad(sung, (0, len(m) - len(sung)))
    else:
        sung = sung_mask(m, sr)
    ref_sung = sung_mask(rm, sr)

    def mt(y):
        return metrics(y, sr, sung)

    target, before = metrics(rm, sr, ref_sung), mt(m)
    print("Before:")
    show("reference", target)
    show("yours", before)

    f, gain = eq_curve(m, rm, sr, a.max_boost, a.max_cut)
    bands = [(100, 200), (200, 400), (400, 800), (800, 1600), (1600, 3200), (3200, 6400), (6400, 12800)]
    print("EQ: " + "  ".join(f"{lo}-{hi} Hz {gain[(f >= lo) & (f < hi)].mean():+.1f}" for lo, hi in bands))
    if a.analyze:
        return
    y = apply_eq(m, sr, f, gain)

    if mt(y)["spread"] > target["spread"] + 3:
        # as much leveling as it takes to reach the reference's evenness, not more
        cands = [(abs(mt(z)["spread"] - target["spread"]), amt, z)
                 for amt in (0.25, 0.5, 0.75, 1.0) for z in [lift_dips(level_ride(y, sr, sung, amount=amt), sr, sung)]]
        _, amount, y = min(cands, key=lambda c: c[:2])
        print(f"Leveler: {int(amount * 100)}% (spread now {mt(y)['spread']:.1f} dB)")
    else:
        # overall evenness already matches, but soft words can still dip; the user wants none
        y = lift_dips(y, sr, sung)
        print(f"Soft-word lift only (spread now {mt(y)['spread']:.1f} dB)")
    if mt(y)["spread"] > target["spread"] + 0.5:
        r, _ = block_rms(y, sr)
        levels = db(r[sung_blocks(sung, sr)[: len(r)]])
        cands = []
        for pct in (10, 30, 50):
            thr = np.percentile(levels, pct)
            for q in (1, 1.5, 2, 3, 4, 6, 8, 12):
                # prefer the gentlest setting that gets within 0.5 dB
                cands.append((max(abs(mt(compress(y, sr, q, thr))["spread"] - target["spread"]), 0.5), q, -pct, thr))
        _, ratio, _, threshold = min(cands)
        y = compress(y, sr, ratio, threshold)
        print(f"Compression: ratio {ratio}:1 above {threshold:.1f} dBFS")
    else:
        print("Compression: not needed (already as even as the reference)")

    rev = reverb_profile(ref, sr)
    if not np.isnan(target["tail"]) and mt(y)["tail"] < target["tail"] - 1:
        if rev["rt60"] is None:
            print(f"Room: none added (the reference's tail of {target['tail']:.1f} dB doesn't decay "
                  f"({rev['decay_db_per_s']:+.0f} dB/s): it's bleed or noise, not a reverb)")
        else:
            # a room with the reference's own decay time; the quietest that gets within 1 dB of its tail.
            # Never wetter than -8 dB: matching a long reverb's tail with a short loud room sounded like an echo
            rt60 = round(rev["rt60"], 1)
            cands = [(max(abs(mt(add_room(y, sr, w, rt60))["tail"] - target["tail"]), 1.0), w) for w in range(-30, -7)]
            _, wet = min(cands)
            y = add_room(y, sr, wet, rt60)
            print(f"Room: {rt60} s reverb (the reference's decay, {rev['decay_db_per_s']:+.0f} dB/s) at {wet:+d} dB "
                  f"(tail now {mt(y)['tail']:.1f} dB)")
    elif not np.isnan(target["tail"]):
        act, r, _ = active_blocks(y, sr)
        top = np.percentile(db(r[act]), 95)
        # prefer the lowest threshold, then the smallest range, that gets within 1 dB
        cands = [(max(abs(mt(expand(y, sr, rng, top - off, sung))["tail"] - target["tail"]), 1.0), -off, rng)
                 for off in (22, 16, 10) for rng in (0, 6, 12, 18, 24, 30)]
        _, neg_off, best_rng = min(cands)
        y = expand(y, sr, best_rng, top + neg_off, sung)
        print(f"Expander: up to {best_rng} dB below {top + neg_off:.1f} dBFS (sung moments untouched)")

    # level by perceived loudness (LUFS), which is what the ear compares in a mix
    target_lufs = lufs(rm, sr) + a.gain
    y *= 10 ** ((target_lufs - lufs(y, sr)) / 20)
    # limiting lowers the loudness a little; re-level and re-limit until it settles
    ceiling = min(target["level"] + target["crest"], -1.0)
    for _ in range(4):
        y = limit(y, sr, ceiling)
        y *= 10 ** ((target_lufs - lufs(y, sr)) / 20)
    y = limit(y, sr, ceiling)
    print(f"Limiter: ceiling {ceiling:.1f} dBFS")
    if a.hiss:
        floor = noise_floor(rm, sr, ref_sung)
        if floor is None:
            print("Hiss: skipped (reference has under 2 s away from singing to measure its noise)")
        else:
            floor = floor * 10 ** (a.gain / 20)  # keep the same noise-to-voice relation
            floor_db = db(np.sqrt(np.mean(floor ** 2)))
            # your own background noise first comes down to the reference's floor, then hiss fills it
            y, cut = quiet_gaps(y, sr, sung, floor_db - 6)
            y = limit(y + make_hiss(floor, len(y), sr), sr, ceiling)
            print(f"Hiss: added at {floor_db:.1f} dBFS RMS (reference's noise floor)"
                  + (f"; your background between phrases lowered {cut:.0f} dB first" if cut else ""))
    print("After:")
    show("reference", target)
    show("yours", mt(y))
    print(f"  loudness   reference {lufs(rm, sr):.1f} LUFS, yours {lufs(y, sr):.1f} LUFS (target {target_lufs:.1f})")
    if rev["stereo"]:
        width = side_mid_db((ref[:, 0] - ref[:, 1]) / 2, rm, ref_sung)
        y = limit(stereo_room(y, sr, rev["rt60"], width, sung), sr, ceiling)
        print(f"Stereo room: the reference's reverb is stereo (tails side/mid {rev['tail_side_mid_db']:+.1f} dB); "
              f"spread to side/mid {side_mid_db((y[:, 0] - y[:, 1]) / 2, y.mean(axis=1), sung):.1f} dB "
              f"while singing (reference {width:.1f})")
    out = a.output or a.vocal.with_name(f"{a.vocal.stem}_matched.wav")
    sf.write(str(out), y, sr, subtype="FLOAT")
    print(f"Wrote {out}  (peak {db(np.abs(y).max()):.1f} dBFS)")


if __name__ == "__main__":
    main()
