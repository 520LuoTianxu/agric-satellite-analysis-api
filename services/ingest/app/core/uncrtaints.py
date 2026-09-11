"""Thin UnCRtainTS inference wrapper (optional torch + external checkpoint).

Does not vendor PatrickTUM/UnCRtainTS or commit multi-100MB weights.
Set ``UNCRTAINTS_HOME`` to a clone and ``UNCRTAINTS_CHECKPOINT_DIR`` to the
``diagonal_1`` (use_sar, input_t=3) folder. ``DECLOUD_BACKEND=dummy`` runs a
CPU smoke path that does not need weights.

Expected tensor layout (SEN12MS-CR / UnCRtainTS issue #3):
  real_A = concat(S1[T,2], S2[T,13]) * 10   # [B, T, 15, H, W]
  mean   = output[:, :, :13] / 10           # reconstructed S2 in [0, 1]
L2A has no cirrus band: B10 is inserted as zeros.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import structlog

from app.core.decloud import (
    decloud_backend,
    decloud_input_t,
    decloud_use_sar,
    uncrtaints_checkpoint_dir,
    uncrtaints_checkpoint_name,
    uncrtaints_home,
)

logger = structlog.get_logger()

S2_L2A_BANDS = (
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B10",
    "B11",
    "B12",
)
S2_L2A_NO_B10 = tuple(b for b in S2_L2A_BANDS if b != "B10")
S1_LAUNCH_ORDINAL = date(2014, 4, 3).toordinal()

# Element84 sentinel-2-l2a asset names (primary, fallback).
S2_L2A_ASSET_MAP: dict[str, tuple[str, ...]] = {
    "B01": ("coastal", "B01"),
    "B02": ("blue", "B02"),
    "B03": ("green", "B03"),
    "B04": ("red", "B04"),
    "B05": ("rededge1", "B05"),
    "B06": ("rededge2", "B06"),
    "B07": ("rededge3", "B07"),
    "B08": ("nir", "B08"),
    "B8A": ("nir08", "B8A", "B8A"),
    "B09": ("nir09", "B09"),
    "B11": ("swir16", "B11"),
    "B12": ("swir22", "B12"),
}

MS_CLIP_MAX = 10000.0
SAR_DB_MIN = -25.0
SAR_DB_MAX = 0.0


class DecloudUnavailable(RuntimeError):
    """Weights, torch, or the UnCRtainTS package are missing."""


class Inferencer(Protocol):
    def reconstruct(
        self,
        s2_stack: np.ndarray,
        s1_stack: np.ndarray | None,
        date_ordinals: list[int],
    ) -> np.ndarray:
        """Return reconstructed S2 (13, H, W) in [0, 1] reflectance."""


def pick_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def process_ms_default(img: np.ndarray) -> np.ndarray:
    """UnCRtainTS ``process_MS(..., 'default')``: clip 0-10000, map to [0, 1]."""
    clipped = np.clip(img.astype(np.float32), 0.0, MS_CLIP_MAX)
    return clipped / MS_CLIP_MAX


def process_sar_default(img: np.ndarray) -> np.ndarray:
    """UnCRtainTS ``process_SAR(..., 'default')``: clip dB [-25, 0], map to [0, 1]."""
    clipped = np.clip(img.astype(np.float32), SAR_DB_MIN, SAR_DB_MAX)
    return (clipped - SAR_DB_MIN) / (SAR_DB_MAX - SAR_DB_MIN)


def to_ms_dn(band: np.ndarray) -> np.ndarray:
    """Map rasterio window values to the 0-10000 DN range UnCRtainTS expects."""
    arr = band.astype(np.float32)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros_like(arr, dtype=np.float32)
    peak = float(np.nanpercentile(finite, 99.5))
    if peak <= 1.5:
        return np.clip(arr, 0.0, 1.0) * MS_CLIP_MAX
    return np.clip(arr, 0.0, MS_CLIP_MAX)


def stack_s2_13(bands: dict[str, np.ndarray]) -> np.ndarray:
    """(13, H, W) DN stack; missing B10 is zeros."""
    sample = next(iter(bands.values()))
    h, w = sample.shape
    out = np.zeros((len(S2_L2A_BANDS), h, w), dtype=np.float32)
    for i, name in enumerate(S2_L2A_BANDS):
        if name == "B10":
            continue
        src = bands.get(name)
        if src is None:
            continue
        out[i] = to_ms_dn(src)
    return out


def pad_hw(
    arr: np.ndarray, min_hw: int = 32, multiple: int = 16
) -> tuple[np.ndarray, tuple[int, int]]:
    """Pad (..., H, W) to at least min_hw and a multiple of ``multiple``."""
    h, w = arr.shape[-2], arr.shape[-1]
    th = max(min_hw, ((h + multiple - 1) // multiple) * multiple)
    tw = max(min_hw, ((w + multiple - 1) // multiple) * multiple)
    if th == h and tw == w:
        return arr, (h, w)
    pad_h, pad_w = th - h, tw - w
    pads = [(0, 0)] * (arr.ndim - 2) + [(0, pad_h), (0, pad_w)]
    padded = (
        np.pad(arr, pads, mode="reflect")
        if min(h, w) >= 2
        else np.pad(arr, pads, mode="constant")
    )
    return padded.astype(arr.dtype, copy=False), (h, w)


def crop_hw(arr: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    h, w = hw
    return arr[..., :h, :w]


class DummyInferencer:
    """CPU smoke stand-in: darken RGB a bit and lift NIR (no neural net)."""

    def reconstruct(
        self,
        s2_stack: np.ndarray,
        s1_stack: np.ndarray | None,
        date_ordinals: list[int],
    ) -> np.ndarray:
        del s1_stack, date_ordinals
        # s2_stack: (T, 13, H, W) in DN 0-10000; use last (target) step.
        target = s2_stack[-1].astype(np.float32) / MS_CLIP_MAX
        out = target.copy()
        # B02, B03, B04 indices 1,2,3
        out[1:4] = np.clip(out[1:4] * 0.78, 0.0, 1.0)
        # B08 index 7
        out[7] = np.clip(out[7] * 1.08 + 0.04, 0.0, 1.0)
        return out


class UncrtainTSInferencer:
    def __init__(self, device: str | None = None) -> None:
        self.device = device or pick_device()
        self.model = self._load()

    def _load(self) -> Any:
        try:
            import torch
        except ImportError as exc:
            raise DecloudUnavailable(
                "torch is not installed; install it on GPU hosts or set "
                "DECLOUD_BACKEND=dummy for smoke tests"
            ) from exc

        home = uncrtaints_home()
        if home:
            import sys

            home_path = str(Path(home).resolve())
            if home_path not in sys.path:
                sys.path.insert(0, home_path)
            model_dir = str(Path(home) / "model")
            if Path(model_dir).is_dir() and model_dir not in sys.path:
                sys.path.insert(0, model_dir)

        try:
            from src.backbones.uncrtaints import UNCRTAINTS  # type: ignore
        except ImportError:
            try:
                from model.src.backbones.uncrtaints import UNCRTAINTS  # type: ignore
            except ImportError as exc:
                raise DecloudUnavailable(
                    "UnCRtainTS package not importable. Clone "
                    "https://github.com/PatrickTUM/UnCRtainTS and set UNCRTAINTS_HOME"
                ) from exc

        ckpt_path = _resolve_checkpoint_file()
        s1_bands = 2 if decloud_use_sar() else 0
        s2_bands = 13
        model = UNCRTAINTS(
            input_dim=s1_bands + s2_bands,
            encoder_widths=[128],
            decoder_widths=[128, 128, 128, 128, 128],
            out_conv=[2 * s2_bands],
            out_nonlin_mean=True,
            out_nonlin_var="softplus",
            agg_mode="att_group",
            encoder_norm="group",
            decoder_norm="batch",
            n_head=16,
            d_model=256,
            d_k=4,
            pad_value=0,
            padding_mode="reflect",
            positional_encoding=True,
            covmode="diag",
            scale_by=10.0,
            separate_out=False,
            use_v=False,
            block_type="mbconv",
            is_mono=decloud_input_t() <= 1,
        )
        state = torch.load(ckpt_path, map_location=self.device)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        if (
            isinstance(state, dict)
            and "model" in state
            and isinstance(state["model"], dict)
        ):
            state = state["model"]
        missing, unexpected = model.load_state_dict(state, strict=False)
        if missing or unexpected:
            logger.warning(
                "uncrtaints_state_partial",
                missing=len(missing),
                unexpected=len(unexpected),
                path=str(ckpt_path),
            )
        model.to(self.device)
        model.eval()
        logger.info(
            "uncrtaints_loaded",
            device=self.device,
            checkpoint=str(ckpt_path),
            use_sar=decloud_use_sar(),
            input_t=decloud_input_t(),
        )
        return model

    def reconstruct(
        self,
        s2_stack: np.ndarray,
        s1_stack: np.ndarray | None,
        date_ordinals: list[int],
    ) -> np.ndarray:
        import torch

        # s2_stack DN (T, 13, H, W) -> [0, 1]
        s2 = process_ms_default(s2_stack)
        t, _, h, w = s2.shape
        if decloud_use_sar():
            if s1_stack is None:
                s1 = np.zeros((t, 2, h, w), dtype=np.float32)
            else:
                s1 = process_sar_default(s1_stack)
            fused = np.concatenate([s1, s2], axis=1)
        else:
            fused = s2
        fused, hw = pad_hw(fused)
        dates = _batch_positions(date_ordinals, t)
        tensor = torch.from_numpy(fused[None] * 10.0).to(self.device)
        pos = torch.from_numpy(dates).to(self.device)
        with torch.no_grad():
            out = self.model(tensor, batch_positions=pos)
        mean = out[:, :, :13, ...].detach().cpu().numpy() / 10.0
        rec = crop_hw(mean[0, -1], hw)
        return np.clip(rec.astype(np.float32), 0.0, 1.0)


def _batch_positions(date_ordinals: list[int], t: int) -> np.ndarray:
    """Days since S1 launch, shape [1, T], as UnCRtainTS positional encoding."""
    vals: list[float] = []
    for i in range(t):
        if i < len(date_ordinals) and date_ordinals[i]:
            vals.append(float(date_ordinals[i] - S1_LAUNCH_ORDINAL))
        elif vals:
            vals.append(vals[-1] + 5.0)
        else:
            vals.append(0.0)
    return np.asarray(vals, dtype=np.float32)[None]


def _resolve_checkpoint_file() -> Path:
    root = uncrtaints_checkpoint_dir()
    if not root:
        raise DecloudUnavailable(
            "UNCRTAINTS_CHECKPOINT_DIR is empty; download diagonal_1 weights "
            "and point the env var at that directory"
        )
    base = Path(root)
    if base.is_file() and base.suffix in {".pth", ".pt", ".ckpt"}:
        return base
    name = uncrtaints_checkpoint_name()
    candidates: list[Path] = []
    if base.is_dir():
        named = base / name
        if named.is_dir():
            candidates.extend(sorted(named.glob("*.pth")))
            candidates.extend(sorted(named.glob("*.pt")))
            candidates.extend(sorted(named.glob("*.ckpt")))
        candidates.extend(sorted(base.glob(f"{name}*.pth")))
        candidates.extend(sorted(base.glob("*.pth")))
        candidates.extend(sorted(base.glob("*.pt")))
        candidates.extend(sorted(base.glob("*.ckpt")))
    if not candidates:
        raise DecloudUnavailable(
            f"no .pth/.pt checkpoint under {base} (expected {name})"
        )
    return candidates[0]


def get_inferencer() -> Inferencer:
    backend = decloud_backend()
    if backend == "dummy":
        logger.info("decloud_backend_dummy", device=pick_device())
        return DummyInferencer()
    return UncrtainTSInferencer(device=pick_device())
