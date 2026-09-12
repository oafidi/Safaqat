"""Figure captioning with a small vision-language model.

Only figures whose class implies real information content are captioned (charts,
diagrams, maps, photos, screenshots). Logos, icons and barcodes are not: a
caption for them is noise in a retrieval index.

Qwen2.5-VL-3B needs ~7.5 GB in bf16, which does not fit alongside anything else
on a 6 GB card, so it is loaded with 4-bit NF4 weights (~2.4 GB, ~3.5 GB peak
during generation). It is also loaded *after* PaddleOCR-VL has been released,
never at the same time -- see :class:`docparser.parser.DocumentParser`.
"""

from __future__ import annotations

import gc

from PIL import Image

_PROMPT = (
    "Describe this figure from a technical document in one or two sentences. "
    "State what it shows, and include any labels, axis names, units or values "
    "that carry meaning. Do not speculate about anything you cannot see."
)

# Below this, an image is too small to hold legible content; upscaling gives the
# VLM something to work with rather than a handful of pixels.
_MIN_SIDE = 64


class Captioner:
    """Lazily-loaded VLM captioner that can be unloaded to free VRAM."""

    def __init__(
        self,
        model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct",
        load_in_4bit: bool = True,
        max_new_tokens: int = 96,
    ):
        self.model_id = model_id
        self.load_in_4bit = load_in_4bit
        self.max_new_tokens = max_new_tokens
        self._model = None
        self._processor = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return

        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        kwargs: dict = {"dtype": torch.float16}
        if self.load_in_4bit and torch.cuda.is_available():
            from transformers import BitsAndBytesConfig

            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
            kwargs["device_map"] = "cuda:0"
        elif torch.cuda.is_available():
            kwargs["device_map"] = "cuda:0"

        self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(self.model_id, **kwargs)
        self._model.eval()
        self._processor = AutoProcessor.from_pretrained(self.model_id)

    def unload(self) -> None:
        """Release the model and give the VRAM back."""
        import torch

        self._model = None
        self._processor = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def caption(self, image: Image.Image) -> str:
        """Describe one figure. Returns "" if the model produces nothing."""
        import torch

        self.load()
        assert self._model is not None and self._processor is not None

        img = image.convert("RGB")
        if min(img.size) < _MIN_SIDE:
            scale = _MIN_SIDE / min(img.size)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

        messages = [
            {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": _PROMPT}]}
        ]
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self._processor(text=[text], images=[img], return_tensors="pt").to(
            self._model.device
        )

        with torch.inference_mode():
            generated = self._model.generate(
                **inputs, max_new_tokens=self.max_new_tokens, do_sample=False
            )

        trimmed = [out[len(inp) :] for inp, out in zip(inputs.input_ids, generated)]
        decoded = self._processor.batch_decode(trimmed, skip_special_tokens=True)
        return decoded[0].strip() if decoded else ""

    def __enter__(self) -> "Captioner":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        self.unload()
