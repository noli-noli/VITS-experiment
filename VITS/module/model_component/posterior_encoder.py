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

from .wn import WN

#linear spectrogramを入力にとりEncodeを実行、zを出力するモデル
class PosteriorEncoder(nn.Module):
    def __init__(self,
        speaker_id_embedding_dim,#話者idの埋め込み先のベクトルの大きさ
        in_spec_channels = 513,#入力する線形スペクトログラムの縦軸(周波数)の次元
        out_z_channels = 192,#出力するzのchannel数
        phoneme_embedding_dim = 192,#TextEncoderで作成した、埋め込み済み音素のベクトルの大きさ
        kernel_size = 5,#WN内のconv1dのカーネルサイズ
        dilation_rate = 1,#WN内のconv1dのdilationを決めるための数値
        n_resblocks = 16,#WN内で、ResidualBlockをいくつ重ねるか
        ):
        super(PosteriorEncoder, self).__init__()

        self.speaker_id_embedding_dim = speaker_id_embedding_dim#話者idの埋め込み先のベクトルの大きさ
        self.in_spec_channels = in_spec_channels#入力する線形スペクトログラムの縦軸(周波数)の次元
        self.out_z_channels = out_z_channels#出力するzのchannel数
        self.phoneme_embedding_dim = phoneme_embedding_dim#TextEncoderで作成した、埋め込み済み音素のベクトルの大きさ
        self.kernel_size = kernel_size#WN内のconv1dのカーネルサイズ
        self.dilation_rate = dilation_rate#WN内のconv1dのdilationを決めるための数値
        self.n_resblocks = n_resblocks#WN内で、ResidualBlockをいくつ重ねるか

        #入力スペクトログラムに対し前処理を行うネットワーク
        self.preprocess = nn.Conv1d(self.in_spec_channels, self.phoneme_embedding_dim, 1)
        #WNを用いて特徴量の抽出を行う　WNの詳細はwn.py参照
        self.wn = WN(self.phoneme_embedding_dim, self.kernel_size, self.dilation_rate, self.n_resblocks, speaker_id_embedding_dim=self.speaker_id_embedding_dim)
        #ガウス分布の平均と分散を生成するネットワーク
        self.projection = nn.Conv1d(self.phoneme_embedding_dim, self.out_z_channels * 2, 1)

    def forward(self, spectrogram, spectrogram_lengths, speaker_id_embedded):
        #maskの生成
        max_length = spectrogram.size(2)
        progression = torch.arange(max_length, dtype=spectrogram_lengths.dtype, device=spectrogram_lengths.device)
        spectrogram_mask = (progression.unsqueeze(0) < spectrogram_lengths.unsqueeze(1))
        spectrogram_mask = torch.unsqueeze(spectrogram_mask, 1).to(spectrogram.dtype)
        #入力スペクトログラムに対しConvを用いて前処理を行う
        x = self.preprocess(spectrogram) * spectrogram_mask
        #WNを用いて特徴量の抽出を行う
        x = self.wn(x, spectrogram_mask, speaker_id_embedded=speaker_id_embedded)
        #出力された特徴マップをもとに統計量を生成
        statistics = self.projection(x) * spectrogram_mask
        gauss_mean, gauss_log_variance = torch.split(statistics, self.out_z_channels, dim=1)
        #平均gauss_mean, 分散exp(gauss_log_variance)の正規分布から値をサンプリング
        z = (gauss_mean + torch.randn_like(gauss_mean) * torch.exp(gauss_log_variance)) * spectrogram_mask
        return z, gauss_mean, gauss_log_variance, spectrogram_mask