from functools import partial
import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as pyd
from einops import rearrange

from ..utils.masking import TriangularCausalMask, ProbMask
from .encoder import Encoder, EncoderLayer, VariableDownsample, Normalization
from .decoder import Decoder, DecoderLayer
from .attn import (
    FullAttention,
    ProbAttention,
    AttentionLayer,
    LocalAttentionLayer,
    PerformerAttention,
    BenchmarkAttention,
    NystromSelfAttention,
)
from .embed import Embedding

warnings.filterwarnings("ignore", category=UserWarning)


class Spacetimeformer(nn.Module):
    def __init__(
        self,
        d_yc: int = 1,
        d_yt: int = 1,
        d_x: int = 4,
        start_token_len: int = 64,
        attn_factor: int = 5,
        d_model: int = 512,
        n_heads: int = 8,
        e_layers: int = 2,
        d_layers: int = 2,
        d_ff: int = 512,
        time_emb_dim: int = 6,
        dropout_emb: float = 0.05,
        dropout_token: float = 0.05,
        dropout_attn_out: float = 0.05,
        dropout_ff: float = 0.05,
        dropout_qkv: float = 0.05,
        global_self_attn: str = "performer",
        local_self_attn: str = "none",
        global_cross_attn: str = "performer",
        local_cross_attn: str = "none",
        performer_attn_kernel: str = "relu",
        performer_redraw_interval: int = 250,
        embed_method: str = "spatio-temporal",
        activation: str = "gelu",
        post_norm: bool = True,
        norm: str = "layer",
        initial_downsample_convs: int = 0,
        intermediate_downsample_convs: int = 0,
        device=torch.device("cuda:0"),
        null_value: float = None,
        out_dim: int = None,
        verbose: bool = True,
    ):
        super().__init__()
        if e_layers:
            assert intermediate_downsample_convs <= e_layers - 1
        if embed_method == "temporal":
            assert (
                local_self_attn == "none"
            ), "local attention not compatible with Temporal-only embedding"
            assert (
                local_cross_attn == "none"
            ), "Local Attention not compatible with Temporal-only embedding"

        self.start_token_len = start_token_len
        self.embed_method = embed_method

        # Embedding
        self.enc_embedding = Embedding(
            d_y=d_yc,
            d_x=d_x,
            d_model=d_model,
            time_emb_dim=time_emb_dim,
            downsample_convs=initial_downsample_convs,
            method=embed_method,
            null_value=null_value,
            is_encoder=True,
        )

        self.dec_embedding = Embedding(
            d_y=d_yt,
            d_x=d_x,
            d_model=d_model,
            time_emb_dim=time_emb_dim,
            downsample_convs=initial_downsample_convs,
            method=embed_method,
            start_token_len=start_token_len,
            null_value=null_value,
            is_encoder=False,
        )

        # Select Attention Mechanisms
        attn_kwargs = lambda _is_encoder: {
            "d_model": d_model,
            "n_heads": n_heads,
            "dropout_qkv": dropout_qkv,
            "d_y": d_yc if _is_encoder else d_yt,
            "dropout_attn_out": dropout_attn_out,
            "attn_factor": attn_factor,
            "performer_attn_kernel": performer_attn_kernel,
            "performer_redraw_interval": performer_redraw_interval,
        }

        self.encoder = Encoder(
            attn_layers=[
                EncoderLayer(
                    global_attention=self._global_attn_switch(
                        global_self_attn, **attn_kwargs(True)
                    ),
                    local_attention=self._local_attn_switch(
                        local_self_attn, **attn_kwargs(True)
                    ),
                    d_model=d_model,
                    d_ff=d_ff,
                    dropout_ff=dropout_ff,
                    activation=activation,
                    post_norm=post_norm,
                    norm=norm,
                )
                for l in range(e_layers)
            ],
            conv_layers=[
                VariableDownsample(d_y=d_yc, d_model=d_model)
                for l in range(intermediate_downsample_convs)
            ],
            norm_layer=Normalization(method=norm, d_model=d_model)
            if not post_norm
            else None,
            emb_dropout=dropout_emb,
            data_dropout=dropout_token,
        )

        # Decoder
        self.decoder = Decoder(
            layers=[
                DecoderLayer(
                    global_self_attention=self._global_attn_switch(
                        global_self_attn, **attn_kwargs(False)
                    ),
                    local_self_attention=self._local_attn_switch(
                        local_self_attn, **attn_kwargs(False)
                    ),
                    global_cross_attention=self._global_attn_switch(
                        global_cross_attn, **attn_kwargs(False)
                    ),
                    local_cross_attention=self._local_attn_switch(
                        local_cross_attn, **attn_kwargs(False)
                    ),
                    d_model=d_model,
                    d_ff=d_ff,
                    dropout_ff=dropout_ff,
                    activation=activation,
                    post_norm=post_norm,
                    norm=norm,
                )
                for l in range(d_layers)
            ],
            norm_layer=Normalization(method=norm, d_model=d_model)
            if not post_norm
            else None,
            emb_dropout=dropout_emb,
            data_dropout=dropout_token,
        )

        qprint = lambda _msg_: print(_msg_) if verbose else None
        qprint(f"GlobalSelfAttn: {self.decoder.layers[0].global_self_attention}")
        qprint(f"GlobalCrossAttn: {self.decoder.layers[0].global_cross_attention}")
        qprint(f"LocalSelfAttn: {self.decoder.layers[0].local_self_attention}")
        qprint(f"LocalCrossAttn: {self.decoder.layers[0].local_cross_attention}")
        qprint(f"Using Embedding: {embed_method}")
        qprint(f"Time Emb Dim: {time_emb_dim}")
        qprint(f"Space Embedding: {self.enc_embedding.SPACE}")
        qprint(f"Time Embedding: {self.enc_embedding.TIME}")
        qprint(f"Val Embedding: {self.enc_embedding.VAL}")
        qprint(f"Given Embedding: {self.enc_embedding.GIVEN}")

        if not out_dim:
            out_dim = 1 if self.embed_method == "spatio-temporal" else d_yt
        # account for mean, std output
        out_dim *= 2
        self.forecaster = nn.Linear(d_model, out_dim, bias=True)
        self.classifier = nn.Linear(d_model, d_yc, bias=True)

        self.d_yt = d_yt

    def _fold_spatio_temporal(self, dec_out):
        dec_out = dec_out.chunk(self.d_yt, dim=1)
        means = []
        log_stds = []
        for y in dec_out:
            mean, log_std = y.chunk(2, dim=-1)
            means.append(mean)
            log_stds.append(log_std)
        means = torch.cat(means, dim=-1)[:, self.start_token_len :, :]
        log_stds = torch.cat(log_stds, dim=-1)[:, self.start_token_len :, :]
        return means, log_stds

    def _fold_spatio_temporal2(self, dec_out):
        means, log_stds = rearrange(
            dec_out, "batch (dy len) mean_std -> mean_std batch len dy", dy=self.d_yt
        )[..., self.start_token_len :, :]
        return means, log_stds

    def forward(
        self,
        x_enc,
        x_mark_enc,
        x_dec,
        x_mark_dec,
        enc_self_mask=None,
        dec_self_mask=None,
        dec_enc_mask=None,
        output_attention=False,
    ):
        batch_size = x_enc.shape[0]

        enc_vt_emb, enc_s_emb, enc_var_idxs = self.enc_embedding(y=x_enc, x=x_mark_enc)
        enc_out, enc_self_attns = self.encoder(
            val_time_emb=enc_vt_emb,
            space_emb=enc_s_emb,
            attn_mask=enc_self_mask,
            output_attn=output_attention,
        )
        dec_vt_emb, dec_s_emb, _ = self.dec_embedding(y=x_dec, x=x_mark_dec)
        dec_out, dec_cross_attns = self.decoder(
            val_time_emb=dec_vt_emb,
            space_emb=dec_s_emb,
            cross=enc_out,
            x_mask=dec_self_mask,
            cross_mask=dec_enc_mask,
            output_cross_attn=output_attention,
        )

        forecast_out = self.forecaster(dec_out)

        if self.embed_method == "spatio-temporal":
            # means, log_stds = self._fold_spatio_temporal(forecast_out)
            means, log_stds = self._fold_spatio_temporal2(forecast_out)
        else:
            forecast_out = forecast_out[:, self.start_token_len :, :]
            means, log_stds = forecast_out.chunk(2, dim=-1)

        # stabilization trick from Neural Processes papers
        stds = 1e-3 + (1.0 - 1e-3) * torch.log(1.0 + log_stds.exp())

        pred_distrib = pyd.Normal(means, stds)

        if enc_var_idxs is not None:
            # note that detaching the input like this means the transformer layers
            # are not optimizing for classification accuracy (but the linear classifier
            # layer still is). This is just a test to see how much unique spatial info
            # remains in the output after all the global attention layers.
            classifier_enc_out = self.classifier(enc_out.detach())
        else:
            classifier_enc_out, enc_var_idxs = None, None

        return (
            pred_distrib,
            (classifier_enc_out, enc_var_idxs),
            (enc_self_attns, dec_cross_attns),
        )

    def _global_attn_switch(
        self,
        global_attn_str: str,
        d_model: int,
        n_heads: int,
        d_y: int,
        dropout_qkv: float,
        dropout_attn_out: float,
        attn_factor: int,
        performer_attn_kernel: str,
        performer_redraw_interval: int,
    ):

        if global_attn_str == "full":
            # standard full (n^2) attention
            Attn = partial(
                AttentionLayer,
                attention=partial(FullAttention, attention_dropout=dropout_attn_out),
                d_model=d_model,
                n_heads=n_heads,
                mix=False,
                dropout_qkv=dropout_qkv,
            )
        elif global_attn_str == "prob":
            # Informer-style Prob self Full cross attention
            Attn = partial(
                AttentionLayer,
                attention=partial(
                    ProbAttention,
                    factor=attn_factor,
                    attention_dropout=dropout_attn_out,
                ),
                d_model=d_model,
                n_heads=n_heads,
                mix=False,
                dropout_qkv=dropout_qkv,
            )
        elif global_attn_str == "performer":
            # Performer Linear Attention
            Attn = partial(
                AttentionLayer,
                attention=partial(
                    PerformerAttention,
                    dim_heads=(d_model // n_heads),
                    kernel=performer_attn_kernel,
                    feature_redraw_interval=performer_redraw_interval,
                ),
                d_model=d_model,
                n_heads=n_heads,
                mix=False,
                dropout_qkv=dropout_qkv,
            )
        elif global_attn_str == "nystromformer":
            Attn = partial(
                NystromSelfAttention,
                d_model=d_model,
                n_heads=n_heads,
                attention_dropout=dropout_attn_out,
            )
        elif global_attn_str == "benchmark":
            Attn = BenchmarkAttention
        elif global_attn_str == "none":
            Attn = lambda: None
        else:
            raise ValueError(f"Unrecognized Global Attention '{global_attn_str}'")
        return Attn()

    def _local_attn_switch(
        self,
        local_attn_str: str,
        d_y: int,
        d_model: int,
        n_heads: int,
        dropout_qkv: float,
        dropout_attn_out: float,
        attn_factor: int,
        performer_attn_kernel: str,
        performer_redraw_interval: int,
    ):

        if local_attn_str == "prob":
            # Prob Local Attention
            Attn = partial(
                LocalAttentionLayer,
                attention=partial(
                    ProbAttention,
                    factor=attn_factor,
                    attention_dropout=dropout_attn_out,
                ),
                d_model=d_model,
                n_heads=n_heads,
                dropout_qkv=dropout_qkv,
                d_y=d_y,
            )
        elif local_attn_str == "full":
            Attn = partial(
                LocalAttentionLayer,
                attention=partial(FullAttention, attention_dropout=dropout_attn_out),
                d_model=d_model,
                n_heads=n_heads,
                dropout_qkv=dropout_qkv,
                d_y=d_y,
            )
        elif local_attn_str == "performer":
            # Performer Local Attention
            Attn = partial(
                LocalAttentionLayer,
                attention=partial(
                    PerformerAttention,
                    dim_heads=(d_model // n_heads),
                    kernel=performer_attn_kernel,
                    feature_redraw_interval=performer_redraw_interval,
                ),
                d_model=d_model,
                n_heads=n_heads,
                dropout_qkv=dropout_qkv,
                d_y=d_y,
            )
        elif local_attn_str == "benchmark":
            Attn = BenchmarkAttention
        elif local_attn_str == "none":
            # Ablation of Local Attention
            Attn = lambda: None
        else:
            raise ValueError(f"Unrecognized Local Attention '{local_attn_str}'")
        return Attn()
