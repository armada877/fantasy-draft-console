#!/usr/bin/env python3
"""Empirical-Bayes shrinkage toward the league mean.

Why this module exists: sample size varies ~8x across this account. the 12-team league has eight
usable seasons, chi-phi-american four (two of them FAAB), inlaws-outlaws exactly one —
and inside a single league, a manager who joined last year has one season next to
someone with eight. Reporting both as flat per-manager means would make the newcomer's
noise look like a personality. The draft console already learned this lesson the other
way round: CLAUDE.md records that giving a history-less manager a flat `1.0` modelled
him as paying full projected value at every position and inflated predicted competition.

So every per-manager number is a posterior mean:

    shrunk_i = w_i * own_i + (1 - w_i) * mu          w_i = n_i / (n_i + sigma^2/tau^2)

`tau^2` (real between-manager spread) and `sigma^2` (within-manager noise) are estimated
from the data by method of moments, so the shrinkage strength is learned, not chosen. If
the managers turn out not to differ beyond noise (`tau^2 <= 0`), every weight collapses
to 0 and everybody gets the league mean — which is the honest answer, not a failure.

    eb_normal(series)           per-season (or per-event) observations -> means
    eb_binomial(successes, trials)   rates -> proportions
    eb_vector(series)           the same, elementwise, for spend_pace

Each returns (values, weights, params). `1 - weight` is the `shrink` the artifact
reports: 0 = entirely this manager's own data, 1 = entirely the league mean.
"""
from __future__ import annotations

import statistics

__all__ = ["eb_normal", "eb_binomial", "eb_vector", "Fit"]


class Fit(dict):
    """The estimated prior, carried alongside so the output can be audited."""


def _empty(keys, mu=0.0):
    return {k: mu for k in keys}, {k: 0.0 for k in keys}, Fit(mu=mu, tau2=0.0,
                                                             sigma2=0.0, n_groups=0)


def eb_normal(series: dict, prior_mean=None):
    """series: {key: [obs, ...]} -> ({key: shrunk}, {key: weight}, Fit).

    sigma^2 is the POOLED within-group variance (a manager's own season-to-season
    scatter); tau^2 is what is left of the between-group spread once that noise is
    subtracted. Groups with a single observation contribute to tau^2 but not to sigma^2,
    which is exactly right — they carry signal about spread, none about noise.
    """
    keys = [k for k, v in series.items() if v]
    if not keys:
        return _empty(series.keys(), prior_mean or 0.0)
    means = {k: statistics.fmean(series[k]) for k in keys}
    ns = {k: len(series[k]) for k in keys}

    ss, df = 0.0, 0
    for k in keys:
        if ns[k] >= 2:
            m = means[k]
            ss += sum((x - m) ** 2 for x in series[k])
            df += ns[k] - 1
    if df > 0:
        sigma2 = ss / df
    else:                       # nobody has repeat observations -> all spread is noise
        allobs = [x for k in keys for x in series[k]]
        sigma2 = statistics.pvariance(allobs) if len(allobs) > 1 else 0.0

    mu = prior_mean if prior_mean is not None else statistics.fmean(means.values())
    k_n = len(keys)
    if k_n < 2:
        tau2 = 0.0
    else:
        between = sum((means[k] - mu) ** 2 for k in keys) / (k_n - 1)
        noise = sum(sigma2 / ns[k] for k in keys) / k_n
        tau2 = max(0.0, between - noise)

    out, w = {}, {}
    for k in series:
        if k not in keys:
            out[k], w[k] = mu, 0.0
            continue
        wi = 0.0 if tau2 <= 0 else (ns[k] * tau2) / (ns[k] * tau2 + sigma2) if sigma2 > 0 else 1.0
        out[k] = wi * means[k] + (1 - wi) * mu
        w[k] = wi
    return out, w, Fit(mu=mu, tau2=tau2, sigma2=sigma2, n_groups=k_n)


def eb_binomial(successes: dict, trials: dict, prior_mean=None):
    """Beta-binomial shrinkage of rates. Sampling noise is mu(1-mu)/n, so a manager with
    4 contested claims is pulled almost all the way to the league rate and one with 90
    barely moves."""
    keys = [k for k in trials if trials.get(k)]
    if not keys:
        return _empty(trials.keys(), prior_mean or 0.0)
    tot_s = sum(successes.get(k, 0) for k in keys)
    tot_n = sum(trials[k] for k in keys)
    mu = prior_mean if prior_mean is not None else (tot_s / tot_n if tot_n else 0.0)
    var_unit = mu * (1 - mu)
    ps = {k: successes.get(k, 0) / trials[k] for k in keys}
    k_n = len(keys)
    if k_n < 2 or var_unit <= 0:
        tau2 = 0.0
    else:
        between = sum((ps[k] - mu) ** 2 for k in keys) / (k_n - 1)
        noise = sum(var_unit / trials[k] for k in keys) / k_n
        tau2 = max(0.0, between - noise)
    out, w = {}, {}
    for k in trials:
        if k not in keys:
            out[k], w[k] = mu, 0.0
            continue
        n = trials[k]
        wi = 0.0 if tau2 <= 0 else (n * tau2) / (n * tau2 + var_unit) if var_unit > 0 else 1.0
        out[k] = wi * ps[k] + (1 - wi) * mu
        w[k] = wi
    return out, w, Fit(mu=mu, tau2=tau2, sigma2=var_unit, n_groups=k_n)


def eb_vector(series: dict, length: int):
    """{key: [[wk1, wk2, ...] per season]} -> ({key: [shrunk...]}, {key: weight}, [Fit]).

    Each week is shrunk on its own — early-season weeks have far more spread between
    managers than week 15 does — and the reported weight is the average across weeks.
    """
    keys = list(series)
    cols, ws, fits = [], [], []
    for i in range(length):
        col = {k: [row[i] for row in series[k] if i < len(row)] for k in keys}
        v, w, f = eb_normal(col)
        cols.append(v)
        ws.append(w)
        fits.append(f)
    out = {k: [cols[i].get(k, 0.0) for i in range(length)] for k in keys}
    wavg = {k: (statistics.fmean([ws[i].get(k, 0.0) for i in range(length)])
                if length else 0.0) for k in keys}
    return out, wavg, fits
