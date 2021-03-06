"""
EEG artifact correction using FASTER

based on pull request for mne-python:
https://github.com/mne-tools/mne-python/pull/1777

NOTE: this code is experimental!!!
"""

def _handle_default(k, v=None):
    bads_faster=dict(max_iter=1, thresh=3, use_metrics=None)
    return bads_faster

### faster_.py

# Authors: Marijn van Vliet <w.m.vanvliet@gmail.com>
#
# License: BSD (3-clause)

import numpy as np
from collections import defaultdict
from mne.utils import logger
from mne.io.pick import pick_info, _picks_by_type
from mne.preprocessing.bads import find_outliers


def _hurst(x):
    """Estimate Hurst exponent on a timeseries.

    The estimation is based on the second order discrete derivative.

    Parameters
    ----------
    x : array, shape(n_channels, n_samples)
        The timeseries to estimate the Hurst exponent for.

    Returns
    -------
    h : float
        The estimation of the Hurst exponent for the given timeseries.
    """
    from scipy.signal import lfilter
    y = np.cumsum(np.diff(x, axis=1), axis=1)

    b1 = [1, -2, 1]
    b2 = [1,  0, -2, 0, 1]

    # second order derivative
    y1 = lfilter(b1, 1, y, axis=1)
    y1 = y1[:, len(b1) - 1:-1]  # first values contain filter artifacts

    # wider second order derivative
    y2 = lfilter(b2, 1, y, axis=1)
    y2 = y2[:, len(b2) - 1:-1]  # first values contain filter artifacts

    s1 = np.mean(y1 ** 2, axis=1)
    s2 = np.mean(y2 ** 2, axis=1)

    return 0.5 * np.log2(s2 / s1)


def _efficient_welch(data, sfreq):
    """Calls scipy.signal.welch with parameters optimized for greatest speed
    at the expense of precision. The window is set to ~10 seconds and windows
    are non-overlapping.

    Parameters
    ----------
    data : array, shape (..., n_samples)
        The timeseries to estimate signal power for. The last dimension
        is assumed to be time.
    sfreq : float
        The sample rate of the timeseries.

    Returns
    -------
    fs : array of float
        The frequencies for which the power spectra was calculated.
    ps : array, shape (..., frequencies)
        The power spectra for each timeseries.
    """
    from scipy.signal import welch
    nperseg = min(data.shape[-1],
                  2 ** int(np.log2(10 * sfreq) + 1))  # next power of 2

    return welch(data, sfreq, nperseg=nperseg, noverlap=0, axis=-1)


def _freqs_power(data, sfreq, freqs):
    """Estimate signal power at specific frequencies.

    Parameters
    ----------
    data : array, shape (..., n_samples)
        The timeseries to estimate signal power for. The last dimension
        is presumed to be time.
    sfreq : float
        The sample rate of the timeseries.
    freqs : array of float
        The frequencies to estimate signal power for.

    Returns
    -------
    p : float
        The summed signal power of each requested frequency.
    """
    fs, ps = _efficient_welch(data, sfreq)
    try:
        return np.sum([ps[..., np.searchsorted(fs, f)] for f in freqs], axis=0)
    except IndexError:
        raise ValueError(
            ("Insufficient sample rate to  estimate power at {} Hz for line "
             "noise detection. Use the 'metrics' parameter to disable the "
             "'line_noise' metric.").format(freqs))


def _power_gradient(data, sfreq, prange):
    """Estimate the gradient of the power spectrum at upper frequencies.

    Parameters
    ----------
    data : array, shape (n_components, n_samples)
        The timeseries to estimate signal power for. The last dimension
        is presumed to be time.
    sfreq : float
        The sample rate of the timeseries.
    prange : pair of floats
        The (lower, upper) frequency limits of the power spectrum to use. In
        the FASTER paper, they set these to the passband of the lowpass filter.

    Returns
    -------
    grad : array of float
        The gradients of the timeseries.
    """
    fs, ps = _efficient_welch(data, sfreq)

    # Limit power spectrum to selected frequencies
    start, stop = (np.searchsorted(fs, p) for p in prange)
    if start >= ps.shape[1]:
        raise ValueError(("Sample rate insufficient to estimate {} Hz power. "
                          "Use the 'power_gradient_range' parameter to tweak "
                          "the tested frequencies for this metric or use the "
                          "'metrics' parameter to disable the "
                          "'power_gradient' metric.").format(prange[0]))
    ps = ps[:, start:stop]

    # Compute mean gradients
    return np.mean(np.diff(ps), axis=1)


def _deviation(data):
    """Computes the deviation from mean for each channel in a set of epochs.

    This is not implemented as a lambda function, because the channel means
    should be cached during the computation.

    Parameters
    ----------
    data : array, shape (n_epochs, n_channels, n_samples)
        The epochs for which to compute the channel deviation.

    Returns
    -------
    dev : list of float
        For each epoch, the mean deviation of the channels.
    """
    ch_mean = np.mean(data, axis=2)
    return ch_mean - np.mean(ch_mean, axis=0)


def _find_bad_channels(epochs, picks, use_metrics, thresh, max_iter):
    """Implements the first step of the FASTER algorithm.

    This function attempts to automatically mark bad EEG channels by performing
    outlier detection. It operated on epoched data, to make sure only relevant
    data is analyzed.

    Additional Parameters
    ---------------------
    use_metrics : list of str
        List of metrics to use. Can be any combination of:
            'variance', 'correlation', 'hurst', 'kurtosis', 'line_noise'
        Defaults to all of them.
    thresh : float
        The threshold value, in standard deviations, to apply. A channel
        crossing this threshold value is marked as bad. Defaults to 3.
    max_iter : int
        The maximum number of iterations performed during outlier detection
        (defaults to 1, as in the original FASTER paper).
    """
    from scipy.stats import kurtosis
    metrics = {
        'variance': lambda x: np.var(x, axis=1),
        'correlation': lambda x: np.mean(
            np.ma.masked_array(np.corrcoef(x),
                               np.identity(len(x), dtype=bool)), axis=0),
        'hurst': lambda x: _hurst(x),
        'kurtosis': lambda x: kurtosis(x, axis=1),
        'line_noise': lambda x: _freqs_power(x, epochs.info['sfreq'],
                                             [50, 60]),
    }

    if use_metrics is None:
        use_metrics = metrics.keys()

    # Concatenate epochs in time
    data = epochs.get_data()[:, picks]
    data = data.transpose(1, 0, 2).reshape(data.shape[1], -1)

    # Find bad channels
    bads = defaultdict(list)
    info = pick_info(epochs.info, picks, copy=True)
    for ch_type, chs in _picks_by_type(info):
        logger.info('Bad channel detection on %s channels:' % ch_type.upper())
        for metric in use_metrics:
            scores = metrics[metric](data[chs])
            bad_channels = [epochs.ch_names[picks[chs[i]]]
                            for i in find_outliers(scores, thresh, max_iter)]
            logger.info('\tBad by %s: %s' % (metric, bad_channels))
            bads[metric].append(bad_channels)

    bads = dict((k, np.concatenate(v).tolist()) for k, v in bads.items())
    return bads


def _find_bad_epochs(epochs, picks, use_metrics, thresh, max_iter):
    """Implements the second step of the FASTER algorithm.

    This function attempts to automatically mark bad epochs by performing
    outlier detection.

    Additional Parameters
    ---------------------
    use_metrics : list of str
        List of metrics to use. Can be any combination of:
        'amplitude', 'variance', 'deviation'. Defaults to all of them.
    thresh : float
        The threshold value, in standard deviations, to apply. A channel
        crossing this threshold value is marked as bad. Defaults to 3.
    max_iter : int
        The maximum number of iterations performed during outlier detection
        (defaults to 1, as in the original FASTER paper).
    """

    metrics = {
        'amplitude': lambda x: np.mean(np.ptp(x, axis=2), axis=1),
        'deviation': lambda x: np.mean(_deviation(x), axis=1),
        'variance': lambda x: np.mean(np.var(x, axis=2), axis=1),
    }

    if use_metrics is None:
        use_metrics = metrics.keys()

    info = pick_info(epochs.info, picks, copy=True)
    data = epochs.get_data()[:, picks]

    bads = defaultdict(list)
    for ch_type, chs in _picks_by_type(info):
        logger.info('Bad epoch detection on %s channels:' % ch_type.upper())
        for metric in use_metrics:
            scores = metrics[metric](data[:, chs])
            bad_epochs = find_outliers(scores, thresh, max_iter)
            logger.info('\tBad by %s: %s' % (metric, bad_epochs))
            bads[metric].append(bad_epochs)

    bads = dict((k, np.concatenate(v).tolist()) for k, v in bads.items())
    return bads


def _find_bad_channels_in_epochs(epochs, picks, use_metrics, thresh, max_iter):
    """Implements the fourth step of the FASTER algorithm.

    This function attempts to automatically mark bad channels in each epochs by
    performing outlier detection.

    Additional Parameters
    ---------------------
    use_metrics : list of str
        List of metrics to use. Can be any combination of:
        'amplitude', 'variance', 'deviation', 'median_gradient'
        Defaults to all of them.
    thresh : float
        The threshold value, in standard deviations, to apply. A channel
        crossing this threshold value is marked as bad. Defaults to 3.
    max_iter : int
        The maximum number of iterations performed during outlier detection
        (defaults to 1, as in the original FASTER paper).
    """

    metrics = {
        'amplitude': lambda x: np.ptp(x, axis=2),
        'deviation': lambda x: _deviation(x),
        'variance': lambda x: np.var(x, axis=2),
        'median_gradient': lambda x: np.median(np.abs(np.diff(x)), axis=2),
        'line_noise': lambda x: _freqs_power(x, epochs.info['sfreq'],
                                             [50, 60]),
    }

    if use_metrics is None:
        use_metrics = metrics.keys()

    info = pick_info(epochs.info, picks, copy=True)
    data = epochs.get_data()[:, picks]
    bads = dict((m, np.zeros((len(data), len(picks)), dtype=bool)) for
                m in metrics)
    for ch_type, chs in _picks_by_type(info):
        ch_names = [info['ch_names'][k] for k in chs]
        chs = np.array(chs)
        for metric in use_metrics:
            logger.info('Bad channel-in-epoch detection on %s channels:'
                        % ch_type.upper())
            s_epochs = metrics[metric](data[:, chs])
            for i_epochs, epoch in enumerate(s_epochs):
                outliers = find_outliers(epoch, thresh, max_iter)
                if len(outliers) > 0:
                    bad_segment = [ch_names[k] for k in outliers]
                    logger.info('Epoch %d, Bad by %s:\n\t%s' % (
                        i_epochs, metric, bad_segment))
                    bads[metric][i_epochs, chs[outliers]] = True

    return bads


### bads.py

# Authors: Denis Engemann <denis.engemann@gmail.com>
#          Marijn van Vliet <w.m.vanvliet@gmail.com>
# License: BSD (3-clause)

import numpy as np
from mne.utils import verbose
from mne.io.pick import pick_info
from mne.io.pick import pick_types
#from mne.defaults import _handle_default
#from . import faster_ as _faster

@verbose
def find_bad_channels(epochs, picks=None, method='faster', method_params=None,
                      return_by_metric=False, verbose=None):
    """Implements the first step of the FASTER algorithm.

    This function attempts to automatically mark bad EEG channels by performing
    outlier detection. It operated on epoched data, to make sure only relevant
    data is analyzed.

    Parameters
    ----------
    epochs : Instance of Epochs
        The epochs for which bad channels need to be marked
    picks : list of int | None
        Channels to operate on. Defaults to EEG channels.
    method : {'faster'}
        The detection algorithm.
    method_params : dict | None
        The method parameters in a dict.

        If ``method`` equals 'faster', and ``method_params``is None,
        defaults to the following parameters. Partial updates are supported.
        use_metrics : list of str
            List of metrics to use. Can be any combination of:
                'variance', 'correlation', 'hurst', 'kurtosis', 'line_noise'
            Defaults to all of them.
        thresh : float
            The threshold value, in standard deviations, to apply. A channel
            crossing this threshold value is marked as bad. Defaults to 3.
        max_iter : int
            The maximum number of iterations performed during outlier detection
            (defaults to 1, as in the original FASTER paper).

    return_by_metric : bool
        Whether to return the bad channels as a flat list (False, default) or
        as a dictionary with the names of the used metrics as keys and the
        bad channels found by this metric as values. Is ignored if not
        supported by method.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).
        Defaults to self.verbose.

    Returns
    -------
    bads : list of str
        The names of the bad EEG channels.
    """
    if picks is None:
        picks = pick_types(epochs.info, meg=True, eeg=True, exclude=[])
    _method_params = _handle_default('bads' + '_' + method, method_params)
    if method == 'faster':
        bads = _find_bad_channels(epochs, picks, **_method_params)
    else:
        raise NotImplementedError(
            'Come back later, for now there is only "FASTER"')

    if return_by_metric:
        return bads
    else:
        return _combine_indices(bads)


@verbose
def find_bad_epochs(epochs, picks=None, return_by_metric=False,
                    method='faster', method_params=None, verbose=None):
    """Implements the second step of the FASTER algorithm.

    This function attempts to automatically mark bad epochs by performing
    outlier detection.

    Parameters
    ----------
    epochs : Instance of Epochs
        The epochs to analyze.
    picks : list of int | None
        Channels to operate on. Defaults to EEG channels.
    method : {'faster'}
        The detection algorithm.
    method_params : dict | None
        The method parameters in a dict.

        If ``method`` equals 'faster', and ``method_params``is None,
        defaults to the following parameters. Partial updates are supported.
        use_metrics : list of str
            List of metrics to use. Can be any combination of:
            'amplitude', 'variance', 'deviation'. Defaults to all of them.
        thresh : float
            The threshold value, in standard deviations, to apply. A channel
            crossing this threshold value is marked as bad. Defaults to 3.
        max_iter : int
            The maximum number of iterations performed during outlier detection
            (defaults to 1, as in the original FASTER paper).

    return_by_metric : bool
        Whether to return the bad channels as a flat list (False, default) or
        as a dictionary with the names of the used metrics as keys and the
        bad channels found by this metric as values. Is ignored if not
        supported by method.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).
        Defaults to self.verbose.

    Returns
    -------
    bads : list of int
        The indices of the bad epochs.
    """
    if picks is None:
        picks = pick_types(epochs.info, meg=True, eeg=True, exclude='bads')
    _method_params = _handle_default('bads' + '_' + method, method_params)
    if method == 'faster':
        bads = _find_bad_epochs(epochs, picks, **_method_params)
    else:
        raise NotImplementedError(
            'Come back later, for now there is only "FASTER"')

    if return_by_metric:
        return bads
    else:
        return _combine_indices(bads)


@verbose
def find_bad_channels_in_epochs(epochs, picks=None, method='faster',
                                method_params=None, return_by_metric=False):
    """Implements the fourth step of the FASTER algorithm.

    This function attempts to automatically mark bad channels in each epochs by
    performing outlier detection.

    Parameters
    ----------
    epochs : Instance of Epochs
        The epochs to analyze.
    picks : list of int | None
        Channels to operate on. Defaults to EEG channels.
    method : {'faster'}
        The detection algorithm.
    method_params : dict | None
        The method parameters in a dict.

        If ``method`` equals 'faster', and ``method_params``is None,
        defaults to the following parameters. Partial updates are supported.
        use_metrics : list of str
            List of metrics to use. Can be any combination of:
            'amplitude', 'variance', 'deviation', 'median_gradient'
            Defaults to all of them.
        thresh : float
            The threshold value, in standard deviations, to apply. A channel
            crossing this threshold value is marked as bad. Defaults to 3.
        max_iter : int
            The maximum number of iterations performed during outlier detection
            (defaults to 1, as in the original FASTER paper).

    return_by_metric : bool
        Whether to return the bad channels as a flat list (False, default) or
        as a dictionary with the names of the used metrics as keys and the
        bad channels found by this metric as values. Is ignored if not
        supported by method.

    Returns
    -------
    bads : list of lists of int
        For each epoch, the indices of the bad channels.
    """
    if picks is None:
        picks = pick_types(epochs.info, meg=True, eeg=True, exclude=[])

    _method_params = _handle_default('bads' + '_' + method, method_params)
    if method == 'faster':
        bads = _find_bad_channels_in_epochs(epochs, picks,
                                                    **_method_params)
    else:
        raise NotImplementedError(
            'Come back later, for now there is only "FASTER"')

    info = pick_info(epochs.info, picks, copy=True)
    if return_by_metric:
        bads = dict((m, _bad_mask_to_names(info, v)) for m, v in bads.items())
    else:
        bads = np.sum(bads.values(), axis=0).astype(bool)
        bads = _bad_mask_to_names(info, bads)

    return bads


def _bad_mask_to_names(info, bad_mask):
    """Remap mask to ch names"""
    bad_idx = [np.where(m)[0] for m in bad_mask]
    return [[info['ch_names'][k] for k in epoch] for epoch in bad_idx]


def _combine_indices(bads):
    """summarize indices"""
    return list(set(v for val in bads.values() if len(val) > 0 for v in val))
