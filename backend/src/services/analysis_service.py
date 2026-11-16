"""Deterministic unmixing of a frozen input snapshot.

``reuse`` lets a replacement study copy per-replicate composition from the
archived original result for unaffected samples, while recomputing only the
samples whose measurement was revoked.  Stream spawning is index-based over
the same code-sorted sample order, so the recomputed sample keeps its original
root sub-stream and every reused sample stays bit-identical.
"""
import numpy as np

from .unmix_algorithm import matrix_diagnostics
from .noise_algorithm import conditional_refits
from ..utils.fingerprint import fingerprint


def analyze_unmix(snapshot, noise_sigma=.005, repeats=80, seed=2026, reuse=None):
    reuse = reuse or {}
    library = snapshot["library"]
    members = sorted(snapshot["endmembers"], key=lambda e: e["code"])
    samples = sorted(snapshot["samples"], key=lambda s: s["code"])
    if not 1 <= len(samples) <= 6 or len(samples) * repeats > 480:
        raise ValueError("选择1–6个样本，样本数×重复数不得超过480")
    if len({s["code"] for s in samples}) != len(samples):
        raise ValueError("混合样本编号重复")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("根种子须为0–2147483647整数")
    wavelengths = np.asarray(library["wavelengths"], dtype=float)
    if wavelengths.ndim != 1 or not np.isfinite(wavelengths).all() or np.any(np.diff(wavelengths) <= 0):
        raise ValueError("波长须有限且严格递增")
    A = np.array([e["spectrum"] for e in members], dtype=float).T
    _, diagnostics = matrix_diagnostics(A)
    if len(wavelengths) != A.shape[0]:
        raise ValueError("波长与端元波段数不匹配")
    if not diagnostics["supported"]:
        raise ValueError(diagnostics["reason"])
    streams = np.random.SeedSequence(seed).spawn(len(samples))
    results = []
    for sample, child in zip(samples, streams):
        archived = reuse.get(sample["id"])
        if archived is not None:
            if list(archived.get("observed", [])) != list(sample["observed"]):
                raise ValueError("复用样本的观测谱与归档结果不一致，拒绝拼接研究结果")
            results.append(_reused_result(sample, archived))
            continue
        refits = conditional_refits(A, sample["observed"], noise_sigma, repeats, child)
        results.append(
            {
                "sample_id": sample["id"],
                "code": sample["code"],
                "name": sample["name"],
                "observed": sample["observed"],
                "source_id": sample.get("source_id"),
                "source_code": sample.get("source_code"),
                "reused_from_run_id": None,
                "spawn_key": list(child.spawn_key),
                "high_residual": refits["point_fit"]["rmse"] > max(3 * noise_sigma, 1e-8),
                **refits,
            }
        )
    canonical = {"library": library, "endmembers": members, "samples": samples}
    hash_payload = {
        "method": "simplex-nullspace-kkt-gaussian-v2",
        "snapshot": canonical,
        "noise_sigma": noise_sigma,
        "repeats": repeats,
        "seed": seed,
    }
    if reuse:
        # Only replacement studies carry reuse provenance, so ordinary studies
        # keep the historical fingerprint basis unchanged.
        hash_payload["reuse_from"] = {
            str(k): v.get("reused_from_run_id") for k, v in reuse.items()
        }
    return {
        "algorithm_version": "simplex-nullspace-kkt-gaussian-v2",
        "library": library,
        "endmembers": members,
        "samples": results,
        "diagnostics": diagnostics,
        "noise_sigma": noise_sigma,
        "repeats": repeats,
        "seed": seed,
        "input_snapshot": canonical,
        "input_hash": fingerprint(hash_payload),
        "reuse_note": (
            "仅重算受撤销影响的样本；其余样本的逐次配比与残差按归档结果原样复用。"
            if reuse
            else ""
        ),
        "assumptions": "固定校准端元的线性混合；权重非负且和为1。噪声在各波段独立同方差Gaussian，sigma由用户给定；高残差只提示失配，不是噪声或成分真实性检验。",
        "quantile_note": "分位范围是条件扰动情景，不是配比真值的覆盖保证。",
        "feasibility_tolerance": 1e-10,
        "condition_limit": 1e6,
    }


def _reused_result(sample, archived):
    reused = dict(archived)
    reused["sample_id"] = sample["id"]
    reused["code"] = sample["code"]
    reused["name"] = sample["name"]
    reused["observed"] = sample["observed"]
    reused["source_id"] = sample.get("source_id")
    reused["source_code"] = sample.get("source_code")
    # ``reused_from_run_id`` is the run whose archived numbers these are; the
    # snapshot records the run the *study* is a replacement of, which is not
    # necessarily the same after multiple refresh rounds.
    reused["reused_from_run_id"] = archived.get("reused_from_run_id") or archived.get("run_id")
    return reused
