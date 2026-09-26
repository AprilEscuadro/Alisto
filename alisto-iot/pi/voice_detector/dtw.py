"""Dynamic Time Warping distance between two MFCC sequences.

Uses a Sakoe-Chiba band so the DP only fills a diagonal strip instead of the
full n*m grid -- this is what keeps matching fast enough to run in real time
on a Pi 3B+.
"""

import numpy as np

import config


def dtw_distance(a, b, radius):
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float("inf")

    # Widen the band when sequence lengths differ, otherwise the (n, m) corner
    # can fall outside the band and become unreachable, forcing a spurious inf
    # even for a genuine match spoken at a different pace.
    radius = max(radius, abs(n - m))

    # Precompute the full pairwise cost matrix with one vectorized numpy call
    # instead of n*m individual np.linalg.norm() calls inside the DP loop --
    # the per-call Python/numpy overhead of the latter dominates on a Pi's
    # weak CPU once there are many templates to check against.
    diff = a[:, None, :] - b[None, :, :]
    cost_matrix = np.sqrt(np.sum(diff * diff, axis=2))  # shape (n, m)

    inf = float("inf")
    d = np.full((n + 1, m + 1), inf)
    d[0, 0] = 0.0

    for i in range(1, n + 1):
        j_start = max(1, i - radius)
        j_end = min(m, i + radius)
        row_prev = d[i - 1]
        row = d[i]
        cost_row = cost_matrix[i - 1]
        for j in range(j_start, j_end + 1):
            row[j] = cost_row[j - 1] + min(row_prev[j], row[j - 1], row_prev[j - 1])

    return d[n, m] / (n + m)  # normalize so length differences don't dominate


def best_match(query_mfcc, templates):
    """templates: dict[name -> mfcc array]. Returns (best_name, best_distance).

    Kept for simple single-pool use (e.g. leave-one-out calibration). For
    actual detection, prefer group_match() below -- matching against a
    single nearest template lets any one unusually "generic" template act as
    a false attractor for unrelated words, which happened repeatedly in
    testing.
    """
    best_name, best_dist = None, float("inf")
    for name, template in templates.items():
        dist = dtw_distance(query_mfcc, template, radius=config.DTW_BAND_RADIUS)
        if dist < best_dist:
            best_name, best_dist = name, dist
    return best_name, best_dist


def group_match(query_mfcc, groups, k=3):
    """groups: dict[group_name -> dict[sample_name -> mfcc]].

    For each group, scores it by the AVERAGE distance to its K closest
    templates (not just the single nearest one). This is far more robust
    than nearest-neighbor matching: a single unusually "generic" template
    can make single-nearest-neighbor matching falsely attract completely
    unrelated words, since only one lucky template needs to be close. With
    k>=2, an unrelated word would need to accidentally resemble MULTIPLE
    templates in a group to score well, which is much less likely.

    Returns (best_group, best_avg_distance, {group: avg_distance}).
    """
    distances_by_group = {}
    for group, templates in groups.items():
        dists = sorted(
            dtw_distance(query_mfcc, t, radius=config.DTW_BAND_RADIUS)
            for t in templates.values()
        )
        k_use = min(k, len(dists))
        distances_by_group[group] = sum(dists[:k_use]) / k_use

    best_group = min(distances_by_group, key=distances_by_group.get)
    return best_group, distances_by_group[best_group], distances_by_group
