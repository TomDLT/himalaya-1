import pytest

import numpy as np
import sklearn.linear_model
import sklearn.model_selection

from himalaya.backend import set_backend
from himalaya.backend import ALL_BACKENDS
from himalaya.utils import assert_array_almost_equal
from himalaya.scoring import r2_score

from himalaya.ridge import solve_group_ridge_random_search


def _create_dataset(backend):
    n_featuress = (10, 20)
    n_samples = 80
    n_targets = 4
    n_gammas = 3

    Xs = [
        backend.asarray(backend.randn(n_samples, n_features), backend.float64)
        for n_features in n_featuress
    ]

    ws = [
        backend.asarray(backend.randn(n_features, n_targets), backend.float64)
        for n_features in n_featuress
    ]
    Ys = backend.stack([X @ w for X, w in zip(Xs, ws)])
    Y = Ys.sum(0)
    Y += backend.asarray(backend.randn(*Y.shape), backend.float64)

    gammas = backend.asarray(backend.rand(n_gammas, len(Xs)), backend.float64)
    gammas /= gammas.sum(1)[:, None]

    return Xs, Y, gammas


@pytest.mark.parametrize('n_targets_batch', [None, 3])
@pytest.mark.parametrize('backend', ALL_BACKENDS)
def test_solve_group_ridge_random_search_n_targets_batch(
        backend, n_targets_batch):
    _test_solve_group_ridge_random_search(backend=backend,
                                           n_targets_batch=n_targets_batch)


@pytest.mark.parametrize('n_alphas_batch', [None, 2])
@pytest.mark.parametrize('backend', ALL_BACKENDS)
def test_solve_group_ridge_random_search_n_alphas_batch(
        backend, n_alphas_batch):
    _test_solve_group_ridge_random_search(backend=backend,
                                           n_alphas_batch=n_alphas_batch)


def _test_solve_group_ridge_random_search(backend, n_targets_batch=None,
                                           n_alphas_batch=None):
    backend = set_backend(backend)

    Xs, Y, gammas = _create_dataset(backend)
    alphas = backend.asarray_like(backend.logspace(-3, 5, 9), Xs[0])
    n_targets = Y.shape[1]
    cv = sklearn.model_selection.check_cv(10)

    ############
    # run solver
    results = solve_group_ridge_random_search(
        Xs, Y, n_iter=gammas, alphas=alphas, score_func=r2_score, cv=cv,
        n_targets_batch=n_targets_batch, progress_bar=False,
        return_weights=True, n_alphas_batch=n_alphas_batch,
        diagonalize_method="svd")
    best_deltas, refit_weights, cv_scores = results

    #########################################
    # compare with sklearn.linear_model.Ridge
    test_scores = []
    for gamma in gammas:
        X = backend.concatenate(
            [x * backend.sqrt(g) for x, g in zip(Xs, gamma)], 1)
        for train, test in cv.split(X):
            for alpha in alphas:
                model = sklearn.linear_model.Ridge(
                    alpha=backend.to_numpy(alpha), fit_intercept=False)
                model = model.fit(backend.to_numpy(X[train]),
                                  backend.to_numpy(Y[train]))
                predictions = backend.asarray_like(
                    model.predict(backend.to_numpy(X[test])), ref=Y)
                test_scores.append(r2_score(Y[test], predictions))

    test_scores = backend.stack(test_scores)
    test_scores = test_scores.reshape(len(gammas), cv.get_n_splits(),
                                      len(alphas), n_targets)
    test_scores_mean = backend.max(test_scores.mean(1), 1)
    assert_array_almost_equal(cv_scores, test_scores_mean, decimal=5)

    ######################
    # test refited_weights
    for tt in range(n_targets):
        gamma = backend.exp(best_deltas[:, tt])
        alpha = 1.0

        # compare primal weights with sklearn.linear_model.Ridge
        X = backend.concatenate(
            [X * backend.sqrt(g) for X, g in zip(Xs, gamma)], 1)
        model = sklearn.linear_model.Ridge(fit_intercept=False,
                                           alpha=backend.to_numpy(alpha))
        w1 = model.fit(backend.to_numpy(X), backend.to_numpy(Y[:, tt])).coef_
        w1 = np.split(w1, np.cumsum([X.shape[1] for X in Xs][:-1]), axis=0)
        w1 = [backend.asarray(w) for w in w1]
        w1_scaled = backend.concatenate(
            [w * backend.sqrt(g) for w, g, in zip(w1, gamma)])
        assert_array_almost_equal(w1_scaled, refit_weights[:, tt], decimal=5)
