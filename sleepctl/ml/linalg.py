"""Tiny pure-Python linear algebra for ridge regression (no numpy required).

Used for a small number of features, so an O(n_features^3) dense solve is fine.
"""

from __future__ import annotations

from typing import List


def solve_linear(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve A x = b via Gaussian elimination with partial pivoting.

    Returns a best-effort solution; near-singular pivots are skipped (treated as 0),
    which is acceptable here because the caller adds Tikhonov regularization.
    """
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            continue
        M[col], M[pivot] = M[pivot], M[col]
        pv = M[col][col]
        for j in range(col, n + 1):
            M[col][j] /= pv
        for r in range(n):
            if r != col and M[r][col] != 0.0:
                f = M[r][col]
                for j in range(col, n + 1):
                    M[r][j] -= f * M[col][j]
    return [M[i][n] for i in range(n)]


def ridge_fit(X: List[List[float]], y: List[float], lam: float) -> List[float]:
    """Solve (X^T X + lam I) w = X^T y for the coefficient vector w."""
    if not X:
        return []
    p = len(X[0])
    A = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for row, yi in zip(X, y):
        for i in range(p):
            b[i] += row[i] * yi
            ri = row[i]
            Ai = A[i]
            for j in range(p):
                Ai[j] += ri * row[j]
    for i in range(p):
        A[i][i] += lam
    return solve_linear(A, b)
