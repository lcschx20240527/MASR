from typing import Tuple

import torch
from torch import Tensor

from masr.data_utils.normalizer import FeatureNormalizer
from masr.model_utils.conformer.decoder import BiTransformerDecoder
from masr.model_utils.conformer.encoder import ConformerEncoder
from masr.model_utils.conformer.loss import LabelSmoothingLoss, CTCLoss
from masr.model_utils.utils.cmvn import GlobalCMVN
from masr.model_utils.utils.common import (IGNORE_ID, add_sos_eos, th_accuracy, reverse_pad_list)


class ConformerModel(torch.nn.Module):
    def __init__(
            self,
            configs,
            input_dim: int,
            vocab_size: int,
            ctc_weight: float = 0.5,
            ignore_id: int = IGNORE_ID,
            reverse_weight: float = 0.0,
            lsm_weight: float = 0.0,
            length_normalized_loss: bool = False,
            use_dynamic_chunk: bool = False,
            use_dynamic_left_chunk: bool = False,
            causal: bool = False):
        assert 0.0 <= ctc_weight <= 1.0, ctc_weight
        super().__init__()
        self.input_dim = input_dim
        feature_normalizer = FeatureNormalizer(mean_istd_filepath=configs.dataset_conf.mean_istd_path)
        global_cmvn = GlobalCMVN(torch.from_numpy(feature_normalizer.mean).float(),
                                 torch.from_numpy(feature_normalizer.istd).float())
        self.encoder = ConformerEncoder(input_dim,
                                        global_cmvn=global_cmvn,
                                        use_dynamic_chunk=use_dynamic_chunk,
                                        use_dynamic_left_chunk=use_dynamic_left_chunk,
                                        causal=causal,
                                        **configs.encoder_conf)
        self.decoder = BiTransformerDecoder(vocab_size, self.encoder.output_size(), **configs.decoder_conf)

        self.ctc = CTCLoss(vocab_size, self.encoder.output_size())
        # note that eos is the same as sos (equivalent ID)
        self.sos = vocab_size - 1
        self.eos = vocab_size - 1
        self.vocab_size = vocab_size
        self.ignore_id = ignore_id
        self.ctc_weight = ctc_weight
        self.reverse_weight = reverse_weight

        self.criterion_att = LabelSmoothingLoss(
            size=vocab_size,
            padding_idx=ignore_id,
            smoothing=lsm_weight,
            normalize_length=length_normalized_loss,
        )

    def forward(
            self,
            speech: torch.Tensor,
            speech_lengths: torch.Tensor,
            text: torch.Tensor,
            text_lengths: torch.Tensor,
    ):
        """Frontend + Encoder + Decoder + Calc loss
        Args:
            speech: (Batch, Length, ...)
            speech_lengths: (Batch, )
            text: (Batch, Length)
            text_lengths: (Batch,)
        Returns:
            total_loss, attention_loss, ctc_loss
        """
        assert text_lengths.dim() == 1, text_lengths.shape
        # Check that batch_size is unified
        assert (speech.shape[0] == speech_lengths.shape[0] == text.shape[0] ==
                text_lengths.shape[0]), (speech.shape, speech_lengths.shape, text.shape, text_lengths.shape)
        # 1. Encoder
        encoder_out, encoder_mask = self.encoder(speech, speech_lengths)
        encoder_out_lens = encoder_mask.squeeze(1).sum(1)  # [B, 1, T] -> [B]

        # 2a. Attention-decoder branch
        loss_att = None
        if self.ctc_weight != 1.0:
            loss_att, acc_att = self._calc_att_loss(encoder_out, encoder_mask,
                                                    text, text_lengths,
                                                    self.reverse_weight)

        # 2b. CTC branch
        loss_ctc = None
        if self.ctc_weight != 0.0:
            loss_ctc = self.ctc(encoder_out, encoder_out_lens, text, text_lengths)

        if loss_ctc is None:
            loss = loss_att
        elif loss_att is None:
            loss = loss_ctc
        else:
            loss = self.ctc_weight * loss_ctc + (1 - self.ctc_weight) * loss_att
        return {"loss": loss, "loss_att": loss_att, "loss_ctc": loss_ctc}

    def _calc_att_loss(self,
                       encoder_out: torch.Tensor,
                       encoder_mask: torch.Tensor,
                       ys_pad: torch.Tensor,
                       ys_pad_lens: torch.Tensor,
                       reverse_weight: float) -> Tuple[torch.Tensor, float]:
        """Calc attention loss.

        Args:
            encoder_out (torch.Tensor): [B, Tmax, D]
            encoder_mask (torch.Tensor): [B, 1, Tmax]
            ys_pad (torch.Tensor): [B, Umax]
            ys_pad_lens (torch.Tensor): [B]
            reverse_weight (float): reverse decoder weight.

        Returns:
            Tuple[torch.Tensor, float]: attention_loss, accuracy rate
        """
        ys_in_pad, ys_out_pad = add_sos_eos(ys_pad, self.sos, self.eos, self.ignore_id)
        ys_in_lens = ys_pad_lens + 1

        r_ys_pad = reverse_pad_list(ys_pad, ys_pad_lens, float(self.ignore_id))
        r_ys_in_pad, r_ys_out_pad = add_sos_eos(r_ys_pad, self.sos, self.eos, self.ignore_id)
        # 1. Forward decoder
        decoder_out, r_decoder_out, _ = self.decoder(
            encoder_out, encoder_mask, ys_in_pad, ys_in_lens, r_ys_in_pad,
            self.reverse_weight)

        # 2. Compute attention loss
        loss_att = self.criterion_att(decoder_out, ys_out_pad)
        r_loss_att = torch.tensor(0.0)
        if self.reverse_weight > 0.0:
            r_loss_att = self.criterion_att(r_decoder_out, r_ys_out_pad)
        loss_att = loss_att * (1 - self.reverse_weight) + r_loss_att * self.reverse_weight
        acc_att = th_accuracy(
            decoder_out.view(-1, self.vocab_size),
            ys_out_pad,
            ignore_label=self.ignore_id,
        )
        return loss_att, acc_att

    @torch.jit.export
    def get_encoder_out(self, speech: torch.Tensor, speech_lengths: torch.Tensor) -> Tensor:
        """ Get encoder output

        Args:
            speech (torch.Tensor): (batch, max_len, feat_dim)
            speech_lengths (torch.Tensor): (batch, )
        Returns:
            Tensor: ctc softmax output
        """
        encoder_out, _ = self.encoder(speech,
                                      speech_lengths,
                                      decoding_chunk_size=-1,
                                      num_decoding_left_chunks=-1)  # (B, maxlen, encoder_dim)
        ctc_probs = self.ctc.softmax(encoder_out)
        return ctc_probs

    @torch.jit.export
    def get_encoder_out_chunk(self,
                              speech: torch.Tensor,
                              offset: int,
                              required_cache_size: int,
                              att_cache: torch.Tensor = torch.zeros([0, 0, 0, 0]),
                              cnn_cache: torch.Tensor = torch.zeros([0, 0, 0, 0])) -> Tuple[Tensor, Tensor, Tensor]:
        """ Get encoder output

        Args:
            speech (torch.Tensor): (batch, max_len, feat_dim)
        Returns:
            Tensor: ctc softmax output
        """
        xs, att_cache, cnn_cache = self.encoder.forward_chunk(xs=speech,
                                                              offset=offset,
                                                              required_cache_size=required_cache_size,
                                                              att_cache=att_cache,
                                                              cnn_cache=cnn_cache)
        ctc_probs = self.ctc.softmax(xs)
        return ctc_probs, att_cache, cnn_cache

    @torch.no_grad()
    def export(self):
        static_model = torch.jit.script(self)
        return static_model


def ConformerModelOnline(configs,
                         input_dim: int,
                         vocab_size: int,
                         ctc_weight: float = 0.5,
                         ignore_id: int = IGNORE_ID,
                         reverse_weight: float = 0.0,
                         lsm_weight: float = 0.0,
                         length_normalized_loss: bool = False):
    model = ConformerModel(configs=configs,
                           input_dim=input_dim,
                           vocab_size=vocab_size,
                           ctc_weight=ctc_weight,
                           ignore_id=ignore_id,
                           reverse_weight=reverse_weight,
                           lsm_weight=lsm_weight,
                           length_normalized_loss=length_normalized_loss,
                           use_dynamic_chunk=True,
                           use_dynamic_left_chunk=False,
                           causal=True)
    return model


def ConformerModelOffline(configs,
                          input_dim: int,
                          vocab_size: int,
                          ctc_weight: float = 0.5,
                          ignore_id: int = IGNORE_ID,
                          reverse_weight: float = 0.0,
                          lsm_weight: float = 0.0,
                          length_normalized_loss: bool = False):
    model = ConformerModel(configs=configs,
                           input_dim=input_dim,
                           vocab_size=vocab_size,
                           ctc_weight=ctc_weight,
                           ignore_id=ignore_id,
                           reverse_weight=reverse_weight,
                           lsm_weight=lsm_weight,
                           length_normalized_loss=length_normalized_loss)
    return model
