"""Fig. gym vs chamber: Polar H10 M'(100 ms) per run (censored, = miss_cens_100ms_pct).
Chamber: no-load runs (any channel set) and k = 0/2/4/8 at 75% load (forced_channel_duty75pct pooled as k = 0).
Gym: all 36 Polar runs by people present at run start (most recent hand count at or before start).
Style follows analyze_session.fig_paper_gym_chamber.
People counts: analyze_session's people_at_start (from background/people.csv), so only
analysis/ is needed, not the raw captures.

Usage: python3 scripts/paper_figures/fig_gym_chamber.py [REPO] OUT_DIR"""
import sys, os
import numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

W, OUT = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."), sys.argv[1])
A = os.path.join(W, "analysis")
C = "miss_cens_100ms_pct"
FLOOR = 0.05
IEEE_SINGLE_COL_IN = 21 * 12 / 72.27
F = "Liberation Serif"
RC = {"font.family": "serif", "font.serif": [F], "mathtext.fontset": "custom", "mathtext.rm": F,
      "mathtext.it": f"{F}:italic", "mathtext.bf": f"{F}:bold", "font.size": 8, "axes.labelsize": 8,
      "legend.fontsize": 8, "xtick.labelsize": 8, "ytick.labelsize": 8, "axes.linewidth": .5,
      "xtick.major.width": .5, "ytick.major.width": .5, "xtick.minor.width": .5, "ytick.minor.width": .5,
      "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
      "pdf.fonttype": 42, "ps.fonttype": 42}
KCOL = {0: "#08306B", 2: "#1478C8", 4: "#4A98D8", 8: "#7FB4E6"}
GYMCOL = "#eb6834"

def load(s):
    d = pd.read_csv(os.path.join(A, s, "tables", "per_run.csv"))
    d["exclude"] = d["exclude"].astype(str).str.lower().isin(["true", "1"])
    return d[~d.exclude & d.device.str.contains("olar")]

ch = pd.concat([load("20261007_105222_chamber_1"), load("20261008_010249_chamber_2")])
noload = ch[(ch.nominal_load_pct == 0) & ch.family.isin(["free", "free_bracket", "free_long", "constrained", "headroom"])]
hr = ch[ch.family.isin(["headroom", "constrained"]) & (ch.nominal_load_pct == 75)].copy()
hr["k"] = hr.headroom_k.astype(float)
hr.loc[hr.condition == "forced_channel_duty75pct", "k"] = 0.0
hr = hr[hr.k.notna()]

gyms = []
for s in ["gym_SPZ_20261013_133642", "gym_SPZ_20261013_160606", "gym_SPZ_20261016_103145"]:
    g = load(s)
    g = g.copy()
    g["people"] = g.people_at_start
    gyms.append(g)
gym = pd.concat(gyms)
assert len(gym) == 36 and gym.people.notna().all(), (len(gym), gym.people.isna().sum())

groups = [("No\nload", noload, "#555555", "chamber")]
groups += [(f"$k$={k:.0f}", g, KCOL[int(k)], "chamber") for k, g in hr.groupby("k")]
for name, lo, hi in [("Low", 0, 10), ("Med.", 10, 20), ("High", 20, 1e9)]:
    g = gym[(gym.people >= lo) & (gym.people < hi)]
    a, b = g.people.min(), g.people.max()
    groups.append((f"{name}\n{a:.0f}" + ("" if a == b else f"–{b:.0f}"), g, GYMCOL, "gym"))

rows = []
with plt.rc_context(RC):
    fig, ax = plt.subplots(figsize=(IEEE_SINGLE_COL_IN, 2.4))
    ax.axhspan(max(noload[C].min(), FLOOR), noload[C].max(), color="#e3e3e3", lw=0, zorder=0)
    for i, (lab, g, col, sec) in enumerate(groups):
        g = g.sort_values("start_time")
        v = g[C].clip(lower=FLOOR).values
        xs = i + np.linspace(-.22, .22, len(v)) if len(v) > 1 else np.array([float(i)])
        if sec == "gym":
            free = (g.family == "gym_free").values
            ax.scatter(xs[free], v[free], marker="o", s=9, color=col, lw=0, zorder=2)
            ax.scatter(xs[~free], v[~free], marker="s", s=9, facecolors="none", edgecolors=col, lw=.6, zorder=2)
        else:
            ax.scatter(xs, v, marker="o", s=9, color=col, lw=0, zorder=2)
        ax.plot([i - .3, i + .3], [np.median(v)] * 2, color="#222222", lw=1, zorder=3)
        rows += [dict(group=lab.replace("\n", " "), section=sec, session=r.session, condition=r.condition,
                      start_time=r.start_time, people=getattr(r, "people", np.nan), M_prime_100ms_pct=getattr(r, C))
                 for r in g.itertuples()]
    n_ch = sum(1 for g in groups if g[3] == "chamber")
    ax.axvline(n_ch - .5, color="#999999", lw=.5, zorder=0)
    ax.set_xticks(range(len(groups))); ax.set_xticklabels([g[0] for g in groups])
    ax.set_xlim(-.6, len(groups) - .4)
    ax.set_yscale("log"); ax.set_ylim(FLOOR * .7, 100)
    ax.set_yticks([.1, 1, 10, 100]); ax.set_yticklabels(["0.1", "1", "10", "100"])
    ax.set_ylabel("$M'$(100 ms) per run (%)")
    for x0, x1, txt in ((-.5, n_ch - .5, "Chamber, 75% load"), (n_ch - .5, len(groups) - .5, "Gym, persons")):
        ax.annotate(txt, xy=((x0 + x1) / 2, 0), xycoords=ax.get_xaxis_transform(), xytext=(0, -24),
                    textcoords="offset points", ha="center", va="top")
    ax.scatter([], [], marker="o", s=9, color=GYMCOL, lw=0, label="gym, AFH free")
    ax.scatter([], [], marker="s", s=9, facecolors="none", edgecolors=GYMCOL, lw=.6, label="gym, confined ($k$=0, 2, 8)")
    ax.fill_between([], [], color="#e3e3e3", label="chamber no-load range")
    ax.legend(loc="lower center", bbox_to_anchor=(.5, 1.0), ncol=2, columnspacing=.8, handletextpad=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "pooled_fig_gym_chamber.pdf"), bbox_inches="tight")
    fig.savefig(os.path.join(OUT, "pooled_fig_gym_chamber.png"), dpi=200, bbox_inches="tight")
pd.DataFrame(rows).to_csv(os.path.join(OUT, "pooled_fig_gym_chamber.csv"), index=False)
for lab, g, _, _ in groups:
    print(lab.replace("\n", " "), len(g), round(g[C].median(), 2), round(g[C].min(), 2), round(g[C].max(), 2))
