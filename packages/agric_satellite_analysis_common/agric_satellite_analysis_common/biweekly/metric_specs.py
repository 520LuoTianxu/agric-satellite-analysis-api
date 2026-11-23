"""Formal names, definitions and fixed display scales for biweekly report metrics.

Pure data: no settings, network or plotting imports. The download-machine
renderer and the API share these so a metric is named, explained and scaled
the same way on every page, every period and every field.

显示范围是固定的绝对色标，不随单期、单田块的最大/最小值伸缩：
- 有效范围（valid_range）取指数的数学定义域或产品存储裁剪范围，超出即视为无效值；
- 显示范围（display_range）上界：NDVI 固定为理论最大值 1.0；EVI、NDRE、NDMI 与
  ingest 指数注册表 ``app/tasks/indices.py`` 的 ``rescale`` 一致（平台图层同口径）；
- 植被类指数显示下界为 0，小于 0 的像元（水体、积雪等非植被）单独着色，不混入色带；
- 低值参考线（low_reference）取指数注册表的预警阈值 ``AlertDefaults.threshold``，
  是平台通用参考值，未按作物和生育阶段校准，只用于描述绝对水平，不作诊断。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    key: str
    abbr: str
    name: str
    purpose: str
    formula: str
    unit: str
    valid_range: tuple[float, float]
    display_range: tuple[float, float]
    scale_kind: str  # "sequential"：低到高单向色带；"diverging"：以 0 为中心的双向色带
    low_reference: float | None
    range_note: str

    @property
    def label(self) -> str:
        """Full formal label used on first mention: 归一化植被指数（NDVI）."""
        return f"{self.name}（{self.abbr}）"


METRIC_SPECS: dict[str, MetricSpec] = {
    "ndvi": MetricSpec(
        key="ndvi",
        abbr="NDVI",
        name="归一化植被指数",
        purpose="反映植被覆盖程度和长势强弱，数值越高表示植被越茂密",
        formula="(近红外 − 红光) ÷ (近红外 + 红光)",
        unit="无量纲",
        valid_range=(-1.0, 1.0),
        display_range=(0.0, 1.0),
        scale_kind="sequential",
        low_reference=0.3,
        range_note="理论取值 −1 至 1；植被与裸土通常为 0 至 1，小于 0 多为水体或积雪",
    ),
    "evi": MetricSpec(
        key="evi",
        abbr="EVI",
        name="增强型植被指数",
        purpose="在NDVI基础上削弱土壤背景和大气影响，植被茂密时不易饱和",
        formula="2.5 × (近红外 − 红光) ÷ (近红外 + 6 × 红光 − 7.5 × 蓝光 + 1)",
        unit="无量纲",
        valid_range=(-1.0, 2.0),
        display_range=(0.0, 1.2),
        scale_kind="sequential",
        low_reference=0.2,
        range_note="无固定理论上限，植被通常为 0 至 1，茂密冠层可略高于 1",
    ),
    "ndre": MetricSpec(
        key="ndre",
        abbr="NDRE",
        name="归一化差值红边指数",
        purpose="对叶片叶绿素含量敏感，用于冠层较密时期的长势参考",
        formula="(近红外 − 红边) ÷ (近红外 + 红边)",
        unit="无量纲",
        valid_range=(-1.0, 1.0),
        display_range=(0.0, 0.8),
        scale_kind="sequential",
        low_reference=0.2,
        range_note="理论取值 −1 至 1；同一冠层的NDRE通常低于NDVI，作物多为 0 至 0.7",
    ),
    "ndmi": MetricSpec(
        key="ndmi",
        abbr="NDMI",
        name="归一化差值水分指数",
        purpose="反映作物冠层含水状况，数值越高表示冠层含水越多",
        formula="(近红外 − 短波红外) ÷ (近红外 + 短波红外)",
        unit="无量纲",
        valid_range=(-1.0, 1.0),
        display_range=(-0.5, 0.5),
        scale_kind="diverging",
        low_reference=0.0,
        range_note="理论取值 −1 至 1；小于 0 多为裸土或冠层含水偏少，大于 0 表示冠层含水较多",
    ),
}

REPORT_METRIC_ORDER: tuple[str, ...] = ("ndvi", "evi", "ndre", "ndmi")


def metric_spec(metric: str) -> MetricSpec:
    return METRIC_SPECS[str(metric).lower()]


def metric_abbr(metric: str) -> str:
    spec = METRIC_SPECS.get(str(metric).lower())
    return spec.abbr if spec else str(metric).upper()


def metric_label(metric: str) -> str:
    spec = METRIC_SPECS.get(str(metric).lower())
    return spec.label if spec else str(metric).upper()
