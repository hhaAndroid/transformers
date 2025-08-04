# coding=utf-8
# Copyright 2025 HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import collections.abc
from dataclasses import dataclass
from typing import Callable, Optional, Union, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.utils.checkpoint

from ...activations import ACT2FN
from ...cache_utils import Cache
from ...modeling_flash_attention_utils import FlashAttentionKwargs
from ...modeling_layers import GradientCheckpointingLayer
from ...modeling_outputs import BaseModelOutput, BaseModelOutputWithPooling
from ...modeling_utils import ALL_ATTENTION_FUNCTIONS, PreTrainedModel
from ...processing_utils import Unpack
from ...utils import (
    ModelOutput,
    TransformersKwargs,
    auto_docstring,
    can_return_tuple,
    is_torchdynamo_compiling,
    logging,
    torch_int,
)
from ..auto import AutoModel
from ..clip.modeling_clip import CLIPMLP
from ..janus.modeling_janus import JanusVisionAttention
from ..llama.modeling_llama import LlamaRMSNorm
from ..llava.modeling_llava import (
    LlavaForConditionalGeneration,
    LlavaModel,
    LlavaPreTrainedModel,
)
from .configuration_interns1 import InternS1Config, InternS1VisionConfig, InternTimeSeriesEncoderConfig,InternTSChatConfig


logger = logging.get_logger(__name__)


def eager_attention_forward(
    module: nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    scaling: float,
    dropout: float = 0.0,
    **kwargs,
):
    key_states = key
    value_states = value

    attn_weights = torch.matmul(query, key_states.transpose(2, 3)) * scaling
    if attention_mask is not None:
        causal_mask = attention_mask[:, :, :, : key_states.shape[-2]]
        attn_weights = attn_weights + causal_mask

    # No upcasting of the attention weights to float32 in this implementation
    attn_weights = nn.functional.softmax(attn_weights, dim=-1)
    attn_weights = nn.functional.dropout(attn_weights, p=dropout, training=module.training)
    attn_output = torch.matmul(attn_weights, value_states)
    attn_output = attn_output.transpose(1, 2).contiguous()

    return attn_output, attn_weights


class InternS1VisionRMSNorm(LlamaRMSNorm):
    pass


class InternS1VisionAttention(JanusVisionAttention):
    def __init__(self, config: InternS1VisionConfig):
        super().__init__()
        del self.num_key_value_groups

        # Needed for flash attention
        self.is_causal = False
        qk_norm = config.use_qk_norm

        self.q_norm = InternS1VisionRMSNorm(self.embed_dim) if qk_norm else nn.Identity()
        self.k_norm = InternS1VisionRMSNorm(self.embed_dim) if qk_norm else nn.Identity()

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        output_attentions: Optional[torch.Tensor] = None,
        **kwargs: Unpack[FlashAttentionKwargs],
    ):
        batch_size, seq_len, _ = hidden_states.size()

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = self.q_norm(query_states)
        key_states = self.k_norm(key_states)

        query_states = query_states.reshape(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.reshape(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(batch_size, seq_len, self.num_heads, self.head_dim).transpose(1, 2)

        attention_interface: Callable = eager_attention_forward
        if self.config._attn_implementation != "eager":
            attention_interface = ALL_ATTENTION_FUNCTIONS[self.config._attn_implementation]

        attn_output, attn_weights = attention_interface(
            self,
            query_states,
            key_states,
            value_states,
            attention_mask,
            dropout=0.0 if not self.training else self.attention_dropout,
            scaling=self.scale,
            is_causal=False,
            **kwargs,
        )
        attn_output = attn_output.reshape(batch_size, seq_len, self.embed_dim)

        output = self.projection_layer(attn_output)
        output = self.projection_dropout(output)

        outputs = (output, attn_weights) if output_attentions else (output, None)
        return outputs


@auto_docstring
class InternS1VisionPreTrainedModel(PreTrainedModel):
    config: InternS1VisionConfig
    base_model_prefix = "interns1_vision"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = True
    _no_split_modules = ["InternS1VisionLayer"]
    _supports_sdpa = True
    _supports_flash_attn = True
    _supports_flex_attn = True
    _supports_attention_backend = True

    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (nn.Linear, nn.Conv2d, nn.ConvTranspose2d)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        elif isinstance(module, InternS1VisionEmbeddings):
            module.cls_token.data.zero_()
            if module.mask_token is not None:
                module.mask_token.data.zero_()
            if module.position_embeddings is not None:
                module.position_embeddings.data.zero_()
        elif isinstance(module, InternS1VisionLayer):
            module.lambda_1.data.fill_(self.config.layer_scale_init_value)
            module.lambda_2.data.fill_(self.config.layer_scale_init_value)


@dataclass
@auto_docstring(
    custom_intro="""
    Class for outputs of [`InternS1VisionModel`].
    """
)
class InternS1VisionModelOutputWithPooling(BaseModelOutputWithPooling):
    r"""
    pooler_output (`torch.FloatTensor` of shape `(batch_size, hidden_size)`):
        Average of the last layer hidden states of the patch tokens (excluding the *[CLS]* token) if
        *config.use_mean_pooling* is set to True. If set to False, then the final hidden state of the *[CLS]* token
        will be returned.
    """


class InternS1VisionPatchEmbeddings(nn.Module):
    """
    This class turns `pixel_values` of shape `(batch_size, num_channels, height, width)` into the initial
    `hidden_states` (patch embeddings) of shape `(batch_size, seq_length, hidden_size)` to be consumed by a
    Transformer.
    """

    def __init__(self, config):
        super().__init__()
        image_size, patch_size = config.image_size, config.patch_size
        num_channels, hidden_size = config.num_channels, config.hidden_size

        num_patches = (image_size[1] // patch_size[1]) * (image_size[0] // patch_size[0])
        patch_shape = (image_size[0] // patch_size[0], image_size[1] // patch_size[1])
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = num_patches
        self.patch_shape = patch_shape

        self.projection = nn.Conv2d(num_channels, hidden_size, kernel_size=patch_size, stride=patch_size)

    def forward(self, pixel_values: torch.Tensor) -> torch.Tensor:
        batch_size, num_channels, height, width = pixel_values.shape
        if num_channels != self.num_channels:
            raise ValueError(
                "Make sure that the channel dimension of the pixel values match with the one set in the configuration."
            )

        embeddings = self.projection(pixel_values.to(self.projection.weight.dtype))
        patch_height, patch_width = embeddings.shape[2], embeddings.shape[3]
        embeddings = embeddings.flatten(2).transpose(1, 2)

        return embeddings, (patch_height, patch_width)


# Based on timm implementation, which can be found here:
# https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/vision_transformer.py
class InternS1VisionEmbeddings(nn.Module):
    """
    Construct the CLS token, position and patch embeddings. Optionally, also the mask token.

    """

    def __init__(self, config: InternS1VisionConfig) -> None:
        super().__init__()

        self.cls_token = nn.Parameter(torch.zeros(1, 1, config.hidden_size))
        if config.use_mask_token:
            self.mask_token = nn.Parameter(torch.zeros(1, 1, config.hidden_size))
        else:
            self.mask_token = None
        self.patch_embeddings = InternS1VisionPatchEmbeddings(config)
        self.patch_size = config.patch_size
        self.image_size = (
            config.image_size
            if isinstance(config.image_size, collections.abc.Iterable)
            else (config.image_size, config.image_size)
        )
        num_patches = self.patch_embeddings.num_patches
        if config.use_absolute_position_embeddings:
            self.position_embeddings = nn.Parameter(torch.zeros(1, num_patches + 1, config.hidden_size))
        else:
            self.position_embeddings = None
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def interpolate_pos_encoding(self, embeddings: torch.Tensor, height: int, width: int) -> torch.Tensor:
        """
        This method allows to interpolate the pre-trained position encodings, to be able to use the model on higher resolution
        images. This method is also adapted to support torch.jit tracing.

        Adapted from:
        - https://github.com/facebookresearch/dino/blob/de9ee3df6cf39fac952ab558447af1fa1365362a/vision_transformer.py#L174-L194, and
        - https://github.com/facebookresearch/dinov2/blob/e1277af2ba9496fbadf7aec6eba56e8d882d1e35/dinov2/models/vision_transformer.py#L179-L211
        """

        num_patches = embeddings.shape[1] - 1
        num_positions = self.position_embeddings.shape[1] - 1

        # always interpolate when tracing to ensure the exported model works for dynamic input shapes
        if not torch.jit.is_tracing() and num_patches == num_positions and height == width:
            return self.position_embeddings

        class_pos_embed = self.position_embeddings[:, :1]
        patch_pos_embed = self.position_embeddings[:, 1:]

        dim = embeddings.shape[-1]

        new_height = height // self.patch_size[0]
        new_width = width // self.patch_size[1]

        sqrt_num_positions = torch_int(num_positions**0.5)
        patch_pos_embed = patch_pos_embed.reshape(1, sqrt_num_positions, sqrt_num_positions, dim)
        patch_pos_embed = patch_pos_embed.permute(0, 3, 1, 2)

        patch_pos_embed = nn.functional.interpolate(
            patch_pos_embed,
            size=(new_height, new_width),
            mode="bicubic",
            align_corners=False,
        )

        patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, dim)

        return torch.cat((class_pos_embed, patch_pos_embed), dim=1)

    def forward(
        self,
        pixel_values: torch.Tensor,
        bool_masked_pos: Optional[torch.BoolTensor] = None,
    ) -> torch.Tensor:
        _, _, height, width = pixel_values.shape
        embeddings, (patch_height, patch_width) = self.patch_embeddings(pixel_values)
        batch_size, seq_len, _ = embeddings.size()

        if bool_masked_pos is not None:
            mask_tokens = self.mask_token.expand(batch_size, seq_len, -1)
            # replace the masked visual tokens by mask_tokens
            w = bool_masked_pos.unsqueeze(-1).type_as(mask_tokens)
            embeddings = embeddings * (1 - w) + mask_tokens * w

        cls_tokens = self.cls_token.expand(batch_size, -1, -1)
        embeddings = torch.cat((cls_tokens, embeddings), dim=1)

        if self.position_embeddings is not None:
            embeddings = embeddings + self.interpolate_pos_encoding(embeddings, height, width)

        embeddings = self.dropout(embeddings)

        return embeddings, (patch_height, patch_width)


# InternTS Encoder 
class InternTimeSeriesModel(PreTrainedModel):
    main_input_name = 'time_series_signals'
    _supports_flash_attn_2 = False
    config_class = InternTimeSeriesEncoderConfig
    _no_split_modules = ['WhisperEncoderLayer']

    def __init__(self, config: InternTimeSeriesEncoderConfig):
        super().__init__(config)
        self.config = config

        self.encoder_embed = Conv1dSubsampling(config)
        self.encoder = InternTimeSeriesEncoder(config)

    def get_input_embeddings(self):
        return self.encoder_embed

    def forward(
            self,
            time_series_signals: Optional[torch.FloatTensor] = None,
            x_lens: Optional[torch.Tensor] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            time_series_embeds: Optional[torch.FloatTensor] = None,
    ) -> Union[Tuple, TimeSeriesModelOutput]:
        
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if time_series_signals is None and time_series_embeds is None:
            raise ValueError('You have to specify time_series_signals or time_series_embeds')

        # 可以直接传入这个embed之后的值，但是需要维度都对得上
        if time_series_embeds is not None and len(time_series_embeds.shape) == 3 and time_series_embeds.shape[-1] == self.config.ts_adapt_in_dim:
            input_embeds = time_series_embeds
        else:
            if len(time_series_signals.shape) == 3:
                # 这里的x_lens最后应该是//10，后面的encoder再//2，总共是在cnn的基础上再//20
                input_embeds, x_lens = self.encoder_embed(time_series_signals, x_lens)
            else:
                raise ValueError(f'wrong time_series_signals size: {time_series_signals.shape}')

        # [B, 64000, 1] -> [B, 200, 256] -> [B, 100, 1024]
        encoder_outputs = self.encoder(
            input_features=input_embeds,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        # x_lens after encoder
        x_lens = (x_lens+1) // 2
        assert torch.all(x_lens > 0), f"The length of time_series_embeds is so small. x_lens: {x_lens}"
        # print("x_lens: ", x_lens)
        
        src_key_padding_mask = make_pad_mask(x_lens)

        last_hidden_state = encoder_outputs.last_hidden_state
        if not return_dict:
            return (last_hidden_state, ) + encoder_outputs[1:]

        return TimeSeriesModelOutput(
            last_hidden_state=last_hidden_state,
            hidden_states=encoder_outputs.hidden_states,    # encoder_state，for every layer
            attentions=encoder_outputs.attentions,
            ts_pad_mask=src_key_padding_mask
        )

@dataclass
class TimeSeriesModelOutput(BaseModelOutput):
    ts_pad_mask: Optional[torch.FloatTensor] = None

def make_pad_mask(lengths: torch.Tensor) -> torch.Tensor:
    """
    Args:
      lengths:
        A 1-D tensor containing sentence lengths.
      max_len:
        The length of masks.
    Returns:
      Return a 2-D bool tensor, where masked positions
      are filled with `True` and non-masked positions are
      filled with `False`.

    >>> lengths = torch.tensor([1, 3, 2, 5])
    >>> make_pad_mask(lengths)
    tensor([[False,  True,  True,  True,  True],
            [False, False, False,  True,  True],
            [False, False,  True,  True,  True],
            [False, False, False, False, False]])
    """
    assert lengths.ndim == 1, lengths.ndim
    max_len = lengths.max()
    n = lengths.size(0)
    seq_range = torch.arange(0, max_len, device=lengths.device)
    expaned_lengths = seq_range.unsqueeze(0).expand(n, max_len)
    return expaned_lengths >= lengths.unsqueeze(-1)

def _make_causal_mask(
    input_ids_shape: torch.Size, dtype: torch.dtype, device: torch.device, past_key_values_length: int = 0
):
    """
    Make causal mask used for bi-directional self-attention.
    """
    bsz, tgt_len = input_ids_shape
    mask = torch.full((tgt_len, tgt_len), torch.finfo(dtype).min, device=device)
    mask_cond = torch.arange(mask.size(-1), device=device)
    mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
    mask = mask.to(dtype)

    if past_key_values_length > 0:
        mask = torch.cat([torch.zeros(tgt_len, past_key_values_length, dtype=dtype, device=device), mask], dim=-1)
    return mask[None, None, :, :].expand(bsz, 1, tgt_len, tgt_len + past_key_values_length)

# Copied from transformers.models.bart.modeling_bart._expand_mask
def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    # print(mask.size())
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)

class InternTimeSeriesEncoder(WhisperPreTrainedModel):

    def __init__(self, config: InternTimeSeriesEncoderConfig):
        super().__init__(config)
        
        self.config = config

        self.dropout = config.dropout
        self.layerdrop = config.encoder_layerdrop

        self.embed_dim = config.d_model
        self.num_mel_bins = config.num_mel_bins
        self.padding_idx = config.pad_token_id
        self.max_source_positions = config.max_source_positions
        self.embed_scale = math.sqrt(self.embed_dim) if config.scale_embedding else 1.0

        self.conv1 = nn.Conv1d(self.num_mel_bins, self.embed_dim, kernel_size=3, padding=1)
        self.conv2 = nn.Conv1d(self.embed_dim, self.embed_dim, kernel_size=3, stride=2, padding=1)
        self.embed_positions = nn.Embedding(self.max_source_positions, self.embed_dim)

        self.layers = nn.ModuleList([WhisperEncoderLayer(config) for _ in range(config.encoder_layers)])
        self.layer_norm = nn.LayerNorm(config.d_model)

        self.gradient_checkpointing = False
        self.post_init()

        self.mask_type = None
        self.chunk_length = None

        self.adapt_in = nn.Linear(config.ts_adapt_in_dim, 80)
        self.adapt_out = nn.Linear(self.embed_dim, config.ts_adapt_out_dim)
        
    def _freeze_parameters(self):
        for param in self.parameters():
            param.requires_grad = False
        self._requires_grad = False

    def get_input_embeddings(self) -> nn.Module:
        return self.conv1

    def set_input_embeddings(self, value: nn.Module):
        self.conv1 = value
        
    def define_masktype(self, masktype, chunk_length=None):
        self.mask_type = masktype
        self.chunk_length = chunk_length

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        # create causal mask
        # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
        combined_attention_mask = None

        if input_shape[-1] > 1:
            combined_attention_mask = _make_causal_mask(
                input_shape,
                inputs_embeds.dtype,
                device=inputs_embeds.device,
                past_key_values_length=past_key_values_length,
            )

        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1])
            combined_attention_mask = (
                expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
            )
        return combined_attention_mask
    
    def prepare_chunk_attention_mask(self, attention_mask, input_shape, inputs_embeds):
        
        block_size = round(self.chunk_length / 4 * 2)
        matrix_size = input_shape[1]
        
        matrix = torch.ones(matrix_size, matrix_size)
        
        num_full_blocks = round(matrix_size // block_size)
        remainder = matrix_size % block_size  
        for i in range(num_full_blocks):
            row_start = i * block_size
            col_start = i * block_size
            matrix[row_start:row_start + block_size, col_start:col_start + block_size] = torch.zeros(block_size, block_size)
        
        if remainder > 0:
            last_row_start = num_full_blocks * block_size
            last_col_start = num_full_blocks * block_size
            matrix[last_row_start:last_row_start + remainder, last_col_start:last_col_start + remainder] = torch.zeros(remainder, remainder)
        
        matrix = matrix * -65504
        matrix = matrix.unsqueeze(0).unsqueeze(0).repeat(input_shape[0], 1, 1, 1)
        attention_mask = matrix.to(inputs_embeds.device)
        return attention_mask
    
    def forward(
            self,
            input_features,
            attention_mask=None,
            head_mask=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
    ):
        # (N, T, C) -> (T, N, C) -> (N, C, T)
        input_features = input_features.permute(1, 0, 2)
        input_features = self.adapt_in(input_features)
        input_features = input_features.permute(1, 2, 0)
        
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # (N, C, T) -> (N, C, T//2)
        inputs_embeds = nn.functional.gelu(self.conv1(input_features))
        inputs_embeds = nn.functional.gelu(self.conv2(inputs_embeds))

        # (N, C, T) -> (N, T, C)
        inputs_embeds = inputs_embeds.permute(0, 2, 1)  # torch.Size([1, 100, 768])
        embed_pos = self.embed_positions.weight         # torch.Size([1500, 768])
        
        if inputs_embeds.shape[1] > embed_pos.shape[0]:
            target_len = inputs_embeds.shape[1]
            padding = [0, 0, 0, target_len-embed_pos.shape[0]]

            embed_pos = F.pad(embed_pos, pad=padding, mode='constant', value=0)
            hidden_states = inputs_embeds[:, :embed_pos.shape[0], :] + embed_pos
        else:
            hidden_states = inputs_embeds + embed_pos[:inputs_embeds.shape[1], :]
        hidden_states = nn.functional.dropout(hidden_states, p=self.dropout, training=self.training)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        
        input_shape = inputs_embeds.size()[:-1]
        past_key_values_length = 0
        attention_mask = None
        if self.mask_type == 'chunk':
            attention_mask = self.prepare_chunk_attention_mask(attention_mask, input_shape, inputs_embeds)
        else:
            attention_mask = self._prepare_decoder_attention_mask(
                attention_mask, input_shape, inputs_embeds, past_key_values_length
            )

        if head_mask is not None:
            assert head_mask.size()[0] == (
                len(self.layers)
            ), f"The head_mask should be specified for {len(self.layers)} layers, but it is for {head_mask.size()[0]}."

        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (self.layer_norm(hidden_states),)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            to_drop = False
            if self.training:
                dropout_probability = torch.rand([])
                if dropout_probability < self.layerdrop:  # skip the layer
                    to_drop = True

            if to_drop:
                layer_outputs = (None, None)
            else:
                if self.gradient_checkpointing and self.training:

                    def create_custom_forward(module):
                        def custom_forward(*inputs):
                            return module(*inputs, output_attentions)

                        return custom_forward

                    layer_outputs = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(encoder_layer),
                        hidden_states,
                        attention_mask,
                        (head_mask[idx] if head_mask is not None else None),
                    )
                else:
                    layer_outputs = encoder_layer(
                        hidden_states,
                        attention_mask,
                        layer_head_mask=(head_mask[idx] if head_mask is not None else None),
                        output_attentions=output_attentions,
                    )

                hidden_states = layer_outputs[0]

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        # (N, T, C) -> (T, N, C)
        hidden_states = hidden_states.permute(1, 0, 2)
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = self.adapt_out(hidden_states)

        # (T, N, C) -> (N, T, C)
        hidden_states = hidden_states.permute(1, 0, 2)
        if output_hidden_states:
            encoder_states = encoder_states + (hidden_states,)
            
        if not return_dict:
            return tuple(v for v in [hidden_states, encoder_states, all_attentions] if v is not None)
        return BaseModelOutput(
            last_hidden_state=hidden_states, hidden_states=encoder_states, attentions=all_attentions
        )

class ConcatSubsampling(nn.Module):
    
    def __init__(
        self,
        in_channels: int,
        concat_size: int,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = in_channels * concat_size
    
    def forward(self, x: torch.Tensor, x_lens: torch.Tensor):
        
        if x.shape[1] % 2 != 0:
            x = x[:, :-1, :]
            
        even_frames = x[:, ::2, :]  
        odd_frames = x[:, 1::2, :]
        x = torch.cat((even_frames, odd_frames), dim=2)
        
        x_lens = x_lens // 2
        return x, x_lens

class Conv1dSubsampling(nn.Module):
    def __init__(self, config: InternTimeSeriesEncoderConfig):
        super().__init__()

        self.channels = config.ts_cnn_channels
        self.kernel_sizes = config.ts_cnn_kernel_sizes
        self.strides = config.ts_cnn_strides
        self.paddings = config.ts_cnn_paddings
        self.concat_in_channels = config.ts_concat_subsampling_in_channels
        self.concat_size = config.ts_concat_subsampling_concat_size

        self.n_layers = len(self.channels) - 1
        if not (len(self.kernel_sizes) == len(self.strides) == len(self.paddings) == self.n_layers):
            raise ValueError("Length mismatch: channels should have 1 more element than kernel_sizes/strides/paddings.")
        
        self.layers = nn.ModuleList()
        in_channels = self.channels[0]
        for i in range(self.n_layers):
            out_channels = self.channels[i + 1]
            conv = nn.Conv1d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=self.kernel_sizes[i],
                stride=self.strides[i],
                padding=self.paddings[i]
            )
            # bn = nn.BatchNorm1d(out_channels)
            # self.layers.append(nn.Sequential(conv, bn))
            self.layers.append(nn.Sequential(conv))
            in_channels = out_channels

        self.subsampling = ConcatSubsampling(self.concat_in_channels, self.concat_size)
        
    def forward(self, x: torch.Tensor, x_lens: torch.Tensor):

        x = x.transpose(1, 2)

        for layer in self.layers:
            x = layer(x)
            x = F.relu(x)
        
        x = x.transpose(1, 2)

        x_lens = ((((x_lens + 1) // 2 + 3) // 4 + 3)// 4 + 4)// 5 
        x, x_lens = self.subsampling(x, x_lens)
        return x, x_lens


class InternS1VisionMLP(CLIPMLP):
    pass


NORM2FN = {"layer_norm": nn.LayerNorm, "rms_norm": InternS1VisionRMSNorm}


class InternS1VisionLayer(GradientCheckpointingLayer):
    """This corresponds to the Block class in the timm implementation."""

    def __init__(self, config: InternS1VisionConfig, drop_path_rate=0.0) -> None:
        super().__init__()
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = InternS1VisionAttention(config)
        self.mlp = InternS1VisionMLP(config)
        # InternS1 uses different layernorm implementations for different models
        self.layernorm_before = NORM2FN[config.norm_type](config.hidden_size, eps=config.layer_norm_eps)
        self.layernorm_after = NORM2FN[config.norm_type](config.hidden_size, eps=config.layer_norm_eps)

        init_values = config.layer_scale_init_value
        self.lambda_1 = nn.Parameter(init_values * torch.ones(config.hidden_size), requires_grad=True)
        self.lambda_2 = nn.Parameter(init_values * torch.ones(config.hidden_size), requires_grad=True)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        if drop_path_rate > 0.:
            try:
                from timm.layers import DropPath
            except ImportError:
                raise ImportError("timm is not installed, please install it to use DropPath "
                                  "by 'pip install timm'. ")
            self.drop_path1 = DropPath(drop_path_rate)
            self.drop_path2 = DropPath(drop_path_rate)
        else:
            self.drop_path1 = nn.Identity()
            self.drop_path2 = nn.Identity()

    def forward(
        self,
        hidden_states: torch.Tensor,
        output_attentions: bool = False,
    ) -> Union[tuple[torch.Tensor], tuple[torch.Tensor, torch.Tensor]]:
        attention_output, attention_weights = self.attention(
            self.layernorm_before(hidden_states),  # in InternS1Vision, layernorm is applied before self-attention
            output_attentions=output_attentions,
        )

        attention_output = self.lambda_1 * attention_output

        # first residual connection
        hidden_states = self.drop_path1(attention_output) + hidden_states

        # in InternS1Vision, layernorm is also applied after self-attention
        layer_output = self.layernorm_after(hidden_states)

        layer_output = self.mlp(layer_output)
        layer_output = self.dropout(layer_output)

        if self.lambda_2 is not None:
            layer_output = self.lambda_2 * layer_output

        # second residual connection
        layer_output = self.drop_path2(layer_output) + hidden_states

        return layer_output, attention_weights


class InternS1VisionEncoder(nn.Module):
    def __init__(self, config: InternS1VisionConfig) -> None:
        super().__init__()
        self.config = config
        dpr = np.linspace(0.0, float(config.drop_path_rate), int(config.num_hidden_layers))
        self.layer = nn.ModuleList([
            InternS1VisionLayer(config, dpr[idx]) for idx in range(config.num_hidden_layers)])
        self.gradient_checkpointing = False

    @can_return_tuple
    def forward(
        self,
        hidden_states: torch.Tensor,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
    ) -> Union[tuple, BaseModelOutput]:
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None

        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_outputs = layer_module(hidden_states, output_attentions)

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_outputs[1],)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        return BaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )


@auto_docstring
class InternS1VisionModel(InternS1VisionPreTrainedModel):
    def __init__(self, config: InternS1VisionConfig) -> None:
        super().__init__(config)
        self.config = config

        self.embeddings = InternS1VisionEmbeddings(config)
        self.encoder = InternS1VisionEncoder(config)

        self.layernorm = (
            nn.Identity() if config.use_mean_pooling else nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        )

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embeddings.patch_embeddings

    @can_return_tuple
    @auto_docstring
    def forward(
        self,
        pixel_values: torch.Tensor,
        bool_masked_pos: Optional[torch.BoolTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
    ) -> Union[tuple, InternS1VisionModelOutputWithPooling]:
        r"""
        bool_masked_pos (`torch.BoolTensor` of shape `(batch_size, num_patches)`, *optional*):
            Boolean masked positions. Indicates which patches are masked (1) and which aren't (0).
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )

        embedding_output, _ = self.embeddings(pixel_values, bool_masked_pos=bool_masked_pos)

        encoder_outputs = self.encoder(
            embedding_output,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )
        sequence_output = encoder_outputs[0]
        sequence_output = self.layernorm(sequence_output)

        return InternS1VisionModelOutputWithPooling(
            last_hidden_state=sequence_output,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
        )


class InternS1PreTrainedModel(LlavaPreTrainedModel):
    def _init_weights(self, module):
        std = getattr(self.config, "initializer_range", self.config.get_text_config().initializer_range)

        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


# InternTS Projector
class InternTSProjector(nn.Module):
    def __init__(self, config: InternS1Config):
        super().__init__()
        ts_hidden_size = config.ts_config.ts_hidden_dim
        llm_hidden_size = config.text_config.hidden_size

        self.layer_norm = nn.LayerNorm(ts_hidden_size)
        self.linear_1 = nn.Linear(ts_hidden_size, llm_hidden_size)
        self.act = nn.GELU()
        self.linear_2 = nn.Linear(llm_hidden_size, llm_hidden_size)

    def forward(self, ts_features):
        hidden_states = self.layer_norm(ts_features)
        hidden_states = self.linear_1(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states

class InternS1MultiModalProjector(nn.Module):
    def __init__(self, config: InternS1Config):
        super().__init__()
        self.layer_norm = nn.LayerNorm(config.vision_config.hidden_size * int(1 / config.downsample_ratio) ** 2)
        self.linear_1 = nn.Linear(
            config.vision_config.hidden_size * int(1 / config.downsample_ratio) ** 2, config.text_config.hidden_size
        )
        self.act = ACT2FN[config.projector_hidden_act]
        self.linear_2 = nn.Linear(config.text_config.hidden_size, config.text_config.hidden_size)

    def forward(self, image_features):
        hidden_states = self.layer_norm(image_features)
        hidden_states = self.linear_1(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states



@dataclass
@auto_docstring(
    custom_intro="""
    Base class for InternS1 outputs, with hidden states and attentions.
    """
)
class InternS1ModelOutputWithPast(ModelOutput):
    r"""
    past_key_values (`Cache`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
        It is a [`~cache_utils.Cache`] instance. For more details, see our [kv cache guide](https://huggingface.co/docs/transformers/en/kv_cache).

        Contains pre-computed hidden-states (key and values in the self-attention blocks and optionally if
        `config.is_encoder_decoder=True` in the cross-attention blocks) that can be used (see `past_key_values`
        input) to speed up sequential decoding.
    hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
        Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
        one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

        Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
    attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
        Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
        sequence_length)`.

        Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
        heads.
    router_logits (`tuple(torch.FloatTensor)`, *optional*, returned when `output_router_probs=True` and `config.add_router_probs=True` is passed or when `config.output_router_probs=True`):
        Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, sequence_length, num_experts)`.

        Raw router logtis (post-softmax) that are computed by MoE routers, these terms are used to compute the auxiliary
        loss for Mixture of Experts models.
    image_hidden_states (`torch.FloatTensor`, *optional*):
        A `torch.FloatTensor` of size `(batch_size, num_images, sequence_length, hidden_size)`.
        image_hidden_states of the model produced by the vision encoder and after projecting the last hidden state.
    """

    last_hidden_state: Optional[torch.FloatTensor] = None
    past_key_values: Optional[Cache] = None
    hidden_states: Optional[tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[tuple[torch.FloatTensor, ...]] = None
    router_logits: Optional[tuple[torch.FloatTensor]] = None
    image_hidden_states: Optional[torch.FloatTensor] = None
    time_series_hidden_states: Optional[torch.FloatTensor] = None


class InternS1Model(LlavaModel):
    _checkpoint_conversion_mapping = {}

    def __init__(self, config: InternS1Config):
        super().__init__(config)

        # InternS1 Vision Model
        self.vision_tower = AutoModel.from_config(config.vision_config)
        self.multi_modal_projector = InternS1MultiModalProjector(config)

        
        # InternTS model
        self.select_layer = config.ts_select_layer if hasattr(config, "ts_select_layer") else -1
        self.time_series_model = InternTimeSeriesModel(config.ts_config)
        self.ts_context_token_id = config.ts_context_token_id if hasattr(config, "ts_context_token_id") else None

        # InternTS projector
        self.ts_projector = InternTSProjector(config)

        # InternS1 Language Model
        self.language_model = AutoModel.from_config(config.text_config)

        self.is_moe_model = False
        if hasattr(config.text_config, 'output_router_logits'):
            self.is_moe_model = True

        self.post_init()

    def pixel_shuffle(self, vision_features: torch.Tensor, scale_factor: float = 0.5):
        """Perform pixel shuffle downsampling on vision features.

        Args:
            vision_features (`torch.Tensor`):
                Input tensor of shape (batch_size, width, height, channels).
            scale_factor (`float`, *optional*, defaults to `0.5`):
                Factor by which to downsample. Default is 0.5, which halves the dimensions.

        Returns:
            vision_features (`torch.Tensor`):
                Downsampled tensor of shape (batch_size, height*scale_factor, width*scale_factor, channels/(scale_factor^2)).
        """
        batch_size, width, height, channels = vision_features.size()

        if height % scale_factor != 0 or width % scale_factor != 0:
            raise ValueError("Height and width must be divisible by scale_factor for proper downsampling.")

        # Reshape to allow downsampling
        vision_features = vision_features.view(
            batch_size, width, int(height * scale_factor), int(channels / scale_factor)
        )
        # Permute dimensions to align downsampled axis correctly
        vision_features = vision_features.permute(0, 2, 1, 3).contiguous()

        # Reshape to achieve final downsampled dimensions
        vision_features = vision_features.view(
            batch_size, int(height * scale_factor), int(width * scale_factor), int(channels / (scale_factor**2))
        )

        # Swap height and width back for proper orientation
        vision_features = vision_features.permute(0, 2, 1, 3).contiguous()

        return vision_features

    def get_image_features(
        self,
        pixel_values: torch.FloatTensor,
        vision_feature_layer: Optional[Union[int, list[int]]] = None,
        vision_feature_select_strategy: Optional[str] = None,
        **kwargs,
    ):
        """
        Obtains image last hidden states from the vision tower and apply multimodal projection.

        Args:
            pixel_values (`torch.FloatTensor]` of shape `(batch_size, channels, height, width)`)
               The tensors corresponding to the input images.
            vision_feature_layer (`int` or `list[int]`):
                Layer index or list of layer indices to extract features from.
        Returns:
            vision_features (`torch.Tensor`): Image feature tensor of shape `(num_images, image_length, embed_dim)`.
        """
        vision_feature_layer = (
            vision_feature_layer if vision_feature_layer is not None else self.config.vision_feature_layer
        )
        vision_feature_select_strategy = (
            vision_feature_select_strategy
            if vision_feature_select_strategy is not None
            else self.config.vision_feature_select_strategy
        )

        downsample_ratio = self.config.downsample_ratio
        if vision_feature_layer == -1:
            vision_features = self.vision_tower(pixel_values=pixel_values).last_hidden_state
        else:
            vision_features = self.vision_model(pixel_values=pixel_values).hidden_states[vision_feature_layer]
        if vision_feature_select_strategy == "default":
            vision_features = vision_features[:, 1:, :]

        # Calculate dimensions based on vision features
        channels = vision_features.shape[1]
        feature_size = int(channels**0.5)
        batch_size = vision_features.shape[0]

        # Reshape tensor to spatial dimensions
        vision_features = vision_features.reshape(batch_size, feature_size, feature_size, -1)

        # Apply downsampling using pixel shuffle
        vision_features = self.pixel_shuffle(vision_features, scale_factor=downsample_ratio)

        # Reshape tensor to prepare for projection
        vision_features = vision_features.reshape(batch_size, -1, vision_features.shape[-1])

        # Project features through multi-modal projector
        vision_features = self.multi_modal_projector(vision_features)
        return vision_features

    # InternTS 
    def get_time_series_features(
        self,
        time_series_signals: torch.FloatTensor,
        x_lens: torch.Tensor,
    ):
        """
        Obtains time series last hidden states from the time series encoder and apply projection.

        Args:
            time_series_signals (`torch.FloatTensor` of shape `(batch_size, sequence_length, num_features)`)
               The tensors corresponding to the input time series signals.
            x_lens (`torch.Tensor` of shape `(batch_size,)`)
               The lengths of each time series sequence.
        Returns:
            time_series_features (`torch.Tensor`): Time series feature tensor of shape `(batch_size, sequence_length, embed_dim)`.
            ts_pad_mask (`torch.Tensor`): Padding mask for time series features.
        """
        if self.select_layer == -1:
            ts_output = self.time_series_model(
                time_series_signals=time_series_signals,
                x_lens=x_lens,
                output_hidden_states=False,
                return_dict=True)
            ts_embeds = ts_output.last_hidden_state
            ts_pad_mask = ts_output.ts_pad_mask
        else:
            ts_output = self.time_series_model(
                time_series_signals=time_series_signals,
                x_lens=x_lens,
                output_hidden_states=True,
                return_dict=True)
            # the pad_mask of each layer is the same
            ts_embeds = ts_output.hidden_states[self.select_layer]
            ts_pad_mask = ts_output.ts_pad_mask
        
        # Project features through time series projector
        time_series_features = self.ts_projector(ts_embeds)
        return time_series_features, ts_pad_mask

    # InternS1
    def get_input_modality(self, pixel_values=None, time_series_signals=None):
        """
        Determine the input modality based on the provided inputs.
        
        Args:
            pixel_values (`torch.FloatTensor`, *optional*): Image input
            time_series_signals (`torch.FloatTensor`, *optional*): Time series input
            
        Returns:
            str: Modality type - 'vision', 'time_series', 'multimodal', or 'text_only'
        """
        has_vision = pixel_values is not None
        has_time_series = time_series_signals is not None
        
        if has_vision and has_time_series:
            return 'multimodal'
        elif has_vision:
            return 'vision'
        elif has_time_series:
            return 'time_series'
        else:
            return 'text_only'

    @can_return_tuple
    @auto_docstring
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        pixel_values: torch.FloatTensor = None,
        # InternTS
        time_series_signals: torch.FloatTensor = None,
        x_lens: torch.Tensor = None,
        # --
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        vision_feature_layer: Optional[Union[int, list[int]]] = None,
        vision_feature_select_strategy: Optional[str] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        **kwargs: Unpack[FlashAttentionKwargs],
    ) -> InternS1ModelOutputWithPast:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        if self.is_moe_model:
            output_router_logits = (
                output_router_logits if output_router_logits is not None else
                self.config.text_config.output_router_logits
            )
            kwargs['output_router_logits'] = output_router_logits

        vision_feature_layer = (
            vision_feature_layer if vision_feature_layer is not None else self.config.vision_feature_layer
        )
        vision_feature_select_strategy = (
            vision_feature_select_strategy
            if vision_feature_select_strategy is not None
            else self.config.vision_feature_select_strategy
        )

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.get_input_embeddings()(input_ids)

        if pixel_values is not None:
            image_features = self.get_image_features(
                pixel_values=pixel_values,
                vision_feature_layer=vision_feature_layer,
                vision_feature_select_strategy=vision_feature_select_strategy,
            )

            if input_ids is None:
                special_image_mask = inputs_embeds == self.get_input_embeddings()(
                    torch.tensor(self.config.image_token_id, dtype=torch.long, device=inputs_embeds.device)
                )
                special_image_mask = special_image_mask.all(-1)
            else:
                special_image_mask = input_ids == self.config.image_token_id

            n_image_tokens = (special_image_mask).sum()
            special_image_mask = special_image_mask.unsqueeze(-1).expand_as(inputs_embeds).to(inputs_embeds.device)

            if not is_torchdynamo_compiling() and inputs_embeds[special_image_mask].numel() != image_features.numel():
                n_image_features = image_features.shape[0] * image_features.shape[1]
                raise ValueError(
                    f"Image features and image tokens do not match: tokens: {n_image_tokens}, features {n_image_features}"
                )
            image_features = image_features.to(inputs_embeds.device, inputs_embeds.dtype)
            inputs_embeds = inputs_embeds.masked_scatter(special_image_mask, image_features)


        # InternTS Time series features
        if time_series_signals is not None:
            time_series_features, ts_pad_mask = self.get_time_series_features(
                time_series_signals=time_series_signals,
                x_lens=x_lens,
            )
            
            # Get valid time series features (remove padding)
            ts_features_valid = time_series_features[~ts_pad_mask]   # [num_valid_ts_tokens, C]
            ts_features_valid = ts_features_valid.to(inputs_embeds.device)

            # Find time series context tokens
            if input_ids is None:
                special_ts_mask = inputs_embeds == self.get_input_embeddings()(
                    torch.tensor(self.ts_context_token_id, dtype=torch.long, device=inputs_embeds.device)
                )
                special_ts_mask = special_ts_mask.all(-1)
            else:
                special_ts_mask = input_ids == self.ts_context_token_id

            n_ts_tokens = (special_ts_mask).sum()
            special_ts_mask = special_ts_mask.unsqueeze(-1).expand_as(inputs_embeds).to(inputs_embeds.device)

            if not is_torchdynamo_compiling() and inputs_embeds[special_ts_mask].numel() != ts_features_valid.numel():
                n_ts_features = ts_features_valid.shape[0]
                raise ValueError(
                    f"Time series features and time series tokens do not match: tokens: {n_ts_tokens}, features {n_ts_features}"
                )
            
            # Replace time series context tokens with actual features
            inputs_embeds = inputs_embeds.masked_scatter(special_ts_mask, ts_features_valid)
        # --

        outputs = self.language_model(
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_router_logits=output_router_logits,
            cache_position=cache_position,
            **kwargs,
        )

        return InternS1ModelOutputWithPast(
            last_hidden_state=outputs.last_hidden_state,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            router_logits=outputs.router_logits if self.is_moe_model else None,
            image_hidden_states=image_features if pixel_values is not None else None,
            time_series_hidden_states=time_series_features if time_series_signals is not None else None,
        )


@dataclass
@auto_docstring(
    custom_intro="""
    Base class for InternS1 causal language model (or autoregressive) outputs.
    """
)
class InternS1CausalLMOutputWithPast(ModelOutput):
    r"""
    loss (`torch.FloatTensor` of shape `(1,)`, *optional*, returned when `labels` is provided):
        Language modeling loss (for next-token prediction).
    aux_loss (`torch.FloatTensor`, *optional*, returned when `labels` is provided):
        aux_loss for the sparse modules.
    logits (`torch.FloatTensor` of shape `(batch_size, sequence_length, config.vocab_size)`):
        Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
    past_key_values (`Cache`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
        It is a [`~cache_utils.Cache`] instance. For more details, see our [kv cache guide](https://huggingface.co/docs/transformers/en/kv_cache).

        Contains pre-computed hidden-states (key and values in the self-attention blocks) that can be used (see
        `past_key_values` input) to speed up sequential decoding.
    hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
        Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
        one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

        Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
    attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
        Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
        sequence_length)`.

        Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
        heads.
    router_logits (`tuple(torch.FloatTensor)`, *optional*, returned when `output_router_probs=True` and `config.add_router_probs=True` is passed or when `config.output_router_probs=True`):
        Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, sequence_length, num_experts)`.

        Raw router logtis (post-softmax) that are computed by MoE routers, these terms are used to compute the auxiliary
        loss for Mixture of Experts models.
    image_hidden_states (`torch.FloatTensor`, *optional*):
        A `torch.FloatTensor` of size `(batch_size, num_images, sequence_length, hidden_size)`.
        image_hidden_states of the model produced by the vision encoder and after projecting the last hidden state.
    """

    loss: Optional[torch.FloatTensor] = None
    aux_loss: Optional[torch.FloatTensor] = None
    logits: Optional[torch.FloatTensor] = None
    past_key_values: Optional[Cache] = None
    hidden_states: Optional[tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[tuple[torch.FloatTensor, ...]] = None
    router_logits: Optional[tuple[torch.FloatTensor]] = None
    image_hidden_states: Optional[torch.FloatTensor] = None
    time_series_hidden_states: Optional[torch.FloatTensor] = None


def load_balancing_loss_func(
    gate_logits: Union[torch.Tensor, tuple[torch.Tensor], None],
    num_experts: Optional[int] = None,
    top_k=2,
    attention_mask: Optional[torch.Tensor] = None,
) -> Union[torch.Tensor, int]:
    r"""
    Computes auxiliary load balancing loss as in Switch Transformer - implemented in Pytorch.

    See Switch Transformer (https://huggingface.co/papers/2101.03961) for more details. This function implements the loss
    function presented in equations (4) - (6) of the paper. It aims at penalizing cases where the routing between
    experts is too unbalanced.

    Args:
        gate_logits:
            Logits from the `gate`, should be a tuple of model.config.num_hidden_layers tensors of
            shape [batch_size X sequence_length, num_experts].
        num_experts:
            Number of experts
        top_k:
            The number of experts to route per-token, can be also interpreted as the `top-k` routing
            parameter.
        attention_mask (`torch.Tensor`, *optional*):
            The attention_mask used in forward function
            shape [batch_size X sequence_length] if not None.

    Returns:
        The auxiliary loss.
    """
    if gate_logits is None or not isinstance(gate_logits, tuple):
        return 0

    if isinstance(gate_logits, tuple):
        compute_device = gate_logits[0].device
        concatenated_gate_logits = torch.cat([layer_gate.to(compute_device) for layer_gate in gate_logits], dim=0)

    routing_weights = torch.nn.functional.softmax(concatenated_gate_logits, dim=-1)

    _, selected_experts = torch.topk(routing_weights, top_k, dim=-1)

    expert_mask = torch.nn.functional.one_hot(selected_experts, num_experts)

    if attention_mask is None:
        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = torch.mean(expert_mask.float(), dim=0)

        # Compute the average probability of routing to these experts
        router_prob_per_expert = torch.mean(routing_weights, dim=0)
    else:
        batch_size, sequence_length = attention_mask.shape
        num_hidden_layers = concatenated_gate_logits.shape[0] // (batch_size * sequence_length)

        # Compute the mask that masks all padding tokens as 0 with the same shape of expert_mask
        expert_attention_mask = (
            attention_mask[None, :, :, None, None]
            .expand((num_hidden_layers, batch_size, sequence_length, top_k, num_experts))
            .reshape(-1, top_k, num_experts)
            .to(compute_device)
        )

        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = torch.sum(expert_mask.float() * expert_attention_mask, dim=0) / torch.sum(
            expert_attention_mask, dim=0
        )

        # Compute the mask that masks all padding tokens as 0 with the same shape of tokens_per_expert
        router_per_expert_attention_mask = (
            attention_mask[None, :, :, None]
            .expand((num_hidden_layers, batch_size, sequence_length, num_experts))
            .reshape(-1, num_experts)
            .to(compute_device)
        )

        # Compute the average probability of routing to these experts
        router_prob_per_expert = torch.sum(routing_weights * router_per_expert_attention_mask, dim=0) / torch.sum(
            router_per_expert_attention_mask, dim=0
        )

    overall_loss = torch.sum(tokens_per_expert * router_prob_per_expert.unsqueeze(0))
    return overall_loss * num_experts


class InternS1ForConditionalGeneration(LlavaForConditionalGeneration):
    _checkpoint_conversion_mapping = {}

    def __init__(self, config: InternS1Config):
        super().__init__(config)
        self.model = InternS1Model(config)
        self.lm_head = nn.Linear(config.text_config.hidden_size, config.text_config.vocab_size, bias=False)

        self.is_moe_model = False
        if hasattr(config.text_config, 'output_router_logits'):
            self.is_moe_model = True
        self.post_init()

    @can_return_tuple
    @auto_docstring
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        pixel_values: torch.FloatTensor = None,
        # InternTS
        time_series_signals: torch.FloatTensor = None,
        x_lens: torch.Tensor = None,
        # --
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        vision_feature_layer: Optional[Union[int, list[int]]] = None,
        vision_feature_select_strategy: Optional[str] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        logits_to_keep: Union[int, torch.Tensor] = 0,
        image_sizes: Optional[torch.Tensor] = None,
        **kwargs: Unpack[TransformersKwargs],
    ) -> Union[tuple, InternS1CausalLMOutputWithPast]:
        r"""
        Example:

        ```python
        >>> import torch
        >>> from transformers import AutoProcessor, AutoModelForImageTextToText

        >>> torch_device = "cuda"
        >>> processor = AutoProcessor.from_pretrained("internlm/Intern-S1")
        >>> model = AutoModelForImageTextToText.from_pretrained(
        ...     "internlm/Intern-S1", torch_dtype=torch.bfloat16, device_map=torch_device
        ... )

        >>> messages = [
        ...     {
        ...         "role": "user",
        ...         "content": [
        ...             {
        ...                 "type": "image",
        ...                 "url": "https://cdn.britannica.com/61/93061-050-99147DCE/Statue-of-Liberty-Island-New-York-Bay.jpg",
        ...             },
        ...             {
        ...                 "type": "image",
        ...                 "url": "https://thumbs.dreamstime.com/b/golden-gate-bridge-san-francisco-purple-flowers-california-echium-candicans-36805947.jpg",
        ...             },
        ...             {"type": "text", "text": "These images depict two different landmarks. Can you identify them?"},
        ...         ],
        ...     },
        ... ]

        >>> inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt").to(torch_device)
        >>> generate_ids = model.generate(**inputs, max_new_tokens=200)
        >>> print(processor.decode(generate_ids[0, inputs["input_ids"].shape[1] :], skip_special_tokens=True))
        ```"""
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        if self.is_moe_model:
            output_router_logits = (
                output_router_logits if output_router_logits is not None else self.config.text_config.output_router_logits
            )
            kwargs['output_router_logits'] = output_router_logits

        vision_feature_layer = (
            vision_feature_layer if vision_feature_layer is not None else self.config.vision_feature_layer
        )
        vision_feature_select_strategy = (
            vision_feature_select_strategy
            if vision_feature_select_strategy is not None
            else self.config.vision_feature_select_strategy
        )

        outputs = self.model(
            input_ids=input_ids,
            pixel_values=pixel_values,
            # InternTS
            time_series_signals=time_series_signals,
            x_lens=x_lens,
            # --
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            vision_feature_layer=vision_feature_layer,
            vision_feature_select_strategy=vision_feature_select_strategy,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_router_logits=output_router_logits,
            cache_position=cache_position,
            image_sizes=image_sizes,
            **kwargs,
        )

        hidden_states = outputs.last_hidden_state
        # Only compute necessary logits, and do not upcast them to float if we are not computing the loss
        slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
        logits = self.lm_head(hidden_states[:, slice_indices, :])

        loss = None
        if labels is not None:
            loss = self.loss_function(
                logits=logits, labels=labels, vocab_size=self.config.text_config.vocab_size, **kwargs
            )

        aux_loss = None
        if self.is_moe_model and output_router_logits and labels is not None:
            aux_loss = load_balancing_loss_func(
                outputs.router_logits,
                self.config.text_config.num_experts,
                self.config.text_config.num_experts_per_tok,
                attention_mask,
            )
            loss += self.config.text_config.router_aux_loss_coef * aux_loss.to(loss.device)

        return InternS1CausalLMOutputWithPast(
            loss=loss,
            aux_loss=aux_loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            router_logits=outputs.router_logits if self.is_moe_model else None,
            image_hidden_states=outputs.image_hidden_states,
            # InternTS
            time_series_hidden_states=outputs.time_series_hidden_states,
        )


__all__ = [
    "InternS1VisionPreTrainedModel",
    "InternS1VisionModel",
    "InternS1PreTrainedModel",
    "InternS1Model",
    "InternS1ForConditionalGeneration",
]
