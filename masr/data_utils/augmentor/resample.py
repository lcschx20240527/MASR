"""Contain the resample augmentation model."""
import numpy as np

from masr.data_utils.audio import AudioSegment
from masr.data_utils.augmentor.base import AugmentorBase


class ResampleAugmentor(AugmentorBase):
    """重采样的增强模型

    See more info here:
    https://ccrma.stanford.edu/~jos/resample/index.html
    
    :param rng: Random generator object.
    :type rng: random.Random
    :param new_sample_rate: New sample rate in Hz.
    :type new_sample_rate: int
    """

    def __init__(self, rng, new_sample_rate: list):
        self._new_sample_rate = new_sample_rate
        self._rng = rng

    def transform_audio(self, audio_segment: AudioSegment):
        """Resamples the input audio to a target sample rate.

        Note that this is an in-place transformation.

        :param audio_segment: Audio segment to add effects to.
        :type audio_segment: AudioSegment|SpeechSegment
        """
        _new_sample_rate = np.random.choice(self._new_sample_rate)
        audio_segment.resample(_new_sample_rate)
