import numpy as np

from keras_hub.src.models.qwen.qwen_backbone import QwenBackbone
from keras_hub.src.utils.preset_utils import load_json

backbone_cls = QwenBackbone


def convert_backbone_config(transformers_config):
    return {
        "vocabulary_size": transformers_config["vocab_size"],
        "hidden_dim": transformers_config["hidden_size"],
        "num_layers": transformers_config["num_hidden_layers"],
        "num_query_heads": transformers_config["num_attention_heads"],
        "num_key_value_heads": transformers_config["num_key_value_heads"],
        "intermediate_dim": transformers_config["intermediate_size"],
        "moe_intermediate_dim": transformers_config["moe_intermediate_size"],
        "shared_expert_intermediate_dim": transformers_config[
            "shared_expert_intermediate_size"
        ],
        "num_experts": transformers_config["num_experts"],
        "top_k": transformers_config["num_experts_per_tok"],
        "norm_topk_prob": transformers_config["norm_topk_prob"],
        "decoder_sparse_step": transformers_config["decoder_sparse_step"],
        "layer_norm_epsilon": transformers_config["rms_norm_eps"],
        "rope_max_wavelength": transformers_config["rope_theta"],
        "use_sliding_window": transformers_config["use_sliding_window"],
        "sliding_window_size": transformers_config["sliding_window"],
        "output_router_logits": transformers_config["output_router_logits"],
    }


def convert_weights(backbone, loader, transformers_config):
    loader.port_weight(
        keras_variable=backbone.get_layer("token_embedding").embeddings,
        hf_weight_key="model.embed_tokens.weight",
    )
    if not backbone.tie_word_embeddings:
        loader.port_weight(
            keras_variable=backbone.get_layer(
                "token_embedding"
            ).reverse_embeddings,
            hf_weight_key="lm_head.weight",
            # rearrange_pattern="b a -> a b",
            hook_fn=lambda hf_tensor, _: np.transpose(hf_tensor, axes=(1, 0)),
        )

    def transpose_and_reshape(x, shape):
        return np.reshape(np.transpose(x), shape)

    for i in range(backbone.num_layers):
        decoder_layer = backbone.get_layer(f"transformer_layer_{i}")

        # Input layernorm
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layernorm.scale,
            hf_weight_key=f"model.layers.{i}.input_layernorm.weight",
        )

        # Attention layers

        ## Query
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._query_dense.kernel,
            hf_weight_key=f"model.layers.{i}.self_attn.q_proj.weight",
            hook_fn=transpose_and_reshape,
        )
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._query_dense.bias,
            hf_weight_key=f"model.layers.{i}.self_attn.q_proj.bias",
            hook_fn=transpose_and_reshape,
        )
        ## Key
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._key_dense.kernel,
            hf_weight_key=f"model.layers.{i}.self_attn.k_proj.weight",
            hook_fn=transpose_and_reshape,
        )
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._key_dense.bias,
            hf_weight_key=f"model.layers.{i}.self_attn.k_proj.bias",
            hook_fn=transpose_and_reshape,
        )
        ## Value
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._value_dense.kernel,
            hf_weight_key=f"model.layers.{i}.self_attn.v_proj.weight",
            hook_fn=transpose_and_reshape,
        )
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._value_dense.bias,
            hf_weight_key=f"model.layers.{i}.self_attn.v_proj.bias",
            hook_fn=transpose_and_reshape,
        )
        ## Output
        loader.port_weight(
            keras_variable=decoder_layer._self_attention_layer._output_dense.kernel,
            hf_weight_key=f"model.layers.{i}.self_attn.o_proj.weight",
            # rearrange_patterns="c (a b) -> a b c",
            # rearrange_dims={"a": backbone.num_query_heads},
            hook_fn=transpose_and_reshape,
        )

        # MLP layers
        if (
            (i not in backbone.mlp_only_layers)
            and backbone.num_experts > 0
            and ((i + 1) % backbone.decoder_parse_step == 0)
        ):
            # MoE layers
            loader.port_weight(
                keras_variable=decoder_layer._mlp.gate_proj.kernel,
                hf_weight_key=f"model.layers.{i}.block.mlp.gate_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )
            for expert_idx in range(backbone.num_experts):
                loader.port_weight(
                    keras_variable=decoder_layer.mlp.experts[
                        expert_idx
                    ]._feedforward_intermediate_dense.kernel,
                    hf_weight_key=f"model.layers.{i}.block.{expert_idx}.mlp.up_proj.weight",
                    # rearrange_patterns="b a -> a b",
                    hook_fn=lambda hf_tensor, _: np.transpose(
                        hf_tensor, axes=(1, 0)
                    ),
                )
                loader.port_weight(
                    keras_variable=decoder_layer.mlp.experts[
                        expert_idx
                    ]._feedforward_output_dense.kernel,
                    hf_weight_key=f"model.layers.{i}.block.{expert_idx}.mlp.down_proj.weight",
                    # rearrange_patterns="b a -> a b",
                    hook_fn=lambda hf_tensor, _: np.transpose(
                        hf_tensor, axes=(1, 0)
                    ),
                )
                loader.port_weight(
                    keras_variable=decoder_layer.mlp.experts[
                        expert_idx
                    ]._feedforward_gate_dense.kernel,
                    hf_weight_key=f"model.layers.{i}.block.{expert_idx}.mlp.gate_proj.weight",
                    # rearrange_patterns="b a -> a b",
                    hook_fn=lambda hf_tensor, _: np.transpose(
                        hf_tensor, axes=(1, 0)
                    ),
                )

                # Feedforward layernorm
                loader.port_weight(
                    keras_variable=decoder_layer.mlp.experts[
                        expert_idx
                    ]._feedforward_layernorm.scale,
                    hf_weight_key=f"model.layers.{i}.block.{expert_idx}.post_attention_layernorm.weight",
                )

            loader.port_weight(
                keras_variable=decoder_layer.mlp.shared_expert._feedforward_intermediate_dense.kernel,
                hf_weight_key=f"model.layers.{i}.block.mlp.shared_expert.up_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )
            loader.port_weight(
                keras_variable=decoder_layer.mlp.shared_expert._feedforward_output_dense.kernel,
                hf_weight_key=f"model.layers.{i}.block.mlp.shared_expert.shared_expert.down_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )
            loader.port_weight(
                keras_variable=decoder_layer.mlp.shared_expert._feedforward_gate_dense.kernel,
                hf_weight_key=f"model.layers.{i}.block.mlp.shared_expert.gate_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )

            # Feedforward layernorm
            loader.port_weight(
                keras_variable=decoder_layer.mlp[
                    expert_idx
                ]._feedforward_layernorm.scale,
                hf_weight_key=f"model.layers.{i}.block.mlp.shared_expert.post_attention_layernorm.weight",
            )

        else:
            loader.port_weight(
                keras_variable=decoder_layer._feedforward_intermediate_dense.kernel,
                hf_weight_key=f"model.layers.{i}.mlp.up_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )
            loader.port_weight(
                keras_variable=decoder_layer._feedforward_output_dense.kernel,
                hf_weight_key=f"model.layers.{i}.mlp.down_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )
            loader.port_weight(
                keras_variable=decoder_layer._feedforward_gate_dense.kernel,
                hf_weight_key=f"model.layers.{i}.mlp.gate_proj.weight",
                # rearrange_patterns="b a -> a b",
                hook_fn=lambda hf_tensor, _: np.transpose(
                    hf_tensor, axes=(1, 0)
                ),
            )

            # Feedforward layernorm
            loader.port_weight(
                keras_variable=decoder_layer._feedforward_layernorm.scale,
                hf_weight_key=f"model.layers.{i}.post_attention_layernorm.weight",
            )

    # Final normalization layer
    loader.port_weight(
        keras_variable=backbone.get_layer("sequence_output_layernorm").scale,
        hf_weight_key="model.norm.weight",
    )

    return backbone


def convert_tokenizer(cls, preset, **kwargs):
    tokenizer_config = load_json(preset, "tokenizer.json")
    vocab = tokenizer_config["model"]["vocab"]
    merges = tokenizer_config["model"]["merges"]

    # Load all special tokens with the exception of "reserved" ones.
    special_tokens = set()
    for token in tokenizer_config["added_tokens"]:
        if not token["content"].startswith("<|reserved_special_token_"):
            vocab[token["content"]] = token["id"]
            special_tokens.add(token["content"])

    kwargs.update(
        {
            "unsplittable_tokens": list(special_tokens),
        }
    )

    return cls(vocabulary=vocab, merges=merges, **kwargs)
