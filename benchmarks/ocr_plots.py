"""Render completed OCR observations without running recognition or timing work."""

import argparse
import json
import math
from pathlib import Path
import sys
import textwrap


CONDITIONS = ("original", "neutral_filename", "pure_neutral")
CONDITION_LABELS = ("Original filename", "Neutral filename; metadata retained", "Pure neutral; metadata stripped")
GROUP_LABELS = {
    "low_outliers": "Low outliers",
    "lower_whisker_to_Q1": "Lower whisker–Q1",
    "Q1_to_median": "Q1–median",
    "median_to_Q3": "Median–Q3",
    "Q3_to_upper_whisker": "Q3–upper whisker",
    "high_outliers": "High outliers",
}


def plot_inputs(data):
    """Validate counts and retain the labels belonging to nonempty groups."""
    distributions = data["distributions"]
    for condition in CONDITIONS:
        group = distributions[condition]
        values = group["values"]
        if not values or len(values) != group["pdfs"]:
            raise ValueError(f"{condition}: missing or inconsistent PDF observations")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError(f"{condition}: durations must be finite nonnegative seconds")
    neutral = distributions["neutral_filename"]
    groups = neutral["groups"]
    labels = []
    selected = []
    seen = set()
    for group in groups:
        key = group["group"]
        if key not in GROUP_LABELS or key in seen:
            raise ValueError("Unknown or duplicated boxplot group")
        seen.add(key)
        for field in ("pdfs", "base_ocr_executions", "ocr_rechecks", "total_ocr_executions"):
            value = group[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid {field}")
        if group["base_ocr_executions"] + group["ocr_rechecks"] != group["total_ocr_executions"]:
            raise ValueError("Base readings plus rechecks must equal all OCR readings")
        if group["pdfs"]:
            if group["mean_seconds"] is None or not math.isfinite(group["mean_seconds"]):
                raise ValueError("A nonempty group needs its observed mean")
            selected.append(group)
            labels.append(GROUP_LABELS[key])
    if sum(g["pdfs"] for g in groups) != neutral["pdfs"]:
        raise ValueError("Disjoint group counts do not reconcile to the PDF population")
    return selected, labels


def render(data, output_stem):
    groups, group_labels = plot_inputs(data)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
    except ImportError as exc:
        raise RuntimeError(
            f"Plotting requires Matplotlib in the selected Python interpreter ({sys.executable}). "
            "Use the project's existing analysis environment; recognition does not require plotting."
        ) from exc
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), gridspec_kw={"height_ratios": [1, 1.2]})
    fig.subplots_adjust(left=.34, right=.97, bottom=.17, top=.84, hspace=.62)
    try:
        boxes = axes[0].boxplot([data["distributions"][key]["values"] for key in CONDITIONS],
            orientation="horizontal", tick_labels=CONDITION_LABELS, patch_artist=True,
            showmeans=True, meanprops={"marker": "D", "markerfacecolor": "#C44E52", "markeredgecolor": "#C44E52"},
            medianprops={"color": "#172B3A", "linewidth": 1.8},
            flierprops={"marker": ".", "markersize": 4, "alpha": .6})
        for patch, color in zip(boxes["boxes"], ("#4C78A8", "#F28E2B", "#59A14F"), strict=True):
            patch.set_facecolor(color)
            patch.set_alpha(.55)
        axes[0].set_title("Latency by filename condition", loc="left", fontweight="bold")
        axes[0].set_xlabel("Observed seconds per PDF presentation")
        axes[0].plot([], [], "D", color="#C44E52", label="Mean")
        axes[0].plot([], [], color="#172B3A", label="Median")
        axes[0].legend(frameon=False, loc="lower right", ncol=2)
        base = [g["base_ocr_executions"] for g in groups]
        rechecks = [g["ocr_rechecks"] for g in groups]
        axes[1].barh(group_labels, base, color="#4C78A8", label="Base OCR readings")
        axes[1].barh(group_labels, rechecks, left=base, color="#F28E2B", label="Additional rechecks")
        maximum = max((g["total_ocr_executions"] for g in groups), default=0)
        axes[1].set_xlim(0, max(1, maximum * 1.85))
        for index, group in enumerate(groups):
            axes[1].text(group["total_ocr_executions"] + max(1, maximum) * .02, index,
                f"{group['pdfs']} PDFs; mean {group['mean_seconds']:.2f} s; {group['time_share']:.1%} of time",
                va="center", fontsize=8)
        axes[1].set_title("Neutral-filename OCR workload by disjoint latency group", loc="left", fontweight="bold")
        axes[1].set_xlabel("Actual Tesseract executions, summed within each group")
        axes[1].xaxis.set_major_locator(MaxNLocator(integer=True))
        axes[1].legend(frameon=False, loc="upper right", ncol=1, fontsize=8)
        for axis in axes:
            axis.invert_yaxis()
            axis.grid(axis="x", alpha=.22)
            axis.set_axisbelow(True)
            axis.spines[["top", "right"]].set_visible(False)
        count = data["distributions"]["neutral_filename"]["pdfs"]
        fig.suptitle(f"OCR workload and latency — {count} distinct PDF byte streams", y=.98, fontsize=14, fontweight="bold")
        fig.text(.03, .90, textwrap.fill("Source: " + data.get("source", "Source version unspecified"), 125), fontsize=9)
        footer = ("One observation per PDF and condition; original aliases averaged. Tukey 1.5-IQR whiskers. "
                  "Related works are not independent families. These completed observations do not establish "
                  "current-candidate speed qualification or a failure probability.")
        fig.text(.03, .035, textwrap.fill(footer, 145), fontsize=9, color="#475569")
        stem = Path(output_stem)
        stem.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("png", "svg"):
            fig.savefig(stem.with_suffix("." + suffix), dpi=170, bbox_inches="tight")
        return {"python": sys.executable, "matplotlib": matplotlib.__version__,
                "pdfs": count, "workload_groups": group_labels, "maximum_ocr_executions": maximum}
    finally:
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output-stem", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.observations.read_text(encoding="utf8"))
    print(json.dumps(render(data, args.output_stem)))


if __name__ == "__main__":
    main()
