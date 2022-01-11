#encoding:utf-8

import random
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data
import torchvision
from torchvision import models,transforms
from torch.autograd import Function
import torch.nn.functional as F

#学習用モデルを構成するための各部品
import .model_component.monotonic_align
from .model_component.decoder import Decoder
from .model_component.flow import Flow
from .model_component.posterior_encoder import PosteriorEncoder
from .model_component.stochastic_duration_predictor import StochasticDurationPredictor
from .model_component.text_encoder import TextEncoder

#モデルの学習を行うためのクラス
class VitsGenerator(nn.Module):
  def __init__(self, n_vocab):
    super().__init__()
    self.n_vocab = n_vocab
    self.spec_channels = 513
    self.inter_channels = 192
    self.hidden_channels = 192
    self.filter_channels = 768
    self.n_heads = 2
    self.n_layers = 6
    self.kernel_size = 3
    self.p_dropout = 0.1
    self.resblock = 1
    self.resblock_kernel_sizes = [3,7,11]
    self.resblock_dilation_sizes = [[1,3,5], [1,3,5], [1,3,5]]
    self.upsample_rates = [8,8,2,2]
    self.upsample_initial_channel = 512
    self.upsample_kernel_sizes = [16,16,4,4]
    self.segment_size = 32
    self.n_speakers = 109
    self.gin_channels = 256

    self.text_encoder = TextEncoder(n_vocab,
        inter_channels,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        kernel_size,
        p_dropout)
    self.decoder = Decoder(inter_channels, resblock, resblock_kernel_sizes, resblock_dilation_sizes, upsample_rates, upsample_initial_channel, upsample_kernel_sizes, gin_channels=gin_channels)
    self.posterior_encoder = PosteriorEncoder(spec_channels, inter_channels, hidden_channels, 5, 1, 16, gin_channels=gin_channels)
    self.flow = Flow(inter_channels, hidden_channels, 5, 1, 4, gin_channels=gin_channels)
    self.stochastic_duration_predictor = StochasticDurationPredictor(hidden_channels, 192, 3, 0.5, 4, gin_channels=gin_channels)
    self.speaker_embedding = nn.Embedding(n_speakers, gin_channels)#話者埋め込み用ネットワーク

  def forward(self, x, x_lengths, y, y_lengths, speaker_id):
    #x.size() : torch.Size([64, 145(example)])
    #x_lengths.size() : torch.Size([64])
    #y.size() : torch.Size([64, 513, 400]) spectrogram
    #y_lengths.size() : torch.Size([64])

    x, m_p, logs_p, x_mask = self.text_encoder(x, x_lengths)
    #x.size() : torch.Size([64, 192, 157(example)])
    #m_p.size() : torch.Size([64, 192, 157(example)])
    #logs_p.size() : torch.Size([64, 192, 157(example)])
    #x_mask.size() : torch.Size([64, 1, 157(example)])

    g = self.speaker_embedding(speaker_id).unsqueeze(-1) # [b, h, 1] #話者埋め込み用ネットワーク
    #g.size() : torch.Size([64, 256, 1])

    #y.size() : torch.Size([64, 513, 400])
    #y_lengths.size() : torch.Size([64])
    z, m_q, logs_q, y_mask = self.posterior_encoder(y, y_lengths, g=g)

    #z.size() : torch.Size([64, 192, 400])
    #y_mask.size() : torch.Size([64, 1, 400])
    z_p = self.flow(z, y_mask, g=g)
    #z_p.size() : torch.Size([64, 192, 400])

    with torch.no_grad():
      # negative cross-entropy
      #logs_p.size() : torch.Size([64, 192, 157(example)])
      s_p_sq_r = torch.exp(-2 * logs_p) # [b, d, t]
      #s_p_sq_r.size() : torch.Size([64, 192, 157(example)])
      neg_cent1 = torch.sum(-0.5 * math.log(2 * math.pi) - logs_p, [1], keepdim=True) # [b, 1, t_s]
      #neg_cent1.size() : torch.Size([64, 1, 157(example)])
      neg_cent2 = torch.matmul(-0.5 * (z_p ** 2).transpose(1, 2), s_p_sq_r) # [b, t_t, d] x [b, d, t_s] = [b, t_t, t_s]
      #neg_cent2.size() : torch.Size([64, 400, 157(example)])
      neg_cent3 = torch.matmul(z_p.transpose(1, 2), (m_p * s_p_sq_r)) # [b, t_t, d] x [b, d, t_s] = [b, t_t, t_s]
      #neg_cent3.size() : torch.Size([64, 400, 157(example)])
      neg_cent4 = torch.sum(-0.5 * (m_p ** 2) * s_p_sq_r, [1], keepdim=True) # [b, 1, t_s]
      #neg_cent4.size() : torch.Size([64, 1, 157(example)])
      neg_cent = neg_cent1 + neg_cent2 + neg_cent3 + neg_cent4
      #neg_cent.size() : torch.Size([64, 400, 157(example)])

      #x_mask.size() : torch.Size([64, 1, 157(exmaple)])
      #y_mask.size() : torch.Size([64, 1, 400])
      #torch.unsqueeze(x_mask, 2).size() : torch.Size([64, 1, 1, 157(exmaple)])
      #torch.unsqueeze(y_mask, -1).size() : torch.Size([64, 1, 400, 1])
      attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
      #attn_mask.size() : torch.Size([64, 1, 400, 157(exmaple)])
      #neg_cent.size() : torch.Size([64, 400, 157(example)])
      attn = monotonic_align.maximum_path(neg_cent, attn_mask.squeeze(1)).unsqueeze(1).detach()

    #attn.size() : torch.Size([64, 1, 400, 157])
    w = attn.sum(2)
    #w.size() : torch.Size([64, 1, 157])

    l_length = self.stochastic_duration_predictor(x, x_mask, w, g=g)
    l_length = l_length / torch.sum(x_mask)

    # expand prior
    m_p = torch.matmul(attn.squeeze(1), m_p.transpose(1, 2)).transpose(1, 2)
    logs_p = torch.matmul(attn.squeeze(1), logs_p.transpose(1, 2)).transpose(1, 2)

    #z.size() : torch.Size([64, 192, 400])
    #y_lengths.size() : torch.Size([64])
    #self.segment_size = 32
    z_slice, ids_slice = commons.rand_slice_segments(z, y_lengths, self.segment_size)

    #z_slice.size() : torch.Size([64, 192, 32])
    o = self.decoder(z_slice, g=g)
    #o.size() : torch.Size([64, 1, 8192])

    return o, l_length, attn, ids_slice, x_mask, y_mask, (z, z_p, m_p, logs_p, m_q, logs_q)

  def infer(self, x, x_lengths, speaker_id, noise_scale=1, length_scale=1, noise_scale_w=1., max_len=None):
    x, m_p, logs_p, x_mask = self.text_encoder(x, x_lengths)
    g = self.speaker_embedding(speaker_id).unsqueeze(-1) # [b, h, 1] #話者埋め込み用ネットワーク

    logw = self.stochastic_duration_predictor(x, x_mask, g=g, reverse=True, noise_scale=noise_scale_w)

    w = torch.exp(logw) * x_mask * length_scale
    w_ceil = torch.ceil(w)
    y_lengths = torch.clamp_min(torch.sum(w_ceil, [1, 2]), 1).long()
    y_mask = torch.unsqueeze(commons.sequence_mask(y_lengths, None), 1).to(x_mask.dtype)
    attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
    attn = commons.generate_path(w_ceil, attn_mask)

    m_p = torch.matmul(attn.squeeze(1), m_p.transpose(1, 2)).transpose(1, 2) # [b, t', t], [b, t, d] -> [b, d, t']
    logs_p = torch.matmul(attn.squeeze(1), logs_p.transpose(1, 2)).transpose(1, 2) # [b, t', t], [b, t, d] -> [b, d, t']

    z_p = m_p + torch.randn_like(m_p) * torch.exp(logs_p) * noise_scale
    z = self.flow(z_p, y_mask, g=g, reverse=True)
    o = self.decoder((z * y_mask)[:,:,:max_len], g=g)
    return o, attn, y_mask, (z, z_p, m_p, logs_p)

  def voice_conversion(self, y, y_lengths, speaker_id_source, speaker_id_target):
    assert self.n_speakers > 0, "n_speakers have to be larger than 0."
    g_source = self.speaker_embedding(speaker_id_source).unsqueeze(-1) #話者埋め込み用ネットワーク
    g_target = self.speaker_embedding(speaker_id_target).unsqueeze(-1) #話者埋め込み用ネットワーク
    z, m_q, logs_q, y_mask = self.posterior_encoder(y, y_lengths, g=g_source)
    z_p = self.flow(z, y_mask, g=g_source)
    z_hat = self.flow(z_p, y_mask, g=g_target, reverse=True)
    o_hat = self.decoder(z_hat * y_mask, g=g_target)
    return o_hat, y_mask, (z, z_p, z_hat)