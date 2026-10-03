"""Tier assignment via 1-D Gaussian mixture over average expert rank.

Port of the mclust call in ff-functions.R: Mclust(avg_rank, G=k). mclust
picks equal- or varying-variance components by BIC; we do the same with
scikit-learn ("tied" vs "full"), then renumber clusters 1..n in rank order
and drop empty ones, as upstream did.
"""
from __future__ import annotations

import numpy as np
from sklearn.mixture import GaussianMixture


def assign_tiers(avg_ranks: list[float], k: int, seed: int = 0) -> list[int]:
    """Return a tier number (1 = best) for each player, in input order."""
    x = np.asarray(avg_ranks, dtype=float).reshape(-1, 1)
    k = max(1, min(k, len(np.unique(x))))
    if k == 1:
        return [1] * len(avg_ranks)

    best = None
    for cov in ("tied", "full"):
        gm = GaussianMixture(n_components=k, covariance_type=cov,
                             n_init=8, reg_covar=1e-3, random_state=seed).fit(x)
        bic = gm.bic(x)
        if best is None or bic < best[0]:
            best = (bic, gm)
    labels = best[1].predict(x)

    # Renumber components by their mean average-rank, best first, skipping
    # any component that ended up empty.
    means = {}
    for lab in np.unique(labels):
        means[lab] = x[labels == lab].mean()
    order = sorted(means, key=means.get)
    remap = {lab: i + 1 for i, lab in enumerate(order)}
    return [remap[lab] for lab in labels]
