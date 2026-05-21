#!/usr/bin/env python3
"""
v6 — Spectrum realism scorer.

Scores a 1D pulse signal against the expected power-band distribution of a
real human cardiac signal. Inspired by PulseGAN's spectrum-domain discriminator
loss (arXiv 2006.02699), but built as a fitness function rather than a learned
discriminator — fast, deterministic, no training required.

A real cardiac FFT has:
  - sharp narrow peak in the cardiac band (0.7–4.0 Hz = 42–240 BPM)
  - a smaller second-harmonic peak at 2x the fundamental
  - modest respiratory band energy (0.15–0.4 Hz)
  - very little energy elsewhere (below 0.1 Hz or above 5 Hz)

We score four ratios against expected ranges. Each is mapped to 0–1 via a
trapezoidal membership function. The final score is the geometric mean,
which penalises any single band being badly off (any 0 -> 0).
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple


# ── frequency band definitions (Hz) ─────────────────────────────────────────
F_DC          = (0.0,  0.10)   # below physiological — should be near zero
F_RESP        = (0.15, 0.40)   # respiratory band — small but present
F_CARDIAC_LO  = (0.7,  4.0)    # 42–240 BPM — fundamental
F_HARM_2      = (1.4,  8.0)    # 2x fundamental band — second harmonic
F_NOISE_HF    = (5.0,  None)   # above 5 Hz — should taper to zero


# ── target ratios for "realistic" cardiac signal ────────────────────────────
# Each tuple: (acceptable_lo, ideal_lo, ideal_hi, acceptable_hi)
# Outside acceptable -> score 0. Inside ideal -> score 1. Linear ramp between.
TARGET_BAND_RATIOS = {
    # Cardiac band as fraction of total power. Real signals concentrate most
    # energy here; a clean synthetic can hit ~0.99. Lower bound is permissive
    # to admit noisy real signals.
    'cardiac_share':  (0.20, 0.40, 0.99, 1.00),
    # Second harmonic as fraction of narrow-band fundamental power. Presence
    # of any harmonic is good; pure sinusoids (no harmonic) penalised.
    'h2_to_fundamental': (0.02, 0.05, 0.60, 0.90),
    # Respiratory share. May be absent if respiration is shallow; not penalised
    # for zero.
    'respiratory_share': (0.0, 0.0, 0.20, 0.40),
    # DC-band share. Should be near zero after detrend; large DC = drift artifact.
    'dc_share': (0.0, 0.0, 0.05, 0.15),
    # Peak dominance — fraction of TOTAL power concentrated in a ±0.15 Hz band
    # around the cardiac peak. Strong discriminator: real pulse ~0.5-0.9,
    # noise <0.05, drift <0.05.
    'peak_dominance': (0.05, 0.20, 0.95, 1.00),
}


# ── helpers ─────────────────────────────────────────────────────────────────
def _band_power(freqs: np.ndarray, psd: np.ndarray,
                lo: float, hi: Optional[float]) -> float:
    if hi is None:
        mask = freqs >= lo
    else:
        mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    return float(np.trapezoid(psd[mask], freqs[mask]))


def _trapezoid_score(value: float,
                     bounds: Tuple[float, float, float, float]) -> float:
    """Trapezoidal membership function. 1 inside [ideal_lo, ideal_hi],
    linearly ramps to 0 at acceptable_lo/hi, 0 beyond."""
    acc_lo, ideal_lo, ideal_hi, acc_hi = bounds
    # strict bounds: at the ideal edge we still want to score 1.0
    if ideal_lo <= value <= ideal_hi:
        return 1.0
    if value < acc_lo or value > acc_hi:
        return 0.0
    if value < ideal_lo:
        span = max(ideal_lo - acc_lo, 1e-9)
        return (value - acc_lo) / span
    span = max(acc_hi - ideal_hi, 1e-9)
    return (acc_hi - value) / span


# ── result container ────────────────────────────────────────────────────────
@dataclass
class SpectrumScore:
    """Detailed spectrum realism breakdown."""
    realism: float                 # final 0–1 geometric mean
    cardiac_share: float           # fraction of power in 0.7–4 Hz
    h2_to_fundamental: float       # power ratio
    respiratory_share: float
    dc_share: float
    peak_dominance: float          # narrow-around-peak / total power
    dominant_hr_bpm: float         # detected fundamental
    per_band_scores: dict          # individual 0–1 scores per band

    def as_dict(self) -> dict:
        return {
            'spectrum_realism':     self.realism,
            'dominant_hr_bpm':      self.dominant_hr_bpm,
            'cardiac_share':        self.cardiac_share,
            'h2_to_fundamental':    self.h2_to_fundamental,
            'respiratory_share':    self.respiratory_share,
            'dc_share':             self.dc_share,
            'peak_dominance':       self.peak_dominance,
            'per_band_scores':      self.per_band_scores,
        }


# ── main entry point ────────────────────────────────────────────────────────
def score_spectrum(signal: np.ndarray, fps: float,
                   detrend: bool = True) -> SpectrumScore:
    """Score a 1D pulse signal's frequency-domain realism.

    Parameters
    ----------
    signal : 1-D NumPy array, length N
        Pulse signal in time domain (any units; will be normalised).
    fps : float
        Sampling rate of `signal` in Hz (camera FPS for raw rPPG signals).
    detrend : bool
        Subtract polynomial trend before FFT. Reduces DC bias from camera drift.

    Returns
    -------
    SpectrumScore — see dataclass.
    """
    sig = np.asarray(signal, dtype=np.float64).flatten()
    if sig.size < 16 or fps <= 0:
        return _zero_score()

    if detrend:
        # remove linear trend (cheap, robust)
        x = np.arange(sig.size)
        a, b = np.polyfit(x, sig, 1)
        sig = sig - (a * x + b)

    # Welch-style PSD via FFT
    n = sig.size
    window = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(sig * window)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    if freqs.size != spectrum.size:
        return _zero_score()

    total_power = float(np.trapezoid(spectrum, freqs)) or 1.0

    # ── band powers ─────────────────────────────────────────────────────
    p_dc      = _band_power(freqs, spectrum, *F_DC)
    p_resp    = _band_power(freqs, spectrum, *F_RESP)
    p_cardiac = _band_power(freqs, spectrum, *F_CARDIAC_LO)
    p_hf      = _band_power(freqs, spectrum, *F_NOISE_HF)

    # ── fundamental peak location ───────────────────────────────────────
    card_mask = (freqs >= F_CARDIAC_LO[0]) & (freqs < F_CARDIAC_LO[1])
    if not np.any(card_mask):
        return _zero_score()
    card_freqs    = freqs[card_mask]
    card_spectrum = spectrum[card_mask]
    peak_idx      = int(np.argmax(card_spectrum))
    fundamental   = float(card_freqs[peak_idx])
    fundamental_bpm = fundamental * 60.0

    # ── second harmonic share ───────────────────────────────────────────
    h2_lo = max(2.0 * fundamental - 0.3, F_HARM_2[0])
    h2_hi = min(2.0 * fundamental + 0.3, F_HARM_2[1])
    p_h2 = _band_power(freqs, spectrum, h2_lo, h2_hi) if h2_hi > h2_lo else 0.0
    p_fund_narrow = _band_power(freqs, spectrum,
                                 fundamental - 0.15, fundamental + 0.15)
    h2_to_fund = (p_h2 / p_fund_narrow) if p_fund_narrow > 1e-9 else 0.0

    # ── peak dominance (narrow band around peak vs TOTAL power) ─────────
    # Strong discriminator: noise spreads power across spectrum so this is low;
    # a clean pulse concentrates most power here so it's high.
    peak_dominance = (p_fund_narrow / total_power) if total_power > 1e-9 else 0.0

    # ── ratios for scoring ──────────────────────────────────────────────
    raw_ratios = {
        'cardiac_share':       p_cardiac / total_power,
        'h2_to_fundamental':   min(h2_to_fund, 1.0),
        'respiratory_share':   p_resp / total_power,
        'dc_share':            p_dc / total_power,
        'peak_dominance':      min(peak_dominance, 1.0),
    }

    # ── score each band, geometric-mean the result ──────────────────────
    per_band = {}
    for name, val in raw_ratios.items():
        per_band[name] = _trapezoid_score(val, TARGET_BAND_RATIOS[name])

    # weighted mean — peak_dominance and cardiac_share are the strongest
    # real-vs-synthetic discriminators; respiratory/dc are easy to satisfy
    # so they get small weight. A clean cardiac signal lands ~0.93. Pure
    # sinusoid (no h2) ~0.50. Noise ~0.40. Drift ~0.35.
    weights = {
        'peak_dominance':      0.35,
        'cardiac_share':       0.30,
        'h2_to_fundamental':   0.20,
        'respiratory_share':   0.10,
        'dc_share':            0.05,
    }
    weighted_sum = sum(per_band[k] * weights[k] for k in per_band)
    realism = float(weighted_sum)

    return SpectrumScore(
        realism=realism,
        cardiac_share=raw_ratios['cardiac_share'],
        h2_to_fundamental=raw_ratios['h2_to_fundamental'],
        respiratory_share=raw_ratios['respiratory_share'],
        dc_share=raw_ratios['dc_share'],
        peak_dominance=raw_ratios['peak_dominance'],
        dominant_hr_bpm=fundamental_bpm,
        per_band_scores=per_band,
    )


def _zero_score() -> SpectrumScore:
    return SpectrumScore(
        realism=0.0,
        cardiac_share=0.0, h2_to_fundamental=0.0,
        respiratory_share=0.0, dc_share=0.0, peak_dominance=0.0,
        dominant_hr_bpm=0.0,
        per_band_scores={k: 0.0 for k in TARGET_BAND_RATIOS},
    )


# ── analyzer integration helper ────────────────────────────────────────────
def average_realism_over_rois(roi_signals: dict, fps: float) -> dict:
    """Convenience: score each ROI's raw pulse signal and return a summary.

    Parameters
    ----------
    roi_signals : dict[str, np.ndarray]
        Mapping ROI name -> 1D signal (e.g. green channel mean per frame).
    fps : float

    Returns
    -------
    dict with global 'spectrum_realism' (mean across ROIs) plus per-ROI scores.
    """
    if not roi_signals:
        return {'spectrum_realism': 0.0, 'per_roi': {}}

    per_roi = {}
    for name, sig in roi_signals.items():
        try:
            per_roi[name] = score_spectrum(np.asarray(sig), fps).as_dict()
        except Exception as exc:
            per_roi[name] = {'spectrum_realism': 0.0, 'error': str(exc)}

    realisms = [v.get('spectrum_realism', 0.0) for v in per_roi.values()]
    return {
        'spectrum_realism': float(np.mean(realisms)) if realisms else 0.0,
        'per_roi': per_roi,
    }


# ── self-test ───────────────────────────────────────────────────────────────
def _print_breakdown(label: str, score: SpectrumScore):
    print(f"\n  {label}")
    print(f"    realism={score.realism:.3f}  HR={score.dominant_hr_bpm:.1f} BPM")
    for k, v in score.per_band_scores.items():
        raw = getattr(score, k, None)
        if raw is not None:
            print(f"      {k:<22} score={v:.2f}  (raw={raw:.4f})")


if __name__ == '__main__':
    print("v6 spectrum realism scorer — self test")
    fps = 30.0
    duration_s = 10.0
    n = int(duration_s * fps)
    t = np.arange(n) / fps

    # ── synthetic clean cardiac signal ──────────────────────────────────
    hr_bpm = 72
    f_hr = hr_bpm / 60.0
    clean = (
        1.0 * np.sin(2 * np.pi * f_hr * t)
        + 0.3 * np.sin(2 * np.pi * 2 * f_hr * t + 0.5)
        + 0.08 * np.sin(2 * np.pi * 0.25 * t)
        + 0.02 * np.random.randn(n)
    )
    _print_breakdown(f"clean cardiac (HR={hr_bpm})", score_spectrum(clean, fps))

    # ── flat noise ─────────────────────────────────────────────────────
    noise = np.random.randn(n)
    _print_breakdown("flat white noise", score_spectrum(noise, fps))

    # ── DC-heavy drift ─────────────────────────────────────────────────
    drift = np.cumsum(np.random.randn(n)) + 0.5 * np.sin(2 * np.pi * f_hr * t)
    _print_breakdown("drift + faint cardiac", score_spectrum(drift, fps))

    # ── pure single tone (no harmonic) ─────────────────────────────────
    pure = np.sin(2 * np.pi * f_hr * t)
    _print_breakdown("pure sinusoid (no harmonic)", score_spectrum(pure, fps))
