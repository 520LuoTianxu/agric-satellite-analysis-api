"""Sentinel-1 STAC / GDAL access helpers.

Default catalog is Element84 Earth Search ``sentinel-1-grd`` on AWS Open Data
(``s3://sentinel-s1-l1c``). Objects are opened via anonymous HTTPS
(``https://sentinel-s1-l1c.s3.amazonaws.com/...``) so workers do not need
requester-pays AWS keys and avoid Azure ``sentinel1euwest`` RTT/SAS 409s.

Optional: set ``S1_STAC_API_URL`` to Planetary Computer (prod or staging) to
use Azure assets with ``planetary_computer`` SAS signing.
"""

from __future__ import annotations

import os
from typing import Any

# Element84 Earth Search (default for agri S1 — AWS HTTPS, no Azure SAS).
S1_STAC_API_URL_DEFAULT = "https://earth-search.aws.element84.com/v1"
S1_STAC_COLLECTION = "sentinel-1-grd"

# AWS Open Data bucket behind Element84 sentinel-1-grd assets.
_S1_AWS_S3_PREFIX = "s3://sentinel-s1-l1c/"
_S1_AWS_HTTPS_PREFIX = "https://sentinel-s1-l1c.s3.amazonaws.com/"

# S1 GDAL Env 默认分块/缓存见 app.core.windowed_gdal（与 S2 共享最快 knobs）。


def s1_stac_api_url() -> str:
    """读取S1目录地址，允许测试或替代部署通过环境变量覆盖默认目录。"""
    return (
        (os.environ.get("S1_STAC_API_URL") or "").strip()
        or S1_STAC_API_URL_DEFAULT
    )


# Prod + staging SAS/STAC hosts. Substring "planetarycomputer.microsoft.com"
# alone misses planetarycomputer-staging.microsoft.com and skips signing → Azure 409.
_MPC_HOST_MARKERS = (
    "planetarycomputer.microsoft.com",
    "planetarycomputer-staging.microsoft.com",
)


def s1_uses_planetary_computer() -> bool:
    """判断当前目录是否需要Planetary Computer的资产签名流程。"""
    url = s1_stac_api_url().lower()
    return any(marker in url for marker in _MPC_HOST_MARKERS)


def _strip_gdal_vsi_prefix(href: str) -> str:
    """去掉 /vsicurl/ 等前缀，避免 pc.sign 不认 VSI 包装而原样返回未签名 URL。"""
    for prefix in ("/vsicurl/", "/vsis3/", "/vsiaz/", "/vsis/"):
        if href.startswith(prefix):
            return href[len(prefix) :]
    return href


def s1_aws_https_href(href: str) -> str:
    """把 Element84 的 s3://sentinel-s1-l1c/ 改写成可匿名 Range 的 HTTPS。

    S3 API 对 requester-pays 需要密钥；实测同对象的 path-style HTTPS 可直接
    HEAD/Range，适合中国区下载机，且避开 Azure SAS。
    """
    raw = _strip_gdal_vsi_prefix(href)
    if raw.startswith(_S1_AWS_S3_PREFIX):
        return _S1_AWS_HTTPS_PREFIX + raw[len(_S1_AWS_S3_PREFIX) :]
    # Already path-style / virtual-hosted AWS URL for this bucket — keep.
    return raw


def sign_s1_href(href: str) -> str:
    """MPC资产默认禁止匿名读，按需签发短期SAS地址，其余目录保持原链接。"""
    if not href:
        return href
    if not s1_uses_planetary_computer():
        return href
    import planetary_computer as pc

    return str(pc.sign(_strip_gdal_vsi_prefix(href)))


def s1_http_href(href: str) -> str:
    """统一在打开前解析可读的 HTTP(S) 地址：AWS 改写 HTTPS，MPC 签发 SAS。"""
    if not href:
        return href
    raw = _strip_gdal_vsi_prefix(href)
    if raw.startswith(_S1_AWS_S3_PREFIX) or "sentinel-s1-l1c.s3" in raw:
        return s1_aws_https_href(raw)
    if s1_uses_planetary_computer():
        return sign_s1_href(raw)
    return raw


def open_s1_stac_client():
    """创建S1 STAC客户端，并在MPC响应中统一签名资产地址。"""
    from app.core.stac_client import open_stac_client

    url = s1_stac_api_url()
    if s1_uses_planetary_computer():
        import planetary_computer as pc

        return open_stac_client(url, modifier=pc.sign_inplace)
    return open_stac_client(url)


def s1_gdal_env() -> dict[str, str]:
    """S1 窗口读 GDAL Env：共享最快 AWS 调优 + 可被 S1_* 覆盖。

    参数只在单次 ``rasterio.Env`` 中生效，避免改进程级环境拖累 OSS/S2。
    Element84 AWS HTTPS 与 Azure SAS 共用这套 knobs；HTTPS 改写见
    ``s1_http_href`` / ``s1_open_path``。
    """
    from app.core.windowed_gdal import windowed_cog_gdal_env

    return windowed_cog_gdal_env(prefix="S1")


def s1_open_path(href: str) -> str:
    """解析可读地址后走 /vsicurl/；sentinel-s1-l1c 优先 HTTPS，不再强制 /vsis3/。"""
    resolved = s1_http_href(href)
    if resolved.startswith(("/vsicurl/", "/vsis3/", "/vsiaz/", "/vsis/")):
        return resolved
    if resolved.startswith("s3://"):
        # 非 Open Data 桶才回退 vsis3（需进程内 AWS 凭证）。
        return resolved.replace("s3://", "/vsis3/", 1)
    if resolved.startswith("http://") or resolved.startswith("https://"):
        # 查询串（含 SAS）必须经 vsicurl，避免 Range 时丢签名。
        return f"/vsicurl/{resolved}"
    return resolved


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
            "planetary-computer package on the ingest image."
        )
    return (
        "S1 uses Element84 Earth Search "
        f"({s1_stac_api_url()}, collection={S1_STAC_COLLECTION}). "
        "Assets are read via anonymous HTTPS on sentinel-s1-l1c "
        "(no requester-pays keys; avoids Azure SAS)."
    )


# Back-compat aliases used by older call sites / logs
def s1_has_aws_credentials() -> bool:
    """兼容旧健康检查字段；MPC通过SAS访问，不再依赖AWS requester-pays密钥。"""
    return True


def s1_missing_credentials_hint() -> str:
    """兼容旧调用方，统一返回当前S1目录的访问配置说明。"""
    return s1_access_hint()
