#encoding:utf-8

import random
import numpy as np
import math

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data
import torchvision
from torchvision import models,transforms
from torch.autograd import Function
import torch.nn.functional as F

def convert_pad_shape(pad_shape):
    l = pad_shape[::-1]
    pad_shape = [item for sublist in l for item in sublist]
    return pad_shape

class MultiHeadAttention(nn.Module):
    def __init__(self, channels, out_channels, n_heads, p_dropout=0., window_size=None, heads_share=True, block_length=None, proximal_bias=False, proximal_init=False):
        super().__init__()
        assert channels % n_heads == 0

        self.channels = channels
        self.out_channels = out_channels
        self.n_heads = n_heads
        self.p_dropout = p_dropout
        self.window_size = window_size
        self.heads_share = heads_share
        self.block_length = block_length
        self.proximal_bias = proximal_bias
        self.proximal_init = proximal_init
        self.attn = None

        self.k_channels = channels // n_heads
        self.conv_q = nn.Conv1d(channels, channels, 1)
        self.conv_k = nn.Conv1d(channels, channels, 1)
        self.conv_v = nn.Conv1d(channels, channels, 1)
        self.conv_o = nn.Conv1d(channels, out_channels, 1)
        self.drop = nn.Dropout(p_dropout)

        if window_size is not None:
            n_heads_rel = 1 if heads_share else n_heads
            rel_stddev = self.k_channels**-0.5
            self.emb_rel_k = nn.Parameter(torch.randn(n_heads_rel, window_size * 2 + 1, self.k_channels) * rel_stddev)
            self.emb_rel_v = nn.Parameter(torch.randn(n_heads_rel, window_size * 2 + 1, self.k_channels) * rel_stddev)

        nn.init.xavier_uniform_(self.conv_q.weight)
        nn.init.xavier_uniform_(self.conv_k.weight)
        nn.init.xavier_uniform_(self.conv_v.weight)
        if proximal_init:
            with torch.no_grad():
                self.conv_k.weight.copy_(self.conv_q.weight)
                self.conv_k.bias.copy_(self.conv_q.bias)
      
    def forward(self, x, c, attn_mask=None):
        q = self.conv_q(x)
        k = self.conv_k(c)
        v = self.conv_v(c)
        
        x, self.attn = self.attention(q, k, v, mask=attn_mask)

        x = self.conv_o(x)
        return x

    def attention(self, query, key, value, mask=None):
        # reshape [b, d, t] -> [b, n_h, t, d_k]
        b, d, t_s, t_t = (*key.size(), query.size(2))
        query = query.view(b, self.n_heads, self.k_channels, t_t).transpose(2, 3)
        key = key.view(b, self.n_heads, self.k_channels, t_s).transpose(2, 3)
        value = value.view(b, self.n_heads, self.k_channels, t_s).transpose(2, 3)

        scores = torch.matmul(query / math.sqrt(self.k_channels), key.transpose(-2, -1))
        if self.window_size is not None:
            assert t_s == t_t, "Relative attention is only available for self-attention."
            key_relative_embeddings = self._get_relative_embeddings(self.emb_rel_k, t_s)
            rel_logits = self._matmul_with_relative_keys(query /math.sqrt(self.k_channels), key_relative_embeddings)
            scores_local = self._relative_position_to_absolute_position(rel_logits)
            scores = scores + scores_local
        if self.proximal_bias:
            assert t_s == t_t, "Proximal bias is only available for self-attention."
            scores = scores + self._attention_bias_proximal(t_s).to(device=scores.device, dtype=scores.dtype)
        if mask is not None:
            scores = scores.masked_fill(mask == 0, -1e4)
            if self.block_length is not None:
                assert t_s == t_t, "Local attention is only available for self-attention."
                block_mask = torch.ones_like(scores).triu(-self.block_length).tril(self.block_length)
                scores = scores.masked_fill(block_mask == 0, -1e4)
        p_attn = F.softmax(scores, dim=-1) # [b, n_h, t_t, t_s]
        p_attn = self.drop(p_attn)
        output = torch.matmul(p_attn, value)
        if self.window_size is not None:
            relative_weights = self._absolute_position_to_relative_position(p_attn)
            value_relative_embeddings = self._get_relative_embeddings(self.emb_rel_v, t_s)
            output = output + self._matmul_with_relative_values(relative_weights, value_relative_embeddings)
        output = output.transpose(2, 3).contiguous().view(b, d, t_t) # [b, n_h, t_t, d_k] -> [b, d, t_t]
        return output, p_attn

    def _matmul_with_relative_values(self, x, y):
        """
        x: [b, h, l, m]
        y: [h or 1, m, d]
        ret: [b, h, l, d]
        """
        ret = torch.matmul(x, y.unsqueeze(0))
        return ret

    def _matmul_with_relative_keys(self, x, y):
        """
        x: [b, h, l, d]
        y: [h or 1, m, d]
        ret: [b, h, l, m]
        """
        ret = torch.matmul(x, y.unsqueeze(0).transpose(-2, -1))
        return ret

    def _get_relative_embeddings(self, relative_embeddings, length):
        max_relative_position = 2 * self.window_size + 1
        # Pad first before slice to avoid using cond ops.
        pad_length = max(length - (self.window_size + 1), 0)
        slice_start_position = max((self.window_size + 1) - length, 0)
        slice_end_position = slice_start_position + 2 * length - 1
        if pad_length > 0:
            padded_relative_embeddings = F.pad(
                relative_embeddings,
                convert_pad_shape([[0, 0], [pad_length, pad_length], [0, 0]]))
        else:
            padded_relative_embeddings = relative_embeddings
        used_relative_embeddings = padded_relative_embeddings[:,slice_start_position:slice_end_position]
        return used_relative_embeddings

    def _relative_position_to_absolute_position(self, x):
        """
        x: [b, h, l, 2*l-1]
        ret: [b, h, l, l]
        """
        batch, heads, length, _ = x.size()
        # Concat columns of pad to shift from relative to absolute indexing.
        x = F.pad(x, convert_pad_shape([[0,0],[0,0],[0,0],[0,1]]))

        # Concat extra elements so to add up to shape (len+1, 2*len-1).
        x_flat = x.view([batch, heads, length * 2 * length])
        x_flat = F.pad(x_flat, convert_pad_shape([[0,0],[0,0],[0,length-1]]))

        # Reshape and slice out the padded elements.
        x_final = x_flat.view([batch, heads, length+1, 2*length-1])[:, :, :length, length-1:]
        return x_final

    def _absolute_position_to_relative_position(self, x):
        """
        x: [b, h, l, l]
        ret: [b, h, l, 2*l-1]
        """
        batch, heads, length, _ = x.size()
        # padd along column
        x = F.pad(x, convert_pad_shape([[0, 0], [0, 0], [0, 0], [0, length-1]]))
        x_flat = x.view([batch, heads, length**2 + length*(length -1)])
        # add 0's in the beginning that will skew the elements after reshape
        x_flat = F.pad(x_flat, convert_pad_shape([[0, 0], [0, 0], [length, 0]]))
        x_final = x_flat.view([batch, heads, length, 2*length])[:,:,:,1:]
        return x_final

    def _attention_bias_proximal(self, length):
        """Bias for self-attention to encourage attention to close positions.
        Args:
        length: an integer scalar.
        Returns:
        a Tensor with shape [1, 1, length, length]
        """
        r = torch.arange(length, dtype=torch.float32)
        diff = torch.unsqueeze(r, 0) - torch.unsqueeze(r, 1)
        return torch.unsqueeze(torch.unsqueeze(-torch.log1p(torch.abs(diff)), 0), 0)


class FeedForwardNetwork(nn.Module):
    def __init__(self, in_channels, out_channels, filter_channels, kernel_size, p_dropout=0., activation=None, causal=False):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.filter_channels = filter_channels
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.padding = self._same_padding

        self.conv_1 = nn.Conv1d(in_channels, filter_channels, kernel_size)
        self.conv_2 = nn.Conv1d(filter_channels, out_channels, kernel_size)
        self.drop = nn.Dropout(p_dropout)

    def forward(self, x, x_mask):
        x = self.conv_1(self.padding(x * x_mask))
        x = torch.relu(x)
        x = self.drop(x)
        x = self.conv_2(self.padding(x * x_mask))
        return x * x_mask

    def _same_padding(self, x):
        #self.kernel_size = 3 固定
        if self.kernel_size == 1:
            return x
        pad_l = (self.kernel_size - 1) // 2#1
        pad_r = self.kernel_size // 2#1
        padding = [[0, 0], [0, 0], [pad_l, pad_r]]
        #特徴量の左右を1ずつpadding
        x = F.pad(x, convert_pad_shape(padding))
        return x

#Transformerに似た、AttentionとFeedForwardNetworkからなるEncoder
class Encoder(nn.Module):
    def __init__(self, phoneme_embedding_dim, filter_channels, n_heads, n_layers, kernel_size=1, p_dropout=0., window_size=4, **kwargs):
        super().__init__()
        self.phoneme_embedding_dim = phoneme_embedding_dim
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout
        self.window_size = window_size

        self.drop = nn.Dropout(p_dropout)
        self.attention_layers = nn.ModuleList()
        self.norm_layers_1 = nn.ModuleList()
        self.ffn_layers = nn.ModuleList()
        self.norm_layers_2 = nn.ModuleList()
        for i in range(self.n_layers):
            self.attention_layers.append(MultiHeadAttention(phoneme_embedding_dim, phoneme_embedding_dim, n_heads, p_dropout=p_dropout, window_size=window_size))
            self.norm_layers_1.append(torch.nn.LayerNorm(phoneme_embedding_dim))
            self.ffn_layers.append(FeedForwardNetwork(phoneme_embedding_dim, phoneme_embedding_dim, filter_channels, kernel_size, p_dropout=p_dropout))
            self.norm_layers_2.append(torch.nn.LayerNorm(phoneme_embedding_dim))

    def forward(self, x, x_mask):
        attn_mask = x_mask.unsqueeze(2) * x_mask.unsqueeze(-1)
        x = x * x_mask
        for i in range(self.n_layers):
            y = self.attention_layers[i](x, x, attn_mask)
            y = self.drop(y)
            x = self.norm_layers_1[i]((x + y).transpose(1, -1)).transpose(1, -1)

            y = self.ffn_layers[i](x, x_mask)
            y = self.drop(y)
            x = self.norm_layers_2[i]((x + y).transpose(1, -1)).transpose(1, -1)
        x = x * x_mask
        return x

#transformerに似た構造のモジュールを用い、音素の列をencodeする
class TextEncoder(nn.Module):
    def __init__(self, 
        n_phoneme,#音素の種類数
        phoneme_embedding_dim = 192,#各音素の埋め込み先のベクトルの大きさ
        out_channels = 192,#出力するmとlogsの次元数
        n_heads = 2,#self.encoder内の、transformerに似た構造のモジュールで使われている、MultiHeadAttentionのhead数
        n_layers = 6,#self.encoder内の、transformerに似た構造のモジュールをいくつ重ねるか
        kernel_size = 3,#self.encoder内の、transformerに似た構造のモジュールにあるFeedForwardNetworkのカーネルサイズ
        filter_channels = 768,#self.encoder内の、transformerに似た構造のモジュールにあるFeedForwardNetworkの隠れ層のチャネル数
        p_dropout = 0.1):#self.encoderの学習時に適用するドロップアウトの比率
        super().__init__()

        self.n_phoneme = n_phoneme#音素の種類数
        self.phoneme_embedding_dim = phoneme_embedding_dim#各音素の埋め込み先のベクトルの大きさ
        self.out_channels = out_channels#出力するmとlogsの次元数
        self.n_heads = n_heads#self.encoder内の、transformerに似た構造のモジュールで使われている、MultiHeadAttentionのhead数
        self.n_layers = n_layers#self.encoder内の、transformerに似た構造のモジュールをいくつ重ねるか
        self.kernel_size = kernel_size#self.encoder内の、transformerに似た構造のモジュールにあるFeedForwardNetworkのカーネルサイズ
        self.filter_channels = filter_channels#self.encoder内の、transformerに似た構造のモジュールにあるFeedForwardNetworkの隠れ層のチャネル数
        self.p_dropout = p_dropout#self.encoderの学習時に適用するドロップアウトの比率

        self.emb = nn.Embedding(self.n_phoneme, self.phoneme_embedding_dim)
        nn.init.normal_(self.emb.weight, 0.0, self.phoneme_embedding_dim**-0.5)

        #Transformerに似た、AttentionとFeedForwardNetworkからなるEncoder
        self.encoder = Encoder(
            self.phoneme_embedding_dim,
            self.filter_channels,
            self.n_heads,
            self.n_layers,
            self.kernel_size,
            self.p_dropout
        )

        self.projection = nn.Conv1d(self.phoneme_embedding_dim, self.out_channels * 2, 1)

    def forward(self, text_padded, text_lengths):
        text_padded_embedded = self.emb(text_padded) * math.sqrt(self.phoneme_embedding_dim)
        text_padded_embedded = torch.transpose(text_padded_embedded, 1, -1)
        #マスクの作成
        max_text_length = text_padded_embedded.size(2)
        progression = torch.arange(max_text_length, dtype=text_lengths.dtype, device=text_lengths.device)
        text_mask = (progression.unsqueeze(0) < text_lengths.unsqueeze(1))
        text_mask = torch.unsqueeze(text_mask, 1).to(text_padded_embedded.dtype)

        text_encoded = self.encoder(text_padded_embedded * text_mask, text_mask)

        stats = self.projection(text_encoded) * text_mask
        m, logs = torch.split(stats, self.out_channels, dim=1)
        return text_encoded, m, logs, text_mask
