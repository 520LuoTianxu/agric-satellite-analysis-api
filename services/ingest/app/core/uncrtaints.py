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

import json
import re
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


_CHECKPOINT_SUFFIXES = (".pth.tar", ".pth", ".pt", ".ckpt")
_BLOCK_DIGIT = re.compile(r"^(in_block|out_block)(\d+)$")
S2_BANDS = 13


def _is_checkpoint_file(path: Path) -> bool:
    return path.name.lower().endswith(_CHECKPOINT_SUFFIXES)


def _checkpoint_rank(path: Path) -> tuple[int, int, str]:
    name = path.name.lower()
    if name.endswith(".pth.tar"):
        ext_rank = 0
    elif name.endswith(".pth"):
        ext_rank = 1
    elif name.endswith(".pt"):
        ext_rank = 2
    else:
        ext_rank = 3
    model_rank = 0 if name.startswith("model.") else 1
    return (ext_rank, model_rank, name)


def _collect_checkpoint_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and _is_checkpoint_file(p)]
    return sorted(found, key=_checkpoint_rank)


def _as_int_list(value: Any, default: list[int]) -> list[int]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        text = value.strip().lstrip("[").rstrip("]")
        if not text:
            return list(default)
        try:
            return [int(part.strip()) for part in text.split(",") if part.strip()]
        except ValueError:
            return list(default)
    if isinstance(value, (list, tuple)):
        try:
            return [int(part) for part in value]
        except (TypeError, ValueError):
            return list(default)
    return list(default)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _load_conf_json(ckpt_path: Path) -> dict[str, Any]:
    conf_path = ckpt_path.parent / "conf.json"
    if not conf_path.is_file():
        return {}
    try:
        with conf_path.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("uncrtaints_conf_unreadable", path=str(conf_path))
        return {}
    return data if isinstance(data, dict) else {}


def _expand_out_conv(out_conv: list[int], covmode: str) -> list[int]:
    """Match train_reconstruct.py: diag/uni add 13, iso adds 1, if still S2-sized."""
    if not out_conv:
        return [2 * S2_BANDS]
    last = out_conv[-1]
    if last != S2_BANDS:
        return out_conv
    expanded = list(out_conv)
    if covmode == "iso":
        expanded[-1] = last + 1
    elif covmode in ("uni", "diag"):
        expanded[-1] = last + S2_BANDS
    return expanded


def _uncrtaints_ctor_kwargs(conf: dict[str, Any], input_dim: int) -> dict[str, Any]:
    covmode = str(conf.get("covmode") or "diag")
    out_conv = _expand_out_conv(
        _as_int_list(conf.get("out_conv"), [2 * S2_BANDS]), covmode
    )
    pretrain = conf.get("pretrain")
    if pretrain is None:
        is_mono = decloud_input_t() <= 1
    else:
        is_mono = _as_bool(pretrain, False)
    return {
        "input_dim": input_dim,
        "encoder_widths": _as_int_list(conf.get("encoder_widths"), [128]),
        "decoder_widths": _as_int_list(
            conf.get("decoder_widths"), [128, 128, 128, 128, 128]
        ),
        "out_conv": out_conv,
        "out_nonlin_mean": _as_bool(conf.get("mean_nonLinearity"), True),
        "out_nonlin_var": str(conf.get("var_nonLinearity") or "softplus"),
        "agg_mode": str(conf.get("agg_mode") or "att_group"),
        "encoder_norm": str(conf.get("encoder_norm") or "group"),
        "decoder_norm": str(conf.get("decoder_norm") or "batch"),
        "n_head": int(conf.get("n_head") or 16),
        "d_model": int(conf.get("d_model") or 256),
        "d_k": int(conf.get("d_k") or 4),
        "pad_value": conf.get("pad_value", 0),
        "padding_mode": str(conf.get("padding_mode") or "reflect"),
        "positional_encoding": _as_bool(conf.get("positional_encoding"), True),
        "covmode": covmode,
        "scale_by": 10.0 if conf.get("scale_by") is None else float(conf["scale_by"]),
        "separate_out": _as_bool(conf.get("separate_out"), False),
        "use_v": _as_bool(conf.get("use_v"), False),
        "block_type": str(conf.get("block_type") or "mbconv"),
        "is_mono": is_mono,
    }


def _extract_state_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DecloudUnavailable("checkpoint is not a state dict")
    nested = raw.get("state_dict")
    if isinstance(nested, dict):
        return nested
    nested = raw.get("model")
    if isinstance(nested, dict):
        return nested
    return raw


def _strip_netg_prefix(state: dict[str, Any]) -> dict[str, Any]:
    """Drop BaseModel wrapper keys so a bare UNCRTAINTS can load official weights."""
    netg_only: dict[str, Any] = {}
    rest: dict[str, Any] = {}
    for key, value in state.items():
        name = key[7:] if key.startswith("module.") else key
        if name.startswith("netG."):
            netg_only[name[5:]] = value
        elif name != "netG":
            rest[name] = value
    return netg_only if netg_only else rest


def _rename_in_out_blocks(state: dict[str, Any]) -> dict[str, Any]:
    """Official load_checkpoint fallback: in_block1 -> in_block.0 (digit minus 1)."""
    renamed: dict[str, Any] = {}
    for key, value in state.items():
        parts = key.split(".")
        new_parts: list[str] = []
        for part in parts:
            match = _BLOCK_DIGIT.match(part)
            if match:
                new_parts.append(f"{match.group(1)}.{int(match.group(2)) - 1}")
            else:
                new_parts.append(part)
        renamed[".".join(new_parts)] = value
    return renamed


def _load_state_into_generator(
    model: Any, state: dict[str, Any], ckpt_path: Path
) -> tuple[list[str], list[str]]:
    cleaned = _strip_netg_prefix(state)
    missing, unexpected = model.load_state_dict(cleaned, strict=False)
    missing_list = list(missing or ())
    unexpected_list = list(unexpected or ())
    if missing_list:
        renamed = _rename_in_out_blocks(cleaned)
        missing, unexpected = model.load_state_dict(renamed, strict=False)
        missing_list = list(missing or ())
        unexpected_list = list(unexpected or ())
    if missing_list or unexpected_list:
        logger.warning(
            "uncrtaints_state_partial",
            missing=len(missing_list),
            unexpected=len(unexpected_list),
            path=str(ckpt_path),
        )
    return missing_list, unexpected_list


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
        conf = _load_conf_json(ckpt_path)
        s1_bands = 2 if decloud_use_sar() else 0
        # Bare generator only. Do not import get_model/BaseModel (pulls fvcore + Adam).
        model = UNCRTAINTS(
            **_uncrtaints_ctor_kwargs(conf, input_dim=s1_bands + S2_BANDS)
        )
        raw = torch.load(ckpt_path, map_location=self.device)
        state = _extract_state_dict(raw)
        _load_state_into_generator(model, state, ckpt_path)
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
        generator = getattr(self.model, "netG", self.model)
        with torch.no_grad():
            out = generator(tensor, batch_positions=pos)
        mean = out[:, :, :13].detach().cpu().numpy() / 10.0
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
    if base.is_file():
        if _is_checkpoint_file(base):
            return base
        raise DecloudUnavailable(
            f"checkpoint file {base} is not .pth.tar/.pth/.pt/.ckpt"
        )
    name = uncrtaints_checkpoint_name()
    search_dirs: list[Path] = []
    named = base / name
    if named.is_dir():
        search_dirs.append(named)
    search_dirs.append(base)
    for directory in search_dirs:
        found = _collect_checkpoint_files(directory)
        if found:
            return found[0]
    raise DecloudUnavailable(
        f"no .pth.tar/.pth/.pt/.ckpt checkpoint under {base} (expected {name})"
    )


def get_inferencer() -> Inferencer:
    backend = decloud_backend()
    if backend == "dummy":
        logger.info("decloud_backend_dummy", device=pick_device())
        return DummyInferencer()
    return UncrtainTSInferencer(device=pick_device())
