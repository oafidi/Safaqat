"""Figure classification: DocumentFigureClassifier-v2.5, on the CPU.

EfficientNet-B0 at 224x224 is ~20 MB and classifies a crop in milliseconds on a
CPU. Keeping it off the GPU is deliberate: the GPU budget belongs to
PaddleOCR-VL and the captioner, and this model would gain nothing from being
there.

A note on the backend. The model card ships a ``model.onnx``, and ONNX Runtime
is the obvious way to run a 20 MB classifier on CPU -- but *that export is
broken*. Its output barely depends on its input: feeding zeros, ones and random
noise produces the same logits to within 0.013, and every image comes back as a
near-uniform distribution over the 26 classes (top class ~0.09, against 1/26 =
0.038 for pure chance). The safetensors weights, run through transformers, give
a confident and correct answer on the same crop (a header logo: ``logo``, p=1.00
via safetensors, versus ``table``, p=0.09 via ONNX).

So the default backend is torch-on-CPU, which is correct. ONNX remains
selectable, and :meth:`_onnx_is_sane` probes the session at load time: if the
export is ever fixed upstream, ``backend="onnx"`` starts working with no code
change, and until then it refuses to silently return garbage.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from .config import CAPTION_CLASSES, SIGNATURE_CLASSES

log = logging.getLogger("docparser.classifier")

# From the model's preprocessor_config.json. The std is not the usual ImageNet
# triple, so it is spelled out rather than assumed.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.47853944, 0.4732864, 0.47434163], dtype=np.float32)
_SIZE = (224, 224)


class FigureClassifier:
    """Classifies a figure crop into one of 26 document-figure classes."""

    def __init__(
        self,
        model_id: str = "docling-project/DocumentFigureClassifier-v2.5",
        backend: str = "torch",
        num_threads: int = 4,
    ):
        self.model_id = model_id
        self.num_threads = num_threads
        self.backend = backend

        if backend == "onnx" and self._load_onnx():
            return
        if backend == "onnx":
            log.warning(
                "The ONNX export of %s is not usable (its output does not depend on "
                "its input); falling back to the torch CPU backend.",
                model_id,
            )
        self.backend = "torch"
        self._load_torch()

    # -- backends ---------------------------------------------------------
    def _load_torch(self) -> None:
        import torch
        from transformers import EfficientNetForImageClassification

        torch.set_num_threads(self.num_threads)
        self._model = EfficientNetForImageClassification.from_pretrained(self.model_id)
        self._model.eval()
        self._model.to("cpu")  # explicitly: never take VRAM from the VL models
        id2label = self._model.config.id2label
        self.labels = [id2label[i] for i in range(len(id2label))]

    def _load_onnx(self) -> bool:
        """Load the ONNX session. Returns False if the export is unusable."""
        try:
            import json

            import onnxruntime as ort
            from huggingface_hub import snapshot_download

            path = Path(snapshot_download(self.model_id, allow_patterns=["*.json", "*.onnx"]))
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.intra_op_num_threads = self.num_threads

            self._session = ort.InferenceSession(
                str(path / "model.onnx"),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name

            id2label = json.loads((path / "config.json").read_text())["id2label"]
            self.labels = [id2label[str(i)] for i in range(len(id2label))]
        except Exception as exc:  # noqa: BLE001 - any failure means: use torch
            log.warning("Could not load the ONNX backend (%s); using torch.", exc)
            return False

        return self._onnx_is_sane()

    def _onnx_is_sane(self) -> bool:
        """Check the session actually responds to its input.

        A working classifier gives very different logits for zeros and for
        random noise. The current published export does not, and would quietly
        mislabel every figure in the document.
        """
        zeros = np.zeros((1, 3, *_SIZE), dtype=np.float32)
        noise = np.random.default_rng(0).standard_normal((1, 3, *_SIZE)).astype(np.float32)
        a = self._session.run(None, {self._input_name: zeros})[0]
        b = self._session.run(None, {self._input_name: noise})[0]
        return bool(np.abs(a - b).max() > 0.1)

    # -- inference --------------------------------------------------------
    def _preprocess(self, image: Image.Image) -> np.ndarray:
        img = image.convert("RGB").resize(_SIZE, Image.BILINEAR)  # resample=2
        arr = np.asarray(img, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        return np.transpose(arr, (2, 0, 1))[None]  # NCHW

    def classify(self, image: Image.Image) -> tuple[str, float]:
        """Return ``(label, confidence)`` for the most likely class."""
        return self.classify_batch([image])[0]

    def classify_batch(self, images: list[Image.Image]) -> list[tuple[str, float]]:
        """Classify several crops at once."""
        if not images:
            return []

        batch = np.concatenate([self._preprocess(im) for im in images], axis=0)

        if self.backend == "onnx":
            logits = self._session.run(None, {self._input_name: batch})[0]
        else:
            import torch

            with torch.inference_mode():
                logits = self._model(torch.from_numpy(batch)).logits.numpy()

        exp = np.exp(logits - logits.max(axis=1, keepdims=True))  # stable softmax
        probs = exp / exp.sum(axis=1, keepdims=True)

        out: list[tuple[str, float]] = []
        for row in probs:
            idx = int(row.argmax())
            out.append((self.labels[idx], float(row[idx])))
        return out

    # -- routing policy ---------------------------------------------------
    @staticmethod
    def should_caption(label: str) -> bool:
        """Whether a figure of this class holds information worth describing."""
        return label in CAPTION_CLASSES

    @staticmethod
    def is_signature(label: str) -> bool:
        """Whether this class means the document is signed."""
        return label in SIGNATURE_CLASSES
