# coding: utf-8
"""Renders the housing "found this week/month" dashboard as a PNG."""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

AREA_BINS = (0, 30, 40, 50, 60, 70, 80, 100, 120, 150, float("inf"))
PRICE_BINS = (0, 500, 700, 900, 1100, 1300, 1500, 1800, 2200, float("inf"))
ROOM_BINS = (0.5, 1.5, 2.5, 3.5, 4.5, float("inf"))
ROOM_LABELS = ("1", "2", "3", "4", "5+")

_BAR_COLORS = ("#4C72B0", "#55A868", "#C44E52")


def _bucket_counts(values, bins):
    counts = [0] * (len(bins) - 1)
    for value in values:
        if value is None:
            continue
        for i in range(len(bins) - 1):
            if bins[i] <= value < bins[i + 1]:
                counts[i] += 1
                break
    return counts


def _range_labels(bins):
    labels = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        labels.append(f"{lo:g}+" if hi == float("inf") else f"{lo:g}-{hi:g}")
    return labels


def render_dashboard(rows, title, axis_labels):
    """rows: iterable of (rooms, area_m2, price_eur) tuples, any field may be
    None. axis_labels: {"area": ..., "price": ..., "rooms": ...} subplot
    titles, already translated by the caller. Returns a PNG BytesIO buffer."""
    rooms_vals = [row[0] for row in rows]
    area_vals = [row[1] for row in rows]
    price_vals = [row[2] for row in rows]

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    fig.suptitle(title, fontsize=14)

    panels = (
        (axes[0], _bucket_counts(area_vals, AREA_BINS), _range_labels(AREA_BINS), axis_labels["area"]),
        (axes[1], _bucket_counts(price_vals, PRICE_BINS), _range_labels(PRICE_BINS), axis_labels["price"]),
        (axes[2], _bucket_counts(rooms_vals, ROOM_BINS), list(ROOM_LABELS), axis_labels["rooms"]),
    )
    for i, (ax, counts, labels, subtitle) in enumerate(panels):
        ax.bar(labels, counts, color=_BAR_COLORS[i])
        ax.set_title(subtitle)
        ax.tick_params(axis="x", rotation=45)

    fig.tight_layout(rect=(0, 0, 1, 0.93))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf


_ALL_COLOR = "#C9CED6"
_MINE_COLOR = "#2E7D32"


def render_comparison(all_rows, mine_rows, title, axis_labels, legend):
    """The weekly digest chart: for area, price and rooms, a grey bar for
    every new flat and a green one beside it for the flats that matched the
    person's filters - so it's visible at a glance where the filter cuts.

    rows: (rooms, area_m2, price_eur) tuples as in `render_dashboard`.
    legend: {"all": ..., "mine": ...}, already translated."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    fig.suptitle(title, fontsize=14)
    panels = (
        (axes[0], 1, AREA_BINS, _range_labels(AREA_BINS), axis_labels["area"]),
        (axes[1], 2, PRICE_BINS, _range_labels(PRICE_BINS), axis_labels["price"]),
        (axes[2], 0, ROOM_BINS, list(ROOM_LABELS), axis_labels["rooms"]),
    )
    width = 0.42
    for ax, index, bins, labels, subtitle in panels:
        every = _bucket_counts([row[index] for row in all_rows], bins)
        mine = _bucket_counts([row[index] for row in mine_rows], bins)
        positions = range(len(labels))
        ax.bar([p - width / 2 for p in positions], every, width, color=_ALL_COLOR, label=legend["all"])
        bars = ax.bar([p + width / 2 for p in positions], mine, width, color=_MINE_COLOR, label=legend["mine"])
        for bar, count in zip(bars, mine):
            if count:
                ax.annotate(str(count), (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            ha="center", va="bottom", fontsize=8, color=_MINE_COLOR)
        ax.set_xticks(list(positions))
        ax.set_xticklabels(labels, rotation=45)
        ax.set_title(subtitle)
    axes[0].legend(loc="upper right", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf
