"""Tier chart rendering (port of error.bar.plot's ggplot chart).

Same visual grammar as borischen.co: one row per player ordered by expert
consensus rank, a horizontal bar showing avg rank +/- half a standard
deviation, colored by tier, with the player label sitting left of the bar.
"""
from __future__ import annotations

import colorsys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .fetch import PlayerRow


def tier_colors(k: int) -> list:
    """Mimic ggplot's scale_colour_hue(l=55) with the hue range widened as
    tier count grows (upstream bumped highcolor at k>11/13/15)."""
    highcolor = 360
    if k > 11:
        highcolor = 450
    if k > 13:
        highcolor = 550
    if k > 15:
        highcolor = 650
    hues = [(i * highcolor / k) % 360 for i in range(k)]
    return [colorsys.hls_to_rgb(h / 360.0, 0.55, 0.85) for h in hues]


def draw_chart(rows: list[PlayerRow], tiers: list[int], title: str, out_png: Path) -> None:
    n = len(rows)
    k = max(tiers)
    colors = tier_colors(k)
    font = 8.0 if n <= 30 else (7.0 if n <= 45 else 6.0)

    fig, ax = plt.subplots(figsize=(9.5, max(8.0, n * 0.16)), dpi=150)
    seen: set[int] = set()
    for row, tier in zip(rows, tiers):
        c = colors[tier - 1]
        label = f"Tier {tier}" if tier not in seen else None
        seen.add(tier)
        y = -row.rank
        ax.errorbar(row.avg, y, xerr=row.std / 2, fmt="none",
                    ecolor=c, elinewidth=2.2, capsize=2, alpha=0.55, label=label)
        ax.plot(row.avg, y, "o", color="0.2", markersize=2.5, zorder=3)
        ax.text(row.avg - row.std / 2 - 0.4, y, row.name,
                ha="right", va="center", fontsize=font, color=c)

    ax.set_title(title, fontsize=11)
    ax.set_xlabel("Average Expert Rank")
    ax.set_ylabel("Expert Consensus Rank")
    ax.set_yticks([])
    lows = min(r.avg - r.std / 2 for r in rows)
    ax.set_xlim(lows - max(8, lows * 0.35), max(r.avg + r.std / 2 for r in rows) + 2)
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
