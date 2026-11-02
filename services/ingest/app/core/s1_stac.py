"""Sentinel-1 STAC / GDAL access helpers (Planetary Computer).

Default catalog is Microsoft Planetary Computer ``sentinel-1-grd``.
Assets live on Azure Blob Storage; unsigned hrefs 404, so HREFs are SAS-signed
via ``planetary_computer`` (no AWS requester-pays keys required).

Optional override: set ``S1_STAC_API_URL`` (must still expose ``sentinel-1-grd``
with readable VV/VH assets after signing when using MPC).
"""

from __future__ import annotations

import os
from typing import Any

# Microsoft Planetary Computer STAC API (default for agri S1).
S1_STAC_API_URL_DEFAULT = "https://planetarycomputer.microsoft.com/api/stac/v1"
S1_STAC_COLLECTION = "sentinel-1-grd"

# Azure Blob（sentinel1euwest 等）跨区 RTT 高：默认 16 KiB curl 分块会放大
# Range 往返次数。S1 Env 内加大分块/缓存，且不改进程级 GDAL，避免拖累 S2/OSS。
_DEFAULT_CURL_CHUNK_SIZE = 262144  # 256 KiB
_DEFAULT_CURL_CACHE_SIZE = 64 * 1024 * 1024
_DEFAULT_VSI_CACHE_SIZE = 64 * 1024 * 1024


def s1_stac_api_url() -> str:
    """读取S1目录地址，允许测试或替代部署通过环境变量覆盖默认目录。"""
    return (
        (os.environ.get("S1_STAC_API_URL") or "").strip()
        or S1_STAC_API_URL_DEFAULT
    )


def s1_uses_planetary_computer() -> bool:
    """判断当前目录是否需要Planetary Computer的资产签名流程。"""
    url = s1_stac_api_url().lower()
    return "planetarycomputer.microsoft.com" in url


def sign_s1_href(href: str) -> str:
    """MPC资产默认禁止匿名读，按需签发短期SAS地址，其余目录保持原链接。"""
    if not href:
        return href
    if not s1_uses_planetary_computer():
        return href
    import planetary_computer as pc

    return str(pc.sign(href))


def open_s1_stac_client():
    """创建S1 STAC客户端，并在MPC响应中统一签名资产地址。"""
    from app.core.stac_client import open_stac_client

    url = s1_stac_api_url()
    if s1_uses_planetary_computer():
        import planetary_computer as pc

        return open_stac_client(url, modifier=pc.sign_inplace)
    return open_stac_client(url)


def _positive_int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def s1_gdal_env() -> dict[str, str]:
    """为签名后的Azure COG配置GDAL读取参数，避免修改进程级环境变量。

    参数只在单次rasterio.Env中生效，防止共享worker里的OSS/GDAL任务互相串扰。
    相对默认 vsicurl：更大 curl 分块、启用 HTTP/2 多路 Range、合并连续区间，
    降低中国→西欧 Azure 高 RTT 下的往返次数。内部重试保持 1 次，避免与
    ``BAND_READ_MAX_ATTEMPTS`` 相乘放大 SSL EOF。
    """
    chunk = _positive_int_env(
        "S1_GDAL_CURL_CHUNK_SIZE",
        _DEFAULT_CURL_CHUNK_SIZE,
        minimum=16 * 1024,
    )
    curl_cache = _positive_int_env(
        "S1_GDAL_CURL_CACHE_SIZE",
        _DEFAULT_CURL_CACHE_SIZE,
        minimum=1024 * 1024,
    )
    vsi_cache = _positive_int_env(
        "S1_VSI_CACHE_SIZE",
        _DEFAULT_VSI_CACHE_SIZE,
        minimum=1024 * 1024,
    )
    http_version = (os.environ.get("S1_GDAL_HTTP_VERSION") or "2").strip() or "2"
    multirange = (os.environ.get("S1_GDAL_HTTP_MULTIRANGE") or "YES").strip() or "YES"
    return {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_UNSAFESSL": "NO",
        "CPL_VSIL_CURL_USE_HEAD": "NO",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_HTTP_MULTIRANGE": multirange,
        "GDAL_HTTP_VERSION": http_version,
        "CPL_VSIL_CURL_CHUNK_SIZE": str(chunk),
        "CPL_VSIL_CURL_CACHE_SIZE": str(curl_cache),
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": str(vsi_cache),
        "GDAL_HTTP_TCP_KEEPALIVE": "YES",
        "GDAL_HTTP_TCP_KEEPIDLE": "30",
        "GDAL_HTTP_TCP_KEEPINTVL": "15",
        "GDAL_HTTP_CONNECTTIMEOUT": "10",
        "GDAL_HTTP_TIMEOUT": "60",
        "GDAL_HTTP_LOW_SPEED_LIMIT": "1",
        "GDAL_HTTP_LOW_SPEED_TIME": "30",
        "GDAL_HTTP_MAX_RETRY": "1",
        "GDAL_HTTP_RETRY_DELAY": "1",
    }


def s1_open_path(href: str) -> str:
    """给MPC链接签名；S3 转 /vsis3/；HTTPS（含 SAS 查询串）走 /vsicurl/。"""
    signed = sign_s1_href(href)
    if signed.startswith("s3://"):
        return signed.replace("s3://", "/vsis3/", 1)
    if signed.startswith(("/vsicurl/", "/vsis3/", "/vsiaz/", "/vsis/")):
        return signed
    if signed.startswith("http://") or signed.startswith("https://"):
        # SAS 查询参数必须经 vsicurl 前缀，避免 Range 请求时丢失签名字段。
        return f"/vsicurl/{signed}"
    return signed


def stac_asset_href(asset: Any) -> str | None:
    """优先选STAC提供的HTTPS替代地址，避免worker额外配置云厂商SDK凭证。"""
    if asset is None:
        return None
    extra = getattr(asset, "extra_fields", None)
    if extra is None and isinstance(asset, dict):
        extra = asset
    if isinstance(extra, dict):
        alts = extra.get("alternate") or extra.get("alternates") or {}
        if isinstance(alts, dict):
            for key in ("https", "HTTPS", "http"):
                alt = alts.get(key)
                if isinstance(alt, dict):
                    href = alt.get("href")
                    if href:
                        return str(href)
                elif isinstance(alt, str) and alt.startswith("http"):
                    return alt
    href = getattr(asset, "href", None)
    if href is None and isinstance(asset, dict):
        href = asset.get("href")
    return str(href) if href else None


def s1_access_hint() -> str:
    """生成S1数据源连通性提示，区分MPC签名要求与自定义目录要求。"""
    if s1_uses_planetary_computer():
        return (
            "S1 uses Microsoft Planetary Computer "
            f"({s1_stac_api_url()}, collection={S1_STAC_COLLECTION}). "
            "Assets need SAS signing via planetary_computer; install the "
            "planetary-computer package on the ingest image. No AWS "
            "requester-pays keys are required for this path."
        )
    return (
        f"S1 STAC is {s1_stac_api_url()} (collection={S1_STAC_COLLECTION}). "
        "Ensure VV/VH assets are readable by GDAL from this catalog."
    )


# Back-compat aliases used by older call sites / logs
def s1_has_aws_credentials() -> bool:
    """兼容旧健康检查字段；MPC通过SAS访问，不再依赖AWS requester-pays密钥。"""
    return True


def s1_missing_credentials_hint() -> str:
    """兼容旧调用方，统一返回当前S1目录的访问配置说明。"""
    return s1_access_hint()
