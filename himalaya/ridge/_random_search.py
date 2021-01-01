import warnings
import numbers

import numpy as np
from sklearn.model_selection import check_cv

from ..backend import get_backend
from ..progress_bar import bar
from ..scoring import l2_neg_loss
from ..validation import check_random_state
from ..kernel_ridge import generate_dirichlet_samples
from ..kernel_ridge._random_search import _select_best_alphas


def solve_banded_ridge_random_search(
        Xs, Y, n_iter=100, concentration=[0.1, 1.0], alphas=1.0,
        score_func=l2_neg_loss, cv=5, return_weights=False, local_alpha=True,
        jitter_alphas=False, random_state=None, n_targets_batch=None,
        n_targets_batch_refit=None, n_alphas_batch=None, progress_bar=True,
        conservative=False, Y_in_cpu=False, diagonalize_method="svd"):
    """Solve banded ridge regression using random search on the simplex.

    Parameters
    ----------
    Xs : list of len (n_spaces), with arrays of shape (n_samples, n_features)
        Input features.
    Y : array of shape (n_samples, n_targets)
        Target data.
    n_iter : int, or array of shape (n_iter, n_spaces)
        Number of feature-space weights combination to search.
        If an array is given, the solver uses it as the list of weights
        to try, instead of sampling from a Dirichlet distribution.
    concentration : float, or list of float
        Concentration parameters of the Dirichlet distribution.
        If a list, iteratively cycle through the list.
        Not used if n_iter is an array.
    alphas : float or array of shape (n_alphas, )
        Range of ridge regularization parameter.
    score_func : callable
        Function used to compute the score of predictions versus Y.
    cv : int or scikit-learn splitter
        Cross-validation splitter. If an int, KFold is used.
    return_weights : bool
        Whether to refit on the entire dataset and return the weights.
    local_alpha : bool
        If True, alphas are selected per target, else shared over all targets.
    jitter_alphas : bool
        If True, alphas range is slightly jittered for each gamma.
    random_state : int, or None
        Random generator seed. Use an int for deterministic search.
    n_targets_batch : int or None
        Size of the batch for over targets during cross-validation.
        Used for memory reasons. If None, uses all n_targets at once.
    n_targets_batch_refit : int or None
        Size of the batch for over targets during refit.
        Used for memory reasons. If None, uses all n_targets at once.
    n_alphas_batch : int or None
        Size of the batch for over alphas. Used for memory reasons.
        If None, uses all n_alphas at once.
    progress_bar : bool
        If True, display a progress bar over gammas.
    conservative : bool
        If True, when selecting the hyperparameter alpha, take the largest one
        that is less than one standard deviation away from the best.
        If False, take the best.
    Y_in_cpu : bool
        If True, keep the target values ``Y`` in CPU memory (slower).
    diagonalize_method : str in {"svd"}
        Method used to diagonalize the features.

    Returns
    -------
    deltas : array of shape (n_spaces, n_targets)
        Best log feature-space weights for each target.
    refit_weights : array of shape (n_features, n_targets), or None
        Refit regression weights on the entire dataset, using selected best
        hyperparameters. Refit weights are always stored on CPU memory.
    cv_scores : array of shape (n_iter, n_targets)
        Cross-validation scores per iteration, averaged over splits, for the
        best alpha. Cross-validation scores will always be on CPU memory.
    """
    backend = get_backend()
    n_spaces = len(Xs)
    if isinstance(n_iter, int):
        gammas = generate_dirichlet_samples(n_samples=n_iter,
                                            n_kernels=n_spaces,
                                            concentration=concentration,
                                            random_state=random_state)
    elif n_iter.ndim == 2:
        gammas = n_iter
        assert gammas.shape[1] == n_spaces
    else:
        raise ValueError("Unknown parameter n_iter=%r." % (n_iter, ))

    if isinstance(alphas, numbers.Number) or alphas.ndim == 0:
        alphas = backend.ones_like(Y, shape=(1, )) * alphas

    dtype = Xs[0].dtype
    gammas = backend.asarray(gammas, dtype=dtype)
    device = getattr(gammas, "device", None)
    gammas, alphas = backend.check_arrays(gammas, alphas)
    Y = backend.asarray(Y, dtype=dtype, device="cpu" if Y_in_cpu else device)
    Xs = [backend.asarray(X, dtype=dtype, device=device) for X in Xs]

    # stack all features
    X_ = backend.concatenate(Xs, 1)
    n_features_list = [X.shape[1] for X in Xs]
    n_features = X_.shape[1]
    start_and_end = np.concatenate([[0], np.cumsum(n_features_list)])
    slices = [
        slice(start, end)
        for start, end in zip(start_and_end[:-1], start_and_end[1:])
    ]
    del Xs

    n_samples, n_targets = Y.shape
    if n_targets_batch is None:
        n_targets_batch = n_targets
    if n_targets_batch_refit is None:
        n_targets_batch_refit = n_targets_batch
    if n_alphas_batch is None:
        n_alphas_batch = len(alphas)

    cv = check_cv(cv)
    n_splits = cv.get_n_splits()
    for train, val in cv.split(Y):
        if len(val) == 0 or len(train) == 0:
            raise ValueError("Empty train or validation set. "
                             "Check that `cv` is correctly defined.")

    random_generator, given_alphas = None, None
    if jitter_alphas:
        random_generator = check_random_state(random_state)
        given_alphas = backend.copy(alphas)

    best_gammas = backend.full_like(gammas, fill_value=1.0 / n_spaces,
                                    shape=(n_spaces, n_targets))
    best_alphas = backend.ones_like(gammas, shape=n_targets)
    cv_scores = backend.zeros_like(gammas, shape=(len(gammas), n_targets),
                                   device="cpu")
    current_best_scores = backend.full_like(gammas, fill_value=-backend.inf,
                                            shape=n_targets)

    # initialize refit ridge weights
    refit_weights = None
    if return_weights:
        refit_weights = backend.zeros_like(gammas,
                                           shape=(n_features, n_targets),
                                           device="cpu")

    for ii, gamma in enumerate(
            bar(gammas, '%d random sampling with cv' % len(gammas),
                use_it=progress_bar)):

        for kk in range(n_spaces):
            X_[:, slices[kk]] *= backend.sqrt(gamma[kk])

        if jitter_alphas:
            noise = backend.asarray_like(random_generator.rand(), alphas)
            alphas = given_alphas * (10 ** (noise - 0.5))

        scores = backend.zeros_like(gammas,
                                    shape=(n_splits, len(alphas), n_targets))
        for jj, (train, test) in enumerate(cv.split(X_)):
            train = backend.to_gpu(train, device=device)
            test = backend.to_gpu(test, device=device)

            for matrix, alpha_batch in _decompose_ridge(
                    Xtrain=X_[train], alphas=alphas,
                    negative_eigenvalues="nan", n_alphas_batch=n_alphas_batch,
                    method=diagonalize_method):
                # n_alphas_batch, n_features, n_samples_train = \
                # matrix.shape
                matrix = backend.matmul(X_[test], matrix)
                # n_alphas_batch, n_samples_test, n_samples_train = \
                # matrix.shape

                predictions = None
                for start in range(0, n_targets, n_targets_batch):
                    batch = slice(start, start + n_targets_batch)
                    Y_batch = backend.to_gpu(Y[:, batch], device=device)

                    predictions = backend.matmul(matrix, Y_batch[train])
                    # n_alphas_batch, n_samples_test, n_targets_batch = \
                    # predictions.shape

                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=UserWarning)
                        scores[jj, alpha_batch, batch] = score_func(
                            Y_batch[test], predictions)
                        # n_alphas_batch, n_targets_batch = score.shape

                # make small alphas impossible to select
                too_small_alphas = backend.isnan(matrix[:, 0, 0])
                scores[jj, alpha_batch, :][too_small_alphas] = -1e5

                del matrix, predictions
            del train, test

        # select best alphas
        alphas_argmax, cv_scores_ii = _select_best_alphas(
            scores, alphas, local_alpha, conservative)
        cv_scores[ii, :] = backend.to_cpu(cv_scores_ii)

        # update best_gammas and best_alphas
        mask = cv_scores_ii > current_best_scores
        current_best_scores[mask] = cv_scores_ii[mask]
        best_gammas[:, mask] = gamma[:, None]
        best_alphas[mask] = alphas[alphas_argmax[mask]]

        # compute primal or dual weights on the entire dataset (nocv)
        if return_weights:
            update_indices = backend.flatnonzero(mask)
            if Y_in_cpu:
                update_indices = backend.to_cpu(update_indices)
            if len(update_indices) > 0:

                # refit weights only for alphas used by at least one target
                used_alphas = backend.unique(best_alphas[mask])
                primal_weights = backend.zeros_like(
                    X_, shape=(n_features, len(update_indices)), device="cpu")
                for matrix, alpha_batch in _decompose_ridge(
                        Xtrain=X_, alphas=used_alphas,
                        negative_eigenvalues="zeros",
                        n_alphas_batch=min(len(used_alphas), n_alphas_batch),
                        method=diagonalize_method):

                    for start in range(0, len(update_indices),
                                       n_targets_batch_refit):
                        batch = slice(start, start + n_targets_batch_refit)

                        weights = backend.matmul(
                            matrix,
                            backend.to_gpu(Y[:, update_indices[batch]],
                                           device=device))
                        # used_n_alphas_batch, n_features, n_targets_batch = \
                        # weights.shape

                        # select alphas corresponding to best cv_score
                        alphas_indices = backend.searchsorted(
                            used_alphas, best_alphas[mask][batch])
                        # mask targets whose selected alphas are outside the
                        # alpha batch
                        mask2 = backend.isin(
                            alphas_indices,
                            backend.arange(len(used_alphas))[alpha_batch])
                        # get indices in alpha_batch
                        alphas_indices = backend.searchsorted(
                            backend.arange(len(used_alphas))[alpha_batch],
                            alphas_indices[mask2])
                        # update corresponding weights
                        tmp = weights[alphas_indices, :,
                                      backend.arange(weights.shape[2])[mask2]]
                        primal_weights[:, batch][:, backend.to_cpu(mask2)] = \
                            backend.to_cpu(tmp).T
                        del weights, alphas_indices, mask2
                    del matrix

                # multiply again by np.sqrt(g), as we then want to use
                # the primal weights on the unscaled features Xs, and not
                # on the scaled features (np.sqrt(g) * Xs)
                for kk in range(n_spaces):
                    primal_weights[slices[kk]] *= backend.to_cpu(
                        backend.sqrt(gamma[kk]))
                refit_weights[:, backend.to_cpu(mask)] = primal_weights
                del primal_weights

            del update_indices
        del mask

        for kk in range(n_spaces):
            X_[:, slices[kk]] /= backend.sqrt(gamma[kk])

    deltas = backend.log(best_gammas / best_alphas[None, :])
    return deltas, refit_weights, cv_scores


def _decompose_ridge(Xtrain, alphas, n_alphas_batch=None, method="svd",
                     negative_eigenvalues="zeros"):
    """Precompute resolution matrices for ridge predictions.

    To compute the prediction::

        Ytest_hat = Xtest @ (XTX + alphas * Id)^-1 @ Xtrain^T @ Ytrain

        where XTX = Xtrain^T @ Xtrain,

    this function precomputes::

        matrices = (XTX + alphas * Id)^-1 @ Xtrain^T.

    Parameters
    ----------
    Xtrain : array of shape (n_samples_train, n_features)
        Concatenated input features.
    alphas : float, or array of shape (n_alphas, )
        Range of ridge regularization parameter.
    n_alphas_batch : int or None
        If not None, returns a generator over batches of alphas.
    method : str in {"svd"}
        Method used to diagonalize the kernel.
    negative_eigenvalues : str in {"nan", "error", "zeros"}
        If the decomposition leads to negative eigenvalues (wrongly emerging
        from float32 errors):
            - "error" raises an error.
            - "zeros" remplaces them with zeros.
            - "nan" returns nans if the regularization does not compensate
                twice the smallest negative value, else it ignores the problem.

    Returns
    -------
    matrices : array of shape (n_alphas, n_samples_test, n_samples_train) or \
        (n_alphas, n_features, n_samples_train) if test is not None
        Precomputed resolution matrices.
    alpha_batch : slice
        Slice of the batch of alphas.
    """
    backend = get_backend()

    use_alpha_batch = n_alphas_batch is not None
    if n_alphas_batch is None:
        n_alphas_batch = len(alphas)

    if method == "svd":
        # SVD: X = U @ np.diag(eigenvalues) @ Vt
        U, eigenvalues, Vt = backend.svd(Xtrain, full_matrices=False)
    else:
        raise ValueError("Unknown method=%r." % (method, ))

    for start in range(0, len(alphas), n_alphas_batch):
        batch = slice(start, start + n_alphas_batch)

        ev_weighting = eigenvalues / (alphas[batch, None] + eigenvalues ** 2)

        # negative eigenvalues can emerge from incorrect kernels,
        # or from float32
        if eigenvalues[0] < 0:
            if negative_eigenvalues == "nan":
                ev_weighting[alphas[batch] < -eigenvalues[0] * 2, :] = \
                    backend.asarray(backend.nan, type=ev_weighting.dtype)

            elif negative_eigenvalues == "zeros":
                eigenvalues[eigenvalues < 0] = 0

            elif negative_eigenvalues == "error":
                raise RuntimeError(
                    "Negative eigenvalues. Make sure the kernel is positive "
                    "semi-definite, increase the regularization alpha, or use"
                    "another solver.")
            else:
                raise ValueError("Unknown negative_eigenvalues=%r." %
                                 (negative_eigenvalues, ))

        matrices = backend.matmul(Vt.T, ev_weighting[:, :, None] * U.T)

        if use_alpha_batch:
            yield matrices, batch
        else:
            return matrices, batch

        del matrices


#: Dictionary with all banded ridge solvers
BANDED_RIDGE_SOLVERS = {
    "random_search": solve_banded_ridge_random_search,
}


def solve_ridge_cv_svd(X, Y, alphas=1.0, score_func=l2_neg_loss, cv=5,
                       local_alpha=True, n_targets_batch=None,
                       n_targets_batch_refit=None, n_alphas_batch=None,
                       conservative=False, Y_in_cpu=False):
    """Solve ridge regression with a grid search over alphas.

    Parameters
    ----------
    X : array of shape (n_samples, n_features)
        Input features.
    Y : array of shape (n_samples, n_targets)
        Target data.
    alphas : float or array of shape (n_alphas, )
        Range of ridge regularization parameter.
    score_func : callable
        Function used to compute the score of predictions versus Y.
    cv : int or scikit-learn splitter
        Cross-validation splitter. If an int, KFold is used.
    local_alpha : bool
        If True, alphas are selected per target, else shared over all targets.
    n_targets_batch : int or None
        Size of the batch for over targets during cross-validation.
        Used for memory reasons. If None, uses all n_targets at once.
    n_targets_batch_refit : int or None
        Size of the batch for over targets during refit.
        Used for memory reasons. If None, uses all n_targets at once.
    n_alphas_batch : int or None
        Size of the batch for over alphas. Used for memory reasons.
        If None, uses all n_alphas at once.
    conservative : bool
        If True, when selecting the hyperparameter alpha, take the largest one
        that is less than one standard deviation away from the best.
        If False, take the best.
    Y_in_cpu : bool
        If True, keep the target values ``Y`` in CPU memory (slower).

    Returns
    -------
    best_alphas : array of shape (n_targets, )
        Selected best hyperparameter alphas.
    coefs : array of shape (n_samples, n_targets)
        Ridge coefficients refit on the entire dataset, using selected
        best hyperparameters alpha. Always stored on CPU memory.
    cv_scores : array of shape (n_targets, )
        Cross-validation scores averaged over splits, for the best alpha.
    """
    backend = get_backend()

    n_iter = backend.ones_like(X, shape=(1, 1))
    fixed_params = dict(return_weights=True, progress_bar=False,
                        concentration=None, jitter_alphas=False,
                        random_state=None, n_iter=n_iter)

    copied_params = dict(alphas=alphas, score_func=score_func, cv=cv,
                         local_alpha=local_alpha,
                         n_targets_batch=n_targets_batch,
                         n_targets_batch_refit=n_targets_batch_refit,
                         n_alphas_batch=n_alphas_batch,
                         conservative=conservative, Y_in_cpu=Y_in_cpu)

    deltas, coefs, cv_scores = \
        solve_banded_ridge_random_search(
            [X], Y, **copied_params, **fixed_params)

    best_alphas = backend.exp(-deltas[0])
    return best_alphas, coefs, cv_scores
