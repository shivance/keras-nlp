from keras import ops

from keras_hub.src.api_export import keras_hub_export
from keras_hub.src.models.causal_lm import CausalLM
from keras_hub.src.models.gemma3.gemma3_backbone import Gemma3Backbone
from keras_hub.src.models.gemma3.gemma3_causal_lm_preprocessor import (
    Gemma3CausalLMPreprocessor,
)
from keras_hub.src.utils.tensor_utils import any_equal


@keras_hub_export("keras_hub.models.Gemma3CausalLM")
class Gemma3CausalLM(CausalLM):
    """An end-to-end multi modal Gemma3 model for causal language modeling.

    A causal language model (LM) predicts the next token based on previous
    tokens. This task setup can be used to train the model unsupervised on
    image and plain text input, or to autoregressively generate plain text
    similar to the data used for training.

    This model has a `generate()` method, which generates text based on a
    prompt. The generation strategy used is controlled by an additional
    `sampler` argument on `compile()`. You can recompile the model with
    different `keras_hub.samplers` objects to control the generation. By
    default, `"greedy"` sampling will be used.

    This model can optionally be configured with a `preprocessor` layer, in
    which case it will automatically apply preprocessing to string inputs during
    `fit()`, `predict()`, `evaluate()` and `generate()`. This is done by default
    when creating the model with `from_preset()`.

    Args:
        backbone: A `keras_hub.models.Gemma3Backbone` instance.
        preprocessor: A `keras_hub.models.Gemma3CausalLMPreprocessor` or
            `None`. If `None`, this model will not apply preprocessing, and
            inputs should be preprocessed before calling the model.
    """

    backbone_cls = Gemma3Backbone
    preprocessor_cls = Gemma3CausalLMPreprocessor

    def __init__(
        self,
        preprocessor,
        backbone,
        **kwargs,
    ):
        # === Layers ===
        self.preprocessor = preprocessor
        self.backbone = backbone

        # === Functional Model ===
        # This must be "backbone.input" i.e. the full input structure,
        # rather than "backbone.inputs" which is the flattened list of inputs.
        inputs = backbone.input
        hidden_state = backbone(inputs=inputs)
        outputs = backbone.token_embedding(hidden_state, reverse=True)

        super().__init__(
            inputs=inputs,
            outputs=outputs,
            **kwargs,
        )

    def compile(
        self,
        optimizer="auto",
        loss="auto",
        *,
        weighted_metrics="auto",
        sampler="greedy",
        **kwargs,
    ):
        super().compile(
            optimizer=optimizer,
            loss=loss,
            weighted_metrics=weighted_metrics,
            sampler=sampler,
            **kwargs,
        )

    def call_with_cache(
        self,
        token_ids,
        cache,
        cache_update_index,
        img_embeddings=None,
        text_mask=None,
        padding_mask=None,
        vision_indices=None,
    ):
        """Forward pass of `Gemma3CausalLM` with cache.

        `call_with_cache` adds an additional forward pass for the model for
        autoregressive inference. Unlike calling the model directly, this method
        allows caching previous key/value Tensors in multi-head attention layer,
        and avoids recomputing the outputs of seen tokens.

        Args:
            token_ids: a dense int Tensor with shape `(batch_size, max_length)`.
            cache: a dense float Tensor, the cache of key and value.
            cache_update_index: int, or int Tensor. The index of current inputs
                in the whole sequence.
            img_embeddings: a dense float Tensor with shape
                `(batch_size, image_sequence_length, hidden_dim)`.
            padding_mask: a dense int Tensor with shape
                `(batch_size, max_length)`.

        Returns:
            A (logits, hidden_states, cache) tuple. Where `logits` is the
            language model logits for the input token_ids, `hidden_states` is
            the final hidden representation of the input tokens, and `cache` is
            the decoding cache.
        """

        text_embeddings = self.backbone.token_embedding(token_ids)
        text_embeddings = text_embeddings * ops.cast(
            ops.sqrt(self.backbone.hidden_dim), text_embeddings.dtype
        )

        # Interleaving logic.
        ## == Interleaving text and images ==
        # Place the image embeddings in the right position in `text_embeddings`.
        if img_embeddings is not None:
            x = self.backbone.interleave_embeddings(
                image_embeddings=img_embeddings,
                text_embeddings=text_embeddings,
                vision_indices=vision_indices,
            )
        else:
            x = text_embeddings

        # Each decoder layer has a cache; we update them separately.
        caches = []
        for i, transformer_layer in enumerate(self.backbone.transformer_layers):
            current_cache = cache[:, i, ...]
            x, next_cache = transformer_layer(
                x,
                cache=current_cache,
                cache_update_index=cache_update_index,
                padding_mask=padding_mask,
                text_mask=text_mask,
            )
            caches.append(next_cache)
        cache = ops.stack(caches, axis=1)
        hidden_states = x = self.backbone.layer_norm(x)
        logits = self.backbone.token_embedding(x, reverse=True)
        return logits, hidden_states, cache

    def _build_cache(
        self,
        token_ids,
        img_embeddings,
        text_mask,
        padding_mask,
        vision_indices,
    ):
        """Build an empty cache for use with `call_with_cache()`."""
        batch_size = ops.shape(token_ids)[0]
        max_length = (
            ops.shape(token_ids)[1]
            # + self.backbone.image_sequence_length
        )
        num_layers = self.backbone.num_layers
        num_heads = self.backbone.num_key_value_heads
        head_dim = self.backbone.head_dim
        shape = [batch_size, num_layers, 2, max_length, num_heads, head_dim]
        cache = ops.zeros(shape, dtype=self.compute_dtype)
        # Seed the cache.
        logits, hidden_states, cache = self.call_with_cache(
            token_ids=token_ids,
            img_embeddings=img_embeddings,
            text_mask=text_mask,
            cache=cache,
            cache_update_index=0,
            padding_mask=padding_mask,
            vision_indices=vision_indices,
        )
        return hidden_states, cache

    def generate_step(self, inputs, stop_token_ids=[106]):
        """A compilable generation function for a single batch of inputs.

        This function represents the inner, XLA-compilable, generation function
        for a single batch of inputs. Inputs should have the same structure as
        model inputs, a dictionary with keys `"token_ids"` and `"padding_mask"`.

        Args:
            inputs: A dictionary with two keys `"token_ids"` and
                `"padding_mask"` and batched tensor values.
            stop_token_ids: Tuple of id's of end token's to stop on. If all
                sequences have produced a new stop token, generation
                will stop.
        """

        token_ids, padding_mask, images, text_mask, vision_indices = (
            inputs["token_ids"],
            inputs["padding_mask"],
            inputs.get("images", None),
            inputs.get("text_mask", None),
            inputs.get("vision_indices", None),
        )
        if not self.backbone.text_only_model:
            if len(ops.shape(images)) == 3:
                # Handle an unbatched image. Unlike `token_ids` and
                # `padding_mask` this will not automatically be upranked.
                images = ops.expand_dims(images, axis=0)
            img_embeddings = self.backbone.vision_encoder(images)
        else:
            img_embeddings = None
            text_mask = None
            vision_indices = None

        # Create and seed cache with a single forward pass.
        hidden_states, cache = self._build_cache(
            token_ids,
            img_embeddings,
            text_mask,
            padding_mask,
            vision_indices,
        )

        # Compute the lengths of all user inputted tokens ids.
        row_lengths = ops.sum(ops.cast(padding_mask, "int32"), axis=-1)
        # Start at the first index that has no user inputted id.
        index = ops.min(row_lengths)

        def next(prompt, cache, index):
            # The cache index is the index of our previous token.
            cache_update_index = index - 1
            batch_size = ops.shape(prompt)[0]
            prompt = ops.slice(prompt, [0, index - 1], [batch_size, 1])
            logits, hidden_states, cache = self.call_with_cache(
                token_ids=prompt,
                cache=cache,
                cache_update_index=cache_update_index,
            )
            return (
                ops.squeeze(logits, axis=1),
                ops.squeeze(hidden_states, axis=1),
                cache,
            )

        token_ids = self.sampler(
            next=next,
            prompt=token_ids,
            cache=cache,
            index=index,
            mask=padding_mask,
            stop_token_ids=stop_token_ids,
            hidden_states=hidden_states,
            model=self,
        )

        # Compute an output padding mask with the token ids we updated.
        if stop_token_ids is not None:
            # Build a mask of `stop_token_ids` locations not in the original
            # prompt (not in locations where `padding_mask` is True).
            end_locations = any_equal(
                token_ids, stop_token_ids, ops.logical_not(padding_mask)
            )

            end_locations = ops.cast(end_locations, "int32")
            # Use cumsum to get ones in all locations after end_locations.
            cumsum = ops.cast(ops.cumsum(end_locations, axis=-1), "int32")
            overflow = cumsum - end_locations
            # Our padding mask is the inverse of these overflow locations.
            padding_mask = ops.logical_not(ops.cast(overflow, "bool"))
        else:
            # Without early stopping, all locations will have been updated.
            padding_mask = ops.ones_like(token_ids, dtype="bool")
        return {
            "token_ids": token_ids,
            "padding_mask": padding_mask,
            "images": images,
        }

    def generate(
        self,
        inputs,
        max_length=None,
        stop_token_ids="auto",
        strip_prompt=False,
    ):
        # If `auto`, add `<end_of_turn>` as a stop token too.
        if self.preprocessor is None and stop_token_ids == "auto":
            raise ValueError(
                "A `preprocessor` must be attached to the model if "
                '`stop_token_ids="auto"`. Currently `preprocessor=None`. To '
                "call `generate()` with preprocessing detached, either pass "
                "`stop_token_ids=None` to always generate until `max_length` "
                "or pass a tuple of token ids that should terminate generation "
                "as `stop_token_ids`."
            )
        elif stop_token_ids == "auto":
            stop_token_ids = [
                self.preprocessor.tokenizer.end_token_id,
                self.preprocessor.tokenizer.token_to_id("<end_of_turn>"),
            ]

        return super().generate(
            inputs,
            max_length=max_length,
            stop_token_ids=stop_token_ids,
            strip_prompt=strip_prompt,
        )
