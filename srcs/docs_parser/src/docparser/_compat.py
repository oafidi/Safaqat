"""Environment fixes that must run before PaddlePaddle is imported.

Two upstream incompatibilities break PaddleOCR-VL in this environment. Both are
patched here, and :func:`apply` is called from ``docparser/__init__.py``, so
importing the package is enough to make Paddle usable.

1. Missing ``libnvToolsExt.so.1``
   ``paddlepaddle-gpu==3.1.0`` is built against CUDA 12.3 and links the legacy
   NVTX library. Current ``nvidia-nvtx-cu12`` wheels no longer ship that ``.so``
   (only the nvtx3 interop shim), so ``import paddle`` fails with an ImportError
   before it ever reaches the GPU. Loading the library with ``RTLD_GLOBAL`` up
   front puts its symbols in the process's global namespace; the dynamic loader
   then resolves ``libpaddle.so``'s ``DT_NEEDED`` entry against the already
   loaded object instead of searching the filesystem.

2. bfloat16 safetensors cannot be read into Paddle
   PaddleOCR-VL's weights are bf16. PaddleX reads them with
   ``safe_open(..., framework="paddle")``, but safetensors' paddle bridge
   materialises tensors through NumPy, which has no bf16 dtype, so every read
   raises ``TypeError: data type 'bfloat16' not understood``. No released
   safetensors version avoids this: <=0.6.2 rejects ``framework="paddle"``
   outright, and >=0.7.0 accepts it but still routes through NumPy.

   Torch reads bf16 safetensors natively, so we serve the ``paddle`` framework
   from a torch-backed reader and reinterpret the raw bits as Paddle bf16
   (uint16 view -> ``paddle.view``). The bit pattern is copied unchanged, so
   weights are bit-for-bit identical to the checkpoint: no cast, no precision
   loss. Any other framework is delegated to the original implementation.
"""

from __future__ import annotations

import ctypes
import glob
import os
import sys

_applied = False


# --------------------------------------------------------------------------
# 1. CUDA library preloads
# --------------------------------------------------------------------------
# Each entry is a library whose symbols some native extension needs but cannot
# find, because the wheel that provides it is not on the loader's search path.
#
#   libnvToolsExt.so.1  -> paddlepaddle-gpu 3.1.0 (CUDA 12.3 build), see above.
#   libnvJitLink.so.13  -> bitsandbytes, used to 4-bit quantise the captioner.
#                          Torch preloads it privately; bitsandbytes' native
#                          library does not, so it fails with
#                          "CUDA 13.x runtime libraries were not found".
_PRELOAD_LIBS = ("libnvToolsExt.so.1", "libnvJitLink.so.13")


def _site_packages() -> list[str]:
    dirs = [p for p in sys.path if p.endswith("site-packages")]
    guess = os.path.join(
        sys.prefix,
        "lib",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
        "site-packages",
    )
    if os.path.isdir(guess) and guess not in dirs:
        dirs.append(guess)
    return dirs


def _preload_cuda_libs() -> None:
    """dlopen CUDA libraries with RTLD_GLOBAL so native extensions resolve them.

    A library already loaded into the global namespace satisfies any later
    DT_NEEDED entry with the same SONAME, so the extension links against it
    instead of searching the filesystem and failing.
    """
    if os.environ.get("DOCPARSER_SKIP_CUDA_PRELOAD"):
        return
    for soname in _PRELOAD_LIBS:
        candidates: list[str] = []
        for base in _site_packages():
            candidates.extend(
                glob.glob(os.path.join(base, "nvidia", "**", soname), recursive=True)
            )
        candidates.append(soname)  # fall back to the loader's default search path
        for so in candidates:
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                continue
        # If none loaded we stay quiet: a CPU-only install does not need these,
        # and the consumer raises a clearer error than we could here.


# --------------------------------------------------------------------------
# 2. bf16-capable safetensors reader for the "paddle" framework
# --------------------------------------------------------------------------
def _torch_to_paddle(tensor):
    """Convert a torch tensor to Paddle, preserving bf16 bit-exactly.

    The result is pinned to host memory. ``paddle.to_tensor`` would otherwise
    allocate on Paddle's current device -- the GPU -- and the whole checkpoint
    would land in VRAM the moment it is read, regardless of what the caller
    asked for. Leaving it on the host lets PaddleX decide when (and whether) to
    move each tensor across.
    """
    import paddle
    import torch

    tensor = tensor.detach().cpu().contiguous()
    cpu = paddle.CPUPlace()
    if tensor.dtype is torch.bfloat16:
        # NumPy has no bf16, so move the raw 16-bit words across and tell Paddle
        # to reinterpret them. paddle.view is a bitcast, not a conversion.
        as_u16 = tensor.view(torch.uint16).numpy()
        return paddle.view(paddle.to_tensor(as_u16, place=cpu), paddle.bfloat16)
    return paddle.to_tensor(tensor.numpy(), place=cpu)


class _PaddleSlice:
    """Mimics safetensors' PySafeSlice, but yields Paddle tensors."""

    def __init__(self, slice_):
        self._slice = slice_

    def get_shape(self):
        return self._slice.get_shape()

    def get_dtype(self):
        return self._slice.get_dtype()

    @property
    def shape(self):
        return self._slice.get_shape()

    def __getitem__(self, item):
        return _torch_to_paddle(self._slice[item])


class _PaddleSafeOpen:
    """safe_open(framework="paddle") served from the torch backend."""

    def __init__(self, filename, **kwargs):
        from safetensors import safe_open as _orig

        kwargs.pop("framework", None)
        self._f = _orig(filename, framework="pt", **kwargs)

    def __enter__(self):
        self._f.__enter__()
        return self

    def __exit__(self, *exc):
        return self._f.__exit__(*exc)

    def keys(self):
        return self._f.keys()

    def metadata(self):
        return self._f.metadata()

    def get_slice(self, name):
        return _PaddleSlice(self._f.get_slice(name))

    def get_tensor(self, name):
        return _torch_to_paddle(self._f.get_tensor(name))


def _patch_safetensors() -> None:
    import safetensors

    if getattr(safetensors.safe_open, "_docparser_patched", False):
        return

    original = safetensors.safe_open

    def safe_open(filename, framework="pt", device="cpu", **kwargs):
        if framework == "paddle":
            return _PaddleSafeOpen(filename, device=device, **kwargs)
        return original(filename, framework=framework, device=device, **kwargs)

    safe_open._docparser_patched = True  # type: ignore[attr-defined]
    safe_open._original = original  # type: ignore[attr-defined]
    safetensors.safe_open = safe_open

    # PaddleX does `from safetensors import safe_open` *inside* the function that
    # loads weights, so rebinding the attribute on the module is enough: the
    # import runs after this patch and picks up our version.


# --------------------------------------------------------------------------
# 3. Build the VL model in bf16 instead of fp32 (the actual OOM)
# --------------------------------------------------------------------------
def _cast_to_bfloat16_via_host(model) -> None:
    """Cast a GPU model to bf16 without ever holding both copies in VRAM.

    ``Layer.to(dtype=...)`` builds the bf16 parameters while the fp32 ones are
    still resident, so the conversion alone peaks at fp32 + bf16 (~3.2 + 1.6 GB).
    Moving the model to host memory first means the fp32 copy is freed before
    the bf16 copy is made, and only the result travels back to the GPU. Slower
    by a couple of seconds, and worth it: it is the difference between a ~5.0 GB
    and a ~3.3 GB peak on a 6 GB card.
    """
    import paddle

    try:
        place = next(iter(model.parameters())).place
        on_gpu = place.is_gpu_place()
    except (StopIteration, AttributeError):
        on_gpu = False

    if not on_gpu:
        model.to(dtype=paddle.bfloat16)
        return

    model.to(device=paddle.CPUPlace())
    paddle.device.cuda.empty_cache()  # hand the fp32 parameters back
    model.to(dtype=paddle.bfloat16)
    model.to(device=paddle.CUDAPlace(0))


def _patch_dtype_conversion() -> None:
    """Match the model's parameter dtype to the checkpoint, not the reverse.

    PaddleOCR-VL's checkpoint is bf16 for every one of its 620 tensors, and
    PaddleX asks for a bf16 model. But its ``dtype_guard`` does not reach every
    submodule, so the model is built with ~455 of those parameters in fp32.
    PaddleX then reconciles the mismatch in the wrong direction: it casts the
    incoming bf16 weights *up* to fp32 to match the model. On a 6 GB card that
    is fatal -- fp32 parameters alone are ~3.6 GB, and with the bf16 state dict
    still resident the load needs ~5.5 GB and dies with

        ResourceExhaustedError: Cannot allocate 144MB ... available 137MB

    The checkpoint is the ground truth here, so we cast the *model* down to
    bf16 first. Then dtypes already agree, PaddleX's cast is a no-op, and the
    model loads in ~1.9 GB.
    """
    from paddlex.inference.models.common.transformers.transformers import model_utils

    if getattr(model_utils._convert_state_dict_dtype_and_shape, "_docparser_patched", False):
        return

    import collections

    import paddle

    original = model_utils._convert_state_dict_dtype_and_shape

    def _convert_state_dict_dtype_and_shape(state_dict, model_to_load, convert_from_hf):
        dtypes = collections.Counter(
            str(v.dtype) for v in state_dict.values() if hasattr(v, "dtype")
        )
        if dtypes and dtypes.most_common(1)[0][0] == "paddle.bfloat16":
            _cast_to_bfloat16_via_host(model_to_load)
        return original(state_dict, model_to_load, convert_from_hf)

    _convert_state_dict_dtype_and_shape._docparser_patched = True  # type: ignore[attr-defined]
    model_utils._convert_state_dict_dtype_and_shape = _convert_state_dict_dtype_and_shape

    # Keep the checkpoint on the host while it is being loaded.
    #
    # PaddleX copies all 620 weight tensors onto the GPU, then copies them again
    # into the model's parameters and frees them -- so for the duration of the
    # load the checkpoint occupies 1.8 GB of VRAM purely in transit, on top of
    # the model itself. Measured at the moment of loading: 5.03 GB in use, of
    # which 1.8 GB is this staging copy. Leaving the tensors in host memory and
    # letting the parameter assignment do the host-to-device copy costs nothing
    # and takes the peak down to ~3.3 GB, which is what buys the headroom for
    # this to run on a 6 GB card that is also driving a display.
    original_load_part = model_utils._load_part_state_dict_from_safetensors

    def _load_part_state_dict_from_safetensors(*args, **kwargs):
        if len(args) >= 5:  # device is the 5th positional parameter
            args = (*args[:4], "cpu", *args[5:])
        else:
            kwargs["device"] = "cpu"
        return original_load_part(*args, **kwargs)

    model_utils._load_part_state_dict_from_safetensors = _load_part_state_dict_from_safetensors


def apply() -> None:
    """Idempotently apply the environment fixes. Safe to call repeatedly."""
    global _applied
    if _applied:
        return
    _preload_cuda_libs()
    _patch_safetensors()
    _applied = True


def apply_paddlex_patches() -> None:
    """Apply fixes that need PaddleX imported. Call before building a pipeline.

    Kept separate from :func:`apply` so that merely importing ``docparser`` does
    not drag in PaddleX (a multi-second import).
    """
    apply()
    _patch_dtype_conversion()
