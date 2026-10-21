#!/usr/bin/env python3
"""Paper Fig. (confinement schematic): BLE data channels used in each chamber channel set.

Drawn at the width main.tex prints it at (\\columnwidth; IEEE: not narrower than a column), in the IEEE style of
analyze_session.py (IEEE_RC), so text prints at 8 pt.

Usage: python3 paper_figures/fig_confinement.py [out_dir]   (default: current directory)
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon, Rectangle  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from analyze_session import IEEE_RC, IEEE_SINGLE_COL_IN  # noqa: E402

SIZE_IN = (IEEE_SINGLE_COL_IN, 1.8)
BUSY, CLEAN, BAND = "#08306B", "#7FB4E6", "#E5F0FC"   # dark / light / fill: distinct in grayscale
NOTE = "#555555"


def freq(n):
    """Centre frequency (MHz) of BLE data channel n."""
    return 2404 + 2 * n if n <= 10 else 2428 + 2 * (n - 11)


busy = set(range(12, 20))   # inside Wi-Fi channel 6
sets = [("AFH free", None, "0–15%"),
        ("Confined", busy, "100%"),
        ("+ $k$ = 2", busy | {2, 32}, "80%"),
        ("+ $k$ = 4", busy | {0, 4, 30, 34}, "67%"),
        ("+ $k$ = 8", busy | {0, 2, 4, 28, 30, 32, 34, 36}, "47%")]

with plt.rc_context(IEEE_RC):
    fig, ax = plt.subplots(figsize=SIZE_IN)
    y0 = len(sets)
    # Wi-Fi channel 6: 16.25 MHz occupied
    ax.add_patch(Rectangle((2437 - 8.125, -0.6), 16.25, y0 + 0.05, color=BAND, lw=0, zorder=0))
    ax.add_patch(Polygon([[2427, y0 - 0.25], [2429, y0 + 0.45], [2445, y0 + 0.45], [2447, y0 - 0.25]],
                         closed=True, fc=BAND, ec=BUSY, lw=0.6))
    ax.text(2437, y0 + 0.65, "Wi-Fi ch. 6, iperf3 UDP, 0–100% load", ha="center", va="bottom", color=BUSY)
    w = 1.5
    for i, (lab, used, note) in enumerate(sets):
        y = y0 - 1 - i
        for n in range(37):
            f = freq(n)
            if used is None:
                inb = n in busy
                ax.add_patch(Rectangle((f - w / 2, y - 0.3), w, 0.6, fc="white" if inb else CLEAN,
                                       ec=BUSY if inb else CLEAN, lw=0.4, hatch="////" if inb else None))
            else:
                if n in used:
                    fc = ec = BUSY if n in busy else CLEAN
                else:
                    fc, ec = "white", "#BDBDBD"
                ax.add_patch(Rectangle((f - w / 2, y - 0.3), w, 0.6, fc=fc, ec=ec, lw=0.4))
        ax.text(2400.5, y, lab, ha="right", va="center")
        ax.text(2481.5, y, note, ha="left", va="center", color=NOTE)
    ax.text(2481.5, y0 - 0.35, "Busy\nshare", ha="left", va="bottom", color=NOTE)
    ax.set_xlim(2401, 2481)
    ax.set_ylim(-0.7, y0 + 1.25)
    ax.set_yticks([])
    ax.set_xticks([2402, 2420, 2440, 2460, 2480])
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Frequency (MHz)", labelpad=1)
    h = [Rectangle((0, 0), 1, 1, fc=BUSY, ec=BUSY), Rectangle((0, 0), 1, 1, fc=CLEAN, ec=CLEAN),
         Rectangle((0, 0), 1, 1, fc="white", ec="#BDBDBD"), Rectangle((0, 0), 1, 1, fc="white", ec=BUSY, hatch="////")]
    ax.legend(h, ["used, in band", "used, clean", "not used", "removed under load"],
              loc="upper center", bbox_to_anchor=(0.45, -0.36), ncol=2, handlelength=1.2, columnspacing=1.0)
    fig.subplots_adjust(left=0.17, right=0.87, top=0.9, bottom=0.4)
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    fig.savefig(os.path.join(out, "fig_confinement.pdf"))
    fig.savefig(os.path.join(out, "fig_confinement.png"), dpi=300)
