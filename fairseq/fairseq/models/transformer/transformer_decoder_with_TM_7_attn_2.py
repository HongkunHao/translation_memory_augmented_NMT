# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import Any, Dict, List, Optional

import torch
import torch.nn as nn
from fairseq import utils
from fairseq.distributed import fsdp_wrap
from fairseq.models import FairseqIncrementalDecoder
from fairseq.models.transformer import TransformerConfig
from fairseq.modules import (
    AdaptiveSoftmax,
    BaseLayer,
    FairseqDropout,
    LayerDropModuleList,
    LayerNorm,
    PositionalEmbedding,
    SinusoidalPositionalEmbedding,
)
from fairseq.modules import transformer_layer
from fairseq.modules.checkpoint_activations import checkpoint_wrapper
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_
from torch import Tensor

from fairseq.models.transformer.transformer_decoder import Linear
from fairseq.modules import MultiheadAttention
from fairseq.modules import MultiheadAttention_new
from fairseq.modules.quant_noise import quant_noise
import torch.nn.functional as F
from typing import Dict, List, Optional, Tuple

import logging
import os
import sys
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)
logger = logging.getLogger("transformer_decoder_with_TM")


# rewrite name for backward compatibility in `make_generation_fast_`
def module_name_fordropout(module_name: str) -> str:
    if module_name == "TransformerDecoderBaseWithTM":
        return "TransformerDecoderWithTM"
    else:
        return module_name


class TransformerDecoderBaseWithTM(FairseqIncrementalDecoder):
    """
    Transformer decoder consisting of *cfg.decoder.layers* layers. Each layer
    is a :class:`TransformerDecoderLayer`.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): decoding dictionary
        embed_tokens (torch.nn.Embedding): output embedding
        no_encoder_attn (bool, optional): whether to attend to encoder outputs
            (default: False).
    """

    def __init__(
        self,
        cfg,
        dictionary,
        embed_tokens,
        no_encoder_attn=False,
        output_projection=None,
    ):
        self.cfg = cfg
        super().__init__(dictionary)
        self.register_buffer("version", torch.Tensor([3]))
        self._future_mask = torch.empty(0)

        self.dropout_module = FairseqDropout(
            cfg.dropout, module_name=module_name_fordropout(self.__class__.__name__)
        )
        self.decoder_layerdrop = cfg.decoder.layerdrop
        self.share_input_output_embed = cfg.share_decoder_input_output_embed

        input_embed_dim = embed_tokens.embedding_dim
        embed_dim = cfg.decoder.embed_dim
        self.embed_dim = embed_dim
        self.output_embed_dim = cfg.decoder.output_dim

        self.padding_idx = embed_tokens.padding_idx
        self.max_target_positions = cfg.max_target_positions

        self.embed_tokens = embed_tokens

        self.embed_scale = 1.0 if cfg.no_scale_embedding else math.sqrt(embed_dim)

        if not cfg.adaptive_input and cfg.quant_noise.pq > 0:
            self.quant_noise = apply_quant_noise_(
                nn.Linear(embed_dim, embed_dim, bias=False),
                cfg.quant_noise.pq,
                cfg.quant_noise.pq_block_size,
            )
        else:
            self.quant_noise = None

        self.project_in_dim = (
            Linear(input_embed_dim, embed_dim, bias=False)
            if embed_dim != input_embed_dim
            else None
        )
        self.embed_positions = (
            PositionalEmbedding(
                self.max_target_positions,
                embed_dim,
                self.padding_idx,
                learned=cfg.decoder.learned_pos,
            )
            if not cfg.no_token_positional_embeddings
            else None
        )
        if cfg.layernorm_embedding:
            self.layernorm_embedding = LayerNorm(embed_dim, export=cfg.export)
        else:
            self.layernorm_embedding = None

        self.cross_self_attention = cfg.cross_self_attention

        if self.decoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.decoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])
        self.layers.extend(
            [
                self.build_decoder_layer(cfg, no_encoder_attn)
                for _ in range(cfg.decoder.layers)
            ]
        )
        self.num_layers = len(self.layers)

        if cfg.decoder.normalize_before and not cfg.no_decoder_final_norm:
            self.layer_norm = LayerNorm(embed_dim, export=cfg.export)
        else:
            self.layer_norm = None

        self.project_out_dim = (
            Linear(embed_dim, self.output_embed_dim, bias=False)
            if embed_dim != self.output_embed_dim and not cfg.tie_adaptive_weights
            else None
        )

        self.adaptive_softmax = None
        self.output_projection = output_projection
        if self.output_projection is None:
            self.build_output_projection(cfg, dictionary, embed_tokens)

        # self.quant_noise = cfg.quant_noise.pq
        # self.quant_noise_block_size = cfg.quant_noise.pq_block_size
        self.alignment_layer = MultiheadAttention(
            embed_dim, 
            1, 
            kdim=cfg.encoder.embed_dim,
            vdim=cfg.encoder.embed_dim,
            dropout=cfg.attention_dropout,
            encoder_decoder_attention=True,
            q_noise=cfg.quant_noise.pq,
            qn_block_size=cfg.quant_noise.pq_block_size,)
        self.alignment_layer_norm = nn.LayerNorm(embed_dim)

        self.ff_layer_1 = quant_noise(nn.Linear(embed_dim, cfg.decoder.ffn_embed_dim), p=cfg.quant_noise.pq, block_size=cfg.quant_noise.pq_block_size)
        self.ff_layer_2 = quant_noise(nn.Linear(cfg.decoder.ffn_embed_dim, embed_dim), p=cfg.quant_noise.pq, block_size=cfg.quant_noise.pq_block_size)
        self.ff_layer_norm = LayerNorm(self.embed_dim, export=cfg.export)
        
        self.activation_fn = utils.get_activation_fn(activation=cfg.activation_fn)
        activation_dropout_p = cfg.activation_dropout
        if activation_dropout_p == 0:
            # for backwards compatibility with models that use cfg.relu_dropout
            activation_dropout_p = cfg.relu_dropout or 0
        self.activation_dropout_module = FairseqDropout(
            float(activation_dropout_p), module_name=self.__class__.__name__
        )
        self.dropout_module = FairseqDropout(
            cfg.dropout, module_name=self.__class__.__name__
        )
        self.normalize_before = cfg.decoder.normalize_before

        self.diverter = nn.Linear(2*embed_dim, 2)

        self.configs = dict()
        config_file_path = '/apdcephfs/share_916081/hongkunhao/JRC_run/fairseq_bm25_ende_sents_17_da/task_file_lr00007_bs4096_0_max0_dr01_gpus8_17/config.txt'
        if os.path.exists(config_file_path):
            config_file = open(r'/apdcephfs/share_916081/hongkunhao/JRC_run/fairseq_bm25_ende_sents_17_da/task_file_lr00007_bs4096_0_max0_dr01_gpus8_17/config.txt', "r")
            for line in config_file.readlines():
                if not line.strip():
                    continue
                key, value = line.strip().split("=")
                self.configs[key] = value
            logger.info("having parseing the config .")
            logger.info(self.configs)
        if 'with_TM_ensemble' in self.configs:
            # self.with_TM_ensemble = bool(self.configs['with_TM_ensemble'])
            assert self.configs['with_TM_ensemble'].lower() == 'true' or self.configs['with_TM_ensemble'].lower() == 'false'
            self.with_TM_ensemble = True if self.configs['with_TM_ensemble'].lower() == 'true' else False
        else:
            self.with_TM_ensemble = False
        if 'TM_num' in self.configs:
            self.TM_num = int(self.configs['TM_num'])
        else:
            self.TM_num = 5
        if 'with_TM_learnable_p' in self.configs:
            # self.with_TM_learnable_p = bool(self.configs['with_TM_learnable_p'])
            assert self.configs['with_TM_learnable_p'].lower() == 'true' or self.configs['with_TM_learnable_p'].lower() == 'false'
            self.with_TM_learnable_p = True if self.configs['with_TM_learnable_p'].lower() == 'true' else False
        else:
            self.with_TM_learnable_p = False
        if 'with_TM_learnable_p_model' in self.configs:
            self.with_TM_learnable_p_model = self.configs['with_TM_learnable_p_model']
        else:
            self.with_TM_learnable_p_model = None

        if self.with_TM_ensemble and self.with_TM_learnable_p:
            assert self.with_TM_learnable_p_model != None
            if self.with_TM_learnable_p_model == 'unscalable_linear':
                self.learnable_p_fc1 = nn.Linear(self.embed_dim * (self.TM_num + 1), self.embed_dim * 4)
                # self.learnable_p_dropout = nn.Dropout(p=0.5)
                self.learnable_p_fc2 = nn.Linear(self.embed_dim * 4, self.TM_num)
            elif self.with_TM_learnable_p_model == 'attention':
                self.learnable_p_attn = MultiheadAttention(
                    embed_dim, 
                    1, 
                    kdim=cfg.encoder.embed_dim,
                    vdim=cfg.encoder.embed_dim,
                    dropout=cfg.attention_dropout,
                    encoder_decoder_attention=True,
                    q_noise=cfg.quant_noise.pq,
                    qn_block_size=cfg.quant_noise.pq_block_size,)
            elif self.with_TM_learnable_p_model == 'linear':
                self.learnable_p_fc1 = nn.Linear(self.embed_dim * 2, self.embed_dim * 4)
                # self.learnable_p_dropout = nn.Dropout(p=0.5)
                self.learnable_p_fc2 = nn.Linear(self.embed_dim * 4, 1)
                # self.learnable_p_layer_norm = nn.LayerNorm(embed_dim)
            elif self.with_TM_learnable_p_model == 'attention_large':
                self.learnable_p_attn = MultiheadAttention_new(
                    embed_dim, 
                    1, 
                    inter_dim=self.embed_dim * 8,
                    kdim=cfg.encoder.embed_dim,
                    vdim=cfg.encoder.embed_dim,
                    dropout=cfg.attention_dropout,
                    encoder_decoder_attention=True,
                    q_noise=cfg.quant_noise.pq,
                    qn_block_size=cfg.quant_noise.pq_block_size,)
            elif self.with_TM_learnable_p_model == 'linear_large':
                self.learnable_p_fc1 = nn.Linear(self.embed_dim * 2, self.embed_dim * 12)
                # self.learnable_p_dropout = nn.Dropout(p=0.5)
                self.learnable_p_fc2 = nn.Linear(self.embed_dim * 12, 1)
                # self.learnable_p_layer_norm = nn.LayerNorm(embed_dim)
            elif self.with_TM_learnable_p_model == 'attention_2':
                self.learnable_p_attn = MultiheadAttention(
                    embed_dim=embed_dim, 
                    num_heads=8, 
                    kdim=cfg.encoder.embed_dim,
                    vdim=cfg.encoder.embed_dim,
                    dropout=cfg.attention_dropout,
                    encoder_decoder_attention=True,
                    q_noise=cfg.quant_noise.pq,
                    qn_block_size=cfg.quant_noise.pq_block_size,)
                self.learnable_p_fc1 = nn.Linear(self.embed_dim * 3, self.embed_dim * 4)
                self.learnable_p_fc2 = nn.Linear(self.embed_dim * 4, 1)

    def build_output_projection(self, cfg, dictionary, embed_tokens):
        if cfg.adaptive_softmax_cutoff is not None:
            self.adaptive_softmax = AdaptiveSoftmax(
                len(dictionary),
                self.output_embed_dim,
                utils.eval_str_list(cfg.adaptive_softmax_cutoff, type=int),
                dropout=cfg.adaptive_softmax_dropout,
                adaptive_inputs=embed_tokens if cfg.tie_adaptive_weights else None,
                factor=cfg.adaptive_softmax_factor,
                tie_proj=cfg.tie_adaptive_proj,
            )
        elif self.share_input_output_embed:
            self.output_projection = nn.Linear(
                self.embed_tokens.weight.shape[1],
                self.embed_tokens.weight.shape[0],
                bias=False,
            )
            self.output_projection.weight = self.embed_tokens.weight
        else:
            self.output_projection = nn.Linear(
                self.output_embed_dim, len(dictionary), bias=False
            )
            nn.init.normal_(
                self.output_projection.weight, mean=0, std=self.output_embed_dim ** -0.5
            )
        num_base_layers = cfg.base_layers
        for i in range(num_base_layers):
            self.layers.insert(
                ((i + 1) * cfg.decoder.layers) // (num_base_layers + 1),
                BaseLayer(cfg),
            )

    def build_decoder_layer(self, cfg, no_encoder_attn=False):
        layer = transformer_layer.TransformerDecoderLayerBase(cfg, no_encoder_attn)
        checkpoint = cfg.checkpoint_activations
        if checkpoint:
            offload_to_cpu = cfg.offload_activations
            layer = checkpoint_wrapper(layer, offload_to_cpu=offload_to_cpu)
        # if we are checkpointing, enforce that FSDP always wraps the
        # checkpointed layer, regardless of layer size
        min_params_to_wrap = cfg.min_params_to_wrap if not checkpoint else 0
        layer = fsdp_wrap(layer, min_num_params=min_params_to_wrap)
        return layer

    def forward(
        self,
        prev_output_tokens,
        encoder_out: Optional[Dict[str, List[Tensor]]] = None,
        incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
        features_only: bool = False,
        full_context_alignment: bool = False,
        alignment_layer: Optional[int] = None,
        alignment_heads: Optional[int] = None,
        src_lengths: Optional[Any] = None,
        return_all_hiddens: bool = False,
    ):
        """
        Args:
            prev_output_tokens (LongTensor): previous decoder outputs of shape
                `(batch, tgt_len)`, for teacher forcing
            encoder_out (optional): output from the encoder, used for
                encoder-side attention, should be of size T x B x C
            incremental_state (dict): dictionary used for storing state during
                :ref:`Incremental decoding`
            features_only (bool, optional): only return features without
                applying output layer (default: False).
            full_context_alignment (bool, optional): don't apply
                auto-regressive mask to self-attention (default: False).

        Returns:
            tuple:
                - the decoder's output of shape `(batch, tgt_len, vocab)`
                - a dictionary with any model-specific outputs
        """
        # for name, parameters in self.named_parameters():
        #   print(name, ': ', parameters.size())
        '''
        learnable_p_fc1.weight :  torch.Size([2048, 3072])
        learnable_p_fc1.bias :  torch.Size([2048])
        learnable_p_fc2.weight :  torch.Size([5, 2048])
        learnable_p_fc2.bias :  torch.Size([5])
        '''
        # with_TM_ensemble = True
        # with_TM_ensemble = False
        with_TM_ensemble = self.with_TM_ensemble
        TM_num = self.TM_num
        with_TM_learnable_p = self.with_TM_learnable_p
        # with_learnable_p = True
        # with_TM_learnable_p = False
        # print("1")
        x, extra = self.extract_features(
            prev_output_tokens,
            encoder_out=encoder_out,
            incremental_state=incremental_state,
            full_context_alignment=full_context_alignment,
            alignment_layer=alignment_layer,
            alignment_heads=alignment_heads,
        )  # X: B x T x C
        # print("2")
        # print(with_TM_ensemble)
        # print(TM_num)
        # print(x.dtype)
        # lprobs = self.output_layer(x)
        # lprobs = F.log_softmax(lprobs, dim=-1, dtype=torch.float32)
        # x = F.softmax(self.output_layer(x), -1)
        # lprobs = torch.log(x + 1e-12)
        # lprobs = self.output_layer(x)
        
        # if not features_only:
        #     x = self.output_layer(x)
        if not with_TM_ensemble:
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            attn, alignment_weight = self.alignment_layer(
                x, memory, memory,
                key_padding_mask=memory_encoder_padding_mask,
                need_weights=True,
                need_head_weights=False,)  # TBC
            attn_normalized = self.alignment_layer_norm(attn)
            gates = F.softmax(self.diverter(torch.cat([x, attn_normalized], -1)), -1, dtype=torch.float32)
            gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

            x = self.alignment_layer_norm(x + attn)
            residual = x
            if self.normalize_before:
                x = self.ff_layer_norm(x)
            x = self.activation_fn(self.ff_layer_1(x))
            x = self.activation_dropout_module(x)
            x = self.ff_layer_2(x)
            x = self.dropout_module(x)
            x = x+residual
            if not self.normalize_before:
                x = self.ff_layer_norm(x)
            
            seq_len, bsz, _ = x.size()
            probs = gen_gate * F.softmax(self.output_layer(x), -1, dtype=torch.float32)  # TBC

            copy_seq = encoder_out["TMs"][0]
            #copy_seq: 5src_len x bsz
            #copy_gate: tgt_len x bsz  
            alignment_weight = alignment_weight.transpose(0,1)
            #alignment_weight: tgt_len x bsz x src_len
            #index: tgt_len x bsz
            index = copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
            copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
            copy_probs = copy_probs.float()
            # -> tgt_len x bsz x src_len
            probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
            lprobs = torch.log(probs + 1e-12)
            lprobs = lprobs.transpose(0, 1)  # BTC
            # print("not ensemble")
            return lprobs, extra
        elif with_TM_ensemble and encoder_out["TMs_similarity"][0] is not None:
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 

            lprobs = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                
                '''
                cur_lprobs = torch.log(probs + 1e-12)
                cur_lprobs = cur_lprobs.transpose(0, 1)  # BTC
                # cur_lprobs = cur_lprobs.unsqueeze(3)
                lprobs.append(cur_lprobs)
                # print(cur_lprobs.size())
                
                '''
                probs = probs.transpose(0, 1)  # BTC
                cur_TMs_similarity = encoder_out["TMs_similarity"][0][:, cur_idx]
                #print(encoder_out["TMs_similarity"][0])
                #print(encoder_out["TMs"][0])
                # print(encoder_out["src_tokens"][0])
                #print(encoder_out["src_lengths"][0])
                #print(cur_copy_seq)
                #print(cur_TMs_similarity)
                cur_TMs_similarity = cur_TMs_similarity[:, None, None]
                probs = probs * cur_TMs_similarity
                lprobs.append(probs)
            final_lprobs = torch.stack(lprobs, dim=0)
            final_lprobs = torch.sum(final_lprobs, dim=0)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            return final_lprobs, extra
        elif with_TM_ensemble and with_TM_learnable_p and self.with_TM_learnable_p_model=='unscalable_linear':
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 
            
            lprobs = []
            attn_all = []
            alignment_weight_all = []
            attn_normalized_all = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                attn_all.append(attn)  # TM_num * TBC
                alignment_weight_all.append(alignment_weight)
                attn_normalized_all.append(attn_normalized)
            
            input_for_learnable_p = torch.cat([x] + attn_all, dim=-1)
            input_for_learnable_p = self.activation_fn(self.learnable_p_fc1(input_for_learnable_p))
            input_for_learnable_p = self.activation_dropout_module(input_for_learnable_p)
            input_for_learnable_p = self.learnable_p_fc2(input_for_learnable_p)
            input_for_learnable_p = self.dropout_module(input_for_learnable_p)  # T x B x TM_num
            input_for_learnable_p = input_for_learnable_p.transpose(0, 1)  # B x T x TM_num
            input_for_learnable_p = F.softmax(input_for_learnable_p, -1, dtype=torch.float32)

            for cur_idx in range(TM_num):
                attn = attn_all[cur_idx]
                alignment_weight = alignment_weight_all[cur_idx]
                attn_normalized = attn_normalized_all[cur_idx]

                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                probs = probs.transpose(0, 1)  # BTC
                cur_TMs_similarity = input_for_learnable_p[:, :, cur_idx]
                cur_TMs_similarity = cur_TMs_similarity[:, :, None]
                # print(probs.shape)
                # print(cur_TMs_similarity.shape)
                # exit()
                probs = probs * cur_TMs_similarity
                lprobs.append(probs)
            final_lprobs = torch.stack(lprobs, dim=0)
            final_lprobs = torch.sum(final_lprobs, dim=0)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            return final_lprobs, extra
        elif with_TM_ensemble and with_TM_learnable_p and (self.with_TM_learnable_p_model=='attention' or self.with_TM_learnable_p_model=='attention_large'):
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 
            
            lprobs = []
            attn_all = []
            alignment_weight_all = []
            attn_normalized_all = []
            # learnable_p_attn_norm_all = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                attn_all.append(attn)  # TM_num * TBC
                alignment_weight_all.append(alignment_weight)
                attn_normalized_all.append(attn_normalized)
                # learnable_p_attn_norm = self.learnable_p_layer_norm(attn)
                # learnable_p_attn_norm_all.append(learnable_p_attn_norm)
            
            learnable_p_all = []
            seq_len, bsz, _ = x.size()
            clone_attn_normalized_all = torch.stack(attn_normalized_all, dim=0)  # TM_num x T x B x C
            clone_attn_normalized_all = clone_attn_normalized_all.transpose(0,1)  # T x TM_num x B x C
            for T_idx in range(seq_len):
                cur_x = x[T_idx, :, :]
                cur_x = cur_x[None, :, :]  # 1 x B x C
                cur_clone_attn_normalized = clone_attn_normalized_all[T_idx, :, :, :]  # TM_num x B x C
                cur_attn, cur_learnable_p = self.learnable_p_attn(
                    cur_x, cur_clone_attn_normalized, cur_clone_attn_normalized,
                    need_weights=True,
                    need_head_weights=False,)  # cur_learnable_p: B x 1 x TM_num
                learnable_p_all.append(cur_learnable_p)

            input_for_learnable_p = torch.cat(learnable_p_all, dim=1)  # B x T x TM_num

            for cur_idx in range(TM_num):
                attn = attn_all[cur_idx]
                alignment_weight = alignment_weight_all[cur_idx]
                attn_normalized = attn_normalized_all[cur_idx]

                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                
                '''
                cur_lprobs = torch.log(probs + 1e-12)
                cur_lprobs = cur_lprobs.transpose(0, 1)  # BTC
                # cur_lprobs = cur_lprobs.unsqueeze(3)
                lprobs.append(cur_lprobs)
                # print(cur_lprobs.size())
                
                '''
                probs = probs.transpose(0, 1)  # BTC
                cur_TMs_similarity = input_for_learnable_p[:, :, cur_idx]
                cur_TMs_similarity = cur_TMs_similarity[:, :, None]
                # print(probs.shape)
                # print(cur_TMs_similarity.shape)
                # exit()
                probs = probs * cur_TMs_similarity
                lprobs.append(probs)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=-1), dim=-1)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=3), dim=3)
            # final_lprobs = torch.FloatTensor(lprobs)
            final_lprobs = torch.stack(lprobs, dim=0)
            final_lprobs = torch.sum(final_lprobs, dim=0)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            return final_lprobs, extra
        elif with_TM_ensemble and with_TM_learnable_p and (self.with_TM_learnable_p_model=='linear' or self.with_TM_learnable_p_model=='linear_large'):
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 
            
            lprobs = []
            attn_all = []
            alignment_weight_all = []
            attn_normalized_all = []
            # learnable_p_attn_normalized_all = []
            input_for_learnable_p = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                attn_all.append(attn)  # TM_num * TBC
                alignment_weight_all.append(alignment_weight)
                attn_normalized_all.append(attn_normalized)
                
                cur_learnable_p = torch.cat([x, attn_normalized], dim=-1)
                cur_learnable_p = self.activation_fn(self.learnable_p_fc1(cur_learnable_p))
                cur_learnable_p = self.activation_dropout_module(cur_learnable_p)
                cur_learnable_p = self.learnable_p_fc2(cur_learnable_p)  # T x B x 1
                cur_learnable_p = self.dropout_module(cur_learnable_p)
                cur_learnable_p = cur_learnable_p.transpose(0, 1)  # B x T x 1
                input_for_learnable_p.append(cur_learnable_p)
            input_for_learnable_p = torch.cat(input_for_learnable_p, dim=2)
            input_for_learnable_p = F.softmax(input_for_learnable_p, 2, dtype=torch.float32)

            for cur_idx in range(TM_num):
                attn = attn_all[cur_idx]
                alignment_weight = alignment_weight_all[cur_idx]
                attn_normalized = attn_normalized_all[cur_idx]

                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                
                '''
                cur_lprobs = torch.log(probs + 1e-12)
                cur_lprobs = cur_lprobs.transpose(0, 1)  # BTC
                # cur_lprobs = cur_lprobs.unsqueeze(3)
                lprobs.append(cur_lprobs)
                # print(cur_lprobs.size())
                
                '''
                probs = probs.transpose(0, 1)  # BTC
                cur_TMs_similarity = input_for_learnable_p[:, :, cur_idx]
                cur_TMs_similarity = cur_TMs_similarity[:, :, None]
                # print(probs.shape)
                # print(cur_TMs_similarity.shape)
                # exit()
                probs = probs * cur_TMs_similarity
                lprobs.append(probs)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=-1), dim=-1)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=3), dim=3)
            # final_lprobs = torch.FloatTensor(lprobs)
            final_lprobs = torch.stack(lprobs, dim=0)
            final_lprobs = torch.sum(final_lprobs, dim=0)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            return final_lprobs, extra
        elif with_TM_ensemble and with_TM_learnable_p and self.with_TM_learnable_p_model=='attention_2':
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 
            
            lprobs = []
            attn_all = []
            alignment_weight_all = []
            attn_normalized_all = []
            # learnable_p_attn_norm_all = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                attn_all.append(attn)  # TM_num * TBC
                alignment_weight_all.append(alignment_weight)
                attn_normalized_all.append(attn_normalized)
                # learnable_p_attn_norm = self.learnable_p_layer_norm(attn)
                # learnable_p_attn_norm_all.append(learnable_p_attn_norm)
            
            # learnable_p_all = []
            learnable_p_attn_all = []
            seq_len, bsz, _ = x.size()
            clone_attn_normalized_all = torch.stack(attn_normalized_all, dim=0)  # TM_num x T x B x C
            clone_attn_normalized_all = clone_attn_normalized_all.transpose(0,1)  # T x TM_num x B x C
            for T_idx in range(seq_len):
                cur_x = x[T_idx, :, :]
                cur_x = cur_x[None, :, :]  # 1 x B x C
                cur_clone_attn_normalized = clone_attn_normalized_all[T_idx, :, :, :]  # TM_num x B x C
                cur_attn, cur_learnable_p = self.learnable_p_attn(
                    cur_x, cur_clone_attn_normalized, cur_clone_attn_normalized,
                    need_weights=True,
                    need_head_weights=False,)  # cur_learnable_p: B x 1 x TM_num  cur_attn: 1 x B x C
                # learnable_p_all.append(cur_learnable_p)
                learnable_p_attn_all.append(cur_attn)

            # input_for_learnable_p = torch.cat(learnable_p_all, dim=1)  # B x T x TM_num
            learnable_p_attn_all_cat = torch.cat(learnable_p_attn_all, dim=0)  # T x B x C
            input_for_learnable_p = []
            for cur_idx in range(TM_num):
                cur_attn_normalized = attn_normalized_all[cur_idx]  # T B C
                cur_learnable_p = torch.cat([x, cur_attn_normalized, learnable_p_attn_all_cat], dim=-1)
                cur_learnable_p = self.activation_fn(self.learnable_p_fc1(cur_learnable_p))
                cur_learnable_p = self.activation_dropout_module(cur_learnable_p)
                cur_learnable_p = self.learnable_p_fc2(cur_learnable_p)  # T x B x 1
                cur_learnable_p = self.dropout_module(cur_learnable_p)
                cur_learnable_p = cur_learnable_p.transpose(0, 1)  # B x T x 1
                input_for_learnable_p.append(cur_learnable_p)
            input_for_learnable_p = torch.cat(input_for_learnable_p, dim=2)
            input_for_learnable_p = F.softmax(input_for_learnable_p, 2, dtype=torch.float32)  # B x T x TM_num
            # print(input_for_learnable_p)
            # print(input_for_learnable_p.size())
            for cur_idx in range(TM_num):
                attn = attn_all[cur_idx]
                alignment_weight = alignment_weight_all[cur_idx]
                attn_normalized = attn_normalized_all[cur_idx]

                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                
                '''
                cur_lprobs = torch.log(probs + 1e-12)
                cur_lprobs = cur_lprobs.transpose(0, 1)  # BTC
                # cur_lprobs = cur_lprobs.unsqueeze(3)
                lprobs.append(cur_lprobs)
                # print(cur_lprobs.size())
                
                '''
                probs = probs.transpose(0, 1)  # BTC
                cur_TMs_similarity = input_for_learnable_p[:, :, cur_idx]
                cur_TMs_similarity = cur_TMs_similarity[:, :, None]
                # print(probs.shape)
                # print(cur_TMs_similarity.shape)
                # exit()
                probs = probs * cur_TMs_similarity
                lprobs.append(probs)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=-1), dim=-1)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=3), dim=3)
            # final_lprobs = torch.FloatTensor(lprobs)
            final_lprobs = torch.stack(lprobs, dim=0)
            final_lprobs = torch.sum(final_lprobs, dim=0)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            return final_lprobs, extra
        else:
            x = x.transpose(0, 1)  # T B C
            memory = encoder_out["memory_encoder_out"][0]  # T * TM_num x B x C
            memory_encoder_padding_mask = encoder_out["memory_encoder_padding_mask"][0]  # B x T * TM_num
            copy_seq = encoder_out["TMs"][0]  # T * TM_num x B 
            TM_num_memorys = memory.chunk(TM_num, dim=0)  # T x B x C
            TM_num_memory_encoder_padding_masks = memory_encoder_padding_mask.chunk(TM_num, dim=1)  # B x T
            TM_num_copy_seqs = copy_seq.chunk(TM_num, dim=0)  # T x B 

            lprobs = []
            for cur_idx in range(TM_num):
                cur_memory = TM_num_memorys[cur_idx]
                cur_memory_encoder_padding_mask = TM_num_memory_encoder_padding_masks[cur_idx]
                clone_x = x.clone()
                attn, alignment_weight = self.alignment_layer(
                    clone_x, cur_memory, cur_memory,
                    key_padding_mask=cur_memory_encoder_padding_mask,
                    need_weights=True,
                    need_head_weights=False,)  # TBC
                attn_normalized = self.alignment_layer_norm(attn)
                gates = F.softmax(self.diverter(torch.cat([clone_x, attn_normalized], -1)), -1, dtype=torch.float32)
                gen_gate, copy_gate = gates.chunk(2, dim=-1)  # T B

                clone_x = self.alignment_layer_norm(clone_x + attn)
                residual = clone_x
                if self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                clone_x = self.activation_fn(self.ff_layer_1(clone_x))
                clone_x = self.activation_dropout_module(clone_x)
                clone_x = self.ff_layer_2(clone_x)
                clone_x = self.dropout_module(clone_x)
                clone_x = clone_x + residual
                if not self.normalize_before:
                    clone_x = self.ff_layer_norm(clone_x)
                
                seq_len, bsz, _ = clone_x.size()
                probs = gen_gate * F.softmax(self.output_layer(clone_x), -1, dtype=torch.float32)  # TBC

                cur_copy_seq = TM_num_copy_seqs[cur_idx]
                #copy_seq: src_len x bsz
                #copy_gate: tgt_len x bsz  
                alignment_weight = alignment_weight.transpose(0,1)
                #alignment_weight: tgt_len x bsz x src_len
                #index: tgt_len x bsz
                index = cur_copy_seq.transpose(0, 1).contiguous().view(1, bsz, -1).expand(seq_len, -1, -1)
                copy_probs = (copy_gate * alignment_weight).view(seq_len, bsz, -1)
                copy_probs = copy_probs.float()
                # -> tgt_len x bsz x src_len
                probs = probs.scatter_add_(-1, index, copy_probs)  # TBC
                
                '''
                cur_lprobs = torch.log(probs + 1e-12)
                cur_lprobs = cur_lprobs.transpose(0, 1)  # BTC
                # cur_lprobs = cur_lprobs.unsqueeze(3)
                lprobs.append(cur_lprobs)
                # print(cur_lprobs.size())
                
                '''
                lprobs.append(probs)
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=-1), dim=-1)
            final_lprobs = torch.mean(torch.stack(lprobs, dim=3), dim=3)
            final_lprobs = torch.log(final_lprobs + 1e-12)
            final_lprobs = final_lprobs.transpose(0, 1)
            # '''
            # final_lprobs = torch.mean(torch.stack(lprobs, dim=3), dim=3)

            # print(final_lprobs.size())
            #final_lprobs, indexs = torch.max(torch.stack(lprobs, dim=-1), dim=-1)
            #final_lprobs = F.softmax(final_lprobs, -1)
            #final_lprobs = torch.log(final_lprobs + 1e-12)
            # print("ensemble")
            return final_lprobs, extra

    def extract_features(
        self,
        prev_output_tokens,
        encoder_out: Optional[Dict[str, List[Tensor]]],
        incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
        full_context_alignment: bool = False,
        alignment_layer: Optional[int] = None,
        alignment_heads: Optional[int] = None,
    ):
        return self.extract_features_scriptable(
            prev_output_tokens,
            encoder_out,
            incremental_state,
            full_context_alignment,
            alignment_layer,
            alignment_heads,
        )

    """
    A scriptable subclass of this class has an extract_features method and calls
    super().extract_features, but super() is not supported in torchscript. A copy of
    this function is made to be used in the subclass instead.
    """

    def extract_features_scriptable(
        self,
        prev_output_tokens,
        encoder_out: Optional[Dict[str, List[Tensor]]],
        incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
        full_context_alignment: bool = False,
        alignment_layer: Optional[int] = None,
        alignment_heads: Optional[int] = None,
    ):
        """
        Similar to *forward* but only return features.

        Includes several features from "Jointly Learning to Align and
        Translate with Transformer Models" (Garg et al., EMNLP 2019).

        Args:
            full_context_alignment (bool, optional): don't apply
                auto-regressive mask to self-attention (default: False).
            alignment_layer (int, optional): return mean alignment over
                heads at this layer (default: last layer).
            alignment_heads (int, optional): only average alignment over
                this many heads (default: all heads).

        Returns:
            tuple:
                - the decoder's features of shape `(batch, tgt_len, embed_dim)`
                - a dictionary with any model-specific outputs
        """
        bs, slen = prev_output_tokens.size()
        if alignment_layer is None:
            alignment_layer = self.num_layers - 1

        enc: Optional[Tensor] = None
        padding_mask: Optional[Tensor] = None
        if encoder_out is not None and len(encoder_out["encoder_out"]) > 0:
            enc = encoder_out["encoder_out"][0]
            assert (
                enc.size()[1] == bs
            ), f"Expected enc.shape == (t, {bs}, c) got {enc.shape}"
        if encoder_out is not None and len(encoder_out["encoder_padding_mask"]) > 0:
            padding_mask = encoder_out["encoder_padding_mask"][0]

        # embed positions
        positions = None
        if self.embed_positions is not None:
            positions = self.embed_positions(
                prev_output_tokens, incremental_state=incremental_state
            )

        if incremental_state is not None:
            prev_output_tokens = prev_output_tokens[:, -1:]
            if positions is not None:
                positions = positions[:, -1:]

        # embed tokens and positions
        x = self.embed_scale * self.embed_tokens(prev_output_tokens)

        if self.quant_noise is not None:
            x = self.quant_noise(x)

        if self.project_in_dim is not None:
            x = self.project_in_dim(x)

        if positions is not None:
            x += positions

        if self.layernorm_embedding is not None:
            x = self.layernorm_embedding(x)

        x = self.dropout_module(x)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        self_attn_padding_mask: Optional[Tensor] = None
        if self.cross_self_attention or prev_output_tokens.eq(self.padding_idx).any():
            self_attn_padding_mask = prev_output_tokens.eq(self.padding_idx)

        # decoder layers
        attn: Optional[Tensor] = None
        inner_states: List[Optional[Tensor]] = [x]
        for idx, layer in enumerate(self.layers):
            if incremental_state is None and not full_context_alignment:
                self_attn_mask = self.buffered_future_mask(x)
            else:
                self_attn_mask = None

            x, layer_attn, _ = layer(
                x,
                enc,
                padding_mask,
                incremental_state,
                self_attn_mask=self_attn_mask,
                self_attn_padding_mask=self_attn_padding_mask,
                need_attn=bool((idx == alignment_layer)),
                need_head_weights=bool((idx == alignment_layer)),
            )
            inner_states.append(x)
            if layer_attn is not None and idx == alignment_layer:
                attn = layer_attn.float().to(x)

        if attn is not None:
            if alignment_heads is not None:
                attn = attn[:alignment_heads]

            # average probabilities over heads
            attn = attn.mean(dim=0)

        if self.layer_norm is not None:
            x = self.layer_norm(x)

        # T x B x C -> B x T x C
        x = x.transpose(0, 1)

        if self.project_out_dim is not None:
            x = self.project_out_dim(x)

        return x, {"attn": [attn], "inner_states": inner_states}

    def output_layer(self, features):
        """Project features to the vocabulary size."""
        if self.adaptive_softmax is None:
            # project back to size of vocabulary
            return self.output_projection(features)
        else:
            return features

    def max_positions(self):
        """Maximum output length supported by the decoder."""
        if self.embed_positions is None:
            return self.max_target_positions
        return min(self.max_target_positions, self.embed_positions.max_positions)

    def buffered_future_mask(self, tensor):
        dim = tensor.size(0)
        # self._future_mask.device != tensor.device is not working in TorchScript. This is a workaround.
        if (
            self._future_mask.size(0) == 0
            or (not self._future_mask.device == tensor.device)
            or self._future_mask.size(0) < dim
        ):
            self._future_mask = torch.triu(
                utils.fill_with_neg_inf(torch.zeros([dim, dim])), 1
            )
        self._future_mask = self._future_mask.to(tensor)
        return self._future_mask[:dim, :dim]

    def upgrade_state_dict_named(self, state_dict, name):
        """Upgrade a (possibly old) state dict for new versions of fairseq."""
        if isinstance(self.embed_positions, SinusoidalPositionalEmbedding):
            weights_key = "{}.embed_positions.weights".format(name)
            if weights_key in state_dict:
                del state_dict[weights_key]
            state_dict[
                "{}.embed_positions._float_tensor".format(name)
            ] = torch.FloatTensor(1)

        if f"{name}.output_projection.weight" not in state_dict:
            if self.share_input_output_embed:
                embed_out_key = f"{name}.embed_tokens.weight"
            else:
                embed_out_key = f"{name}.embed_out"
            if embed_out_key in state_dict:
                state_dict[f"{name}.output_projection.weight"] = state_dict[
                    embed_out_key
                ]
                if not self.share_input_output_embed:
                    del state_dict[embed_out_key]

        for i in range(self.num_layers):
            # update layer norms
            layer_norm_map = {
                "0": "self_attn_layer_norm",
                "1": "encoder_attn_layer_norm",
                "2": "final_layer_norm",
            }
            for old, new in layer_norm_map.items():
                for m in ("weight", "bias"):
                    k = "{}.layers.{}.layer_norms.{}.{}".format(name, i, old, m)
                    if k in state_dict:
                        state_dict[
                            "{}.layers.{}.{}.{}".format(name, i, new, m)
                        ] = state_dict[k]
                        del state_dict[k]

        version_key = "{}.version".format(name)
        if utils.item(state_dict.get(version_key, torch.Tensor([1]))[0]) <= 2:
            # earlier checkpoints did not normalize after the stack of layers
            self.layer_norm = None
            self.normalize = False
            state_dict[version_key] = torch.Tensor([1])

        return state_dict


    def get_normalized_probs(
        self,
        net_output: Tuple[Tensor, Optional[Dict[str, List[Optional[Tensor]]]]],
        log_probs: bool,
        sample: Optional[Dict[str, Tensor]] = None,
    ):
        """Get normalized probabilities (or log probs) from a net's output."""
        return self.get_normalized_probs_scriptable(net_output, log_probs, sample)

    # TorchScript doesn't support super() method so that the scriptable Subclass
    # can't access the base class model in Torchscript.
    # Current workaround is to add a helper function with different name and
    # call the helper function from scriptable Subclass.
    
    def get_normalized_probs_scriptable(
        self,
        net_output: Tuple[Tensor, Optional[Dict[str, List[Optional[Tensor]]]]],
        log_probs: bool,
        sample: Optional[Dict[str, Tensor]] = None,
    ):
        """Get normalized probabilities (or log probs) from a net's output."""
        logits = net_output[0]
        return logits.contiguous()
    

class TransformerDecoderWithTM(TransformerDecoderBaseWithTM):
    def __init__(
        self,
        args,
        dictionary,
        embed_tokens,
        no_encoder_attn=False,
        output_projection=None,
    ):
        self.args = args
        super().__init__(
            TransformerConfig.from_namespace(args),
            dictionary,
            embed_tokens,
            no_encoder_attn=no_encoder_attn,
            output_projection=output_projection,
        )

    def build_output_projection(self, args, dictionary, embed_tokens):
        super().build_output_projection(
            TransformerConfig.from_namespace(args), dictionary, embed_tokens
        )

    def build_decoder_layer(self, args, no_encoder_attn=False):
        return super().build_decoder_layer(
            TransformerConfig.from_namespace(args), no_encoder_attn=no_encoder_attn
        )
