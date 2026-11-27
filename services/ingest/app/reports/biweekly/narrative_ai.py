"""模型撰写双周报里「往好 / 往差」的文字说明。

数值、日期和升高、降低由程序算好后传给模型。模型只写判断和说明，
不能改数字，也不能新增事实里没有的数字。代码不根据涨跌或生育期决定好坏。
模型名、地址和密钥只从 BAILIAN_MODEL、BAILIAN_BASE_URL、BAILIAN_API_KEY 读取。
补全长度、等待和是否思考从 BAILIAN_MAX_TOKENS、BAILIAN_TIMEOUT_S、
BAILIAN_ENABLE_THINKING 读取。DeepSeek 混合思考默认关闭，给 JSON 正文留额度。
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

import structlog

from agric_satellite_analysis_common.biweekly.metric_specs import (
    METRIC_SPECS,
    metric_abbr,
)
from app.reports.biweekly.sowing_gdd import gdd_for_model, sowing_for_model
from agric_satellite_analysis_common.biweekly.phase_policy import STAGE_BACKGROUND

logger = structlog.get_logger("biweekly.narrative")

DEFAULT_BASE_URL = (
    "https://llm-7cudikcfvgf9l1hy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_MODEL = "qwen3.7-flash"
# 百炼 deepseek-v4.1-flash-1017（2026-11 文档）：
# 最大输出 393216。max_tokens 同时限制思维链和正文。thinking_budget 对该快照
# 标为不支持。它是混合思考模型，默认开启思考，HTTP 正文顶层 enable_thinking
# 可关闭；reasoning_effort 仅 low/high/max，默认 high。官方 DeepSeek 的
# thinking.type 不在这条百炼兼容接口的参数表里，这里不发送。
# 生产 2026-11-24 P15：max_tokens=8192、finish_reason=length、completion_tokens=8192、
# content_chars=0、reasoning_chars=14791。思考随预算变长，把上限改成 16384
# 不能单独证明正文会出现。默认关闭思考，正文预算 16384，上限用模型的 393216。
# 非 DeepSeek 模型不发送 enable_thinking，输出保护上限 32768。
# 一次报告最多两次模型调用。超时、连接中断、429 和 5xx 会再试一次。
# 超时未设置时按预算估算，夹在 30–600 秒，两次调用仍低于 Celery 软限制 1500 秒。
DEEPSEEK_OUTPUT_MAX = 393216
OTHER_OUTPUT_MAX = 32768
DEFAULT_MAX_TOKENS = 16384
MIN_MAX_TOKENS = 1024
MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 600
MAX_MODEL_CALLS = 2
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
JUDGMENTS = {"better", "worse", "unclear"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = """你在写农业遥感双周报的正文。读者是种植户和农技人员。用户消息里有 parameter_guide 和本期参数。parameter_guide 只解释字段含义，其余字段是程序已经算好的事实。请按本期事实写。按本期生育阶段自己组织判断：不同阶段要回答的问题不一样，不要套同一套句子。本期真有的遥感、天气和土壤，只要会改变这个阶段的读法，就写进结论。结论要写完整，不要收成一两句，也不要为了短而省掉。

指标用正式名称：归一化植被指数（NDVI）、增强型植被指数（EVI）、归一化差值红边指数（NDRE）、归一化差值水分指数（NDMI）。第一次出现可以带中文全称，后面用缩写。不要把它们改叫「绿度」「叶片含水」或「长势」。不要写像元、百分位、中位数、格点、ET0、EXG 和公式。句子按事实来组织，不要每条都写成“由A升到B，提高C”，也不要只写“全部上升、方向一致”。

结论从本期生育阶段要回答的问题写起，不要复述 stage_context 或 conclusion_task。某个参数为空就不要编，也不要用“缺失”凑字。

遥感按 zones 写空间，点出方位和指数，不要只说「不齐」或「其余方位」。level=higher 是这一方位已经高出整块地，level=lower 是明显更低，level=similar 是接近整块地，level=unreadable 是这一方位这次大部分读不到。出苗期：higher 写成这些方位已经有植被信号、更像先出苗；similar 而全田仍低时，写成这些方位仍看不出苗，把方位名写出来；lower 写成比已经偏低的全田还弱，是要重点看的地方；unreadable 写成这次看不清、不能判断出苗。全田一起偏低是出苗期的常态，用一句说清，不要把常态写成异常。spatial_split=even 时方位没有拉开，不要逐个报数，出苗期就写这次还分不出哪一块先出苗。分蘖、返青、拔节看覆盖或冠层是全田一起变，还是只在部分方位，没跟上的方位要写出来。越冬看指数是否稳住。灌浆期和成熟期：全田一起下降是成熟本身。若有的方位已经明显更低、有的仍高，按方位说明哪些更像已经收获或覆盖已经掉下去，哪些仍像未收；指数拉不开就写这次分不出收获范围，不要改写成笼统的长势句。

天气写进结论，不要留到气象那一节才第一次出现。有气温就写冷热，有去年同期就比去年高还是低；有降水毫米数就写有效降雨，并按毫米数说比去年多还是少。前期 antecedent 有有效降雨时，和本期连着看。再按这个阶段说明意味着什么：出苗看冷和雨会不会让苗出得慢，越冬看冷，返青和拔节看回升时水分够不够参照，灌浆和成熟看热和干。有积温就写积温，有播种日期就写哪一天播的、到最近观测过了几天。出苗期积温偏低可以说出苗会慢，不要把播后不久或积温还低时的低指数写成苗弱，也不要编一个该出苗的积温标准。积温没盖住播种当天时，按给出的起止日期写。缺哪一项就跳过，不要写「没有可用」「没有记录」「无从对照」或「未取得」。weather 为空就不要提天气。没有播种日期或积温就不要写这两项。不要写「雨水」或「雨水账」，不要写格点、ET0。autumn_emergence 是 ww_autumn_soil_baseline_v1 对冬小麦秋季出苗的算法判定，是有算法支撑的证据，不是人工填写，也不是苗数或精确出苗日。有这项时，出苗判断优先用它的 status、emerged_flag、first_emerged_date 和类别占比，再和方位指数、天气、积温一起看，并遵守 disclaimer 里的限制。没有这项就不要写算法出苗结论。积温和出苗是两路独立事实：缺出苗时，已有的天气和积温仍要写；缺积温时，已有的出苗算法结果仍要写。干湿只在有效降雨、NDMI 和 water_risk_lines 够用时判断要不要留意浇水或排水；不够就说一次看不准，不要写成没有风险，也不要写灌多少毫米。

有土壤底子就写进结论：用土质、酸碱和能存多少水，说明它和本期出苗或干湿怎么一起看。保水差、又碰上有效降雨少时，仍看不出苗的方位更要盯。不要另作缺肥、病虫害诊断，也不要把 context_lines 里的土壤原句再抄一遍。status 不是 ready 就不要写土壤。

key_nodes 是人填写的农事。有记录就写做了什么、哪一天、填写里能看懂的内容，再按 compare_with 的意思说明本期指数和这件事对得上、还看不出来，还是对不上。compare_with 不要原句抄进正文。没有记录就不要写农事。不要把农事说成卫星测到的，不要建议施肥或打药，不要写缺肥、病虫害、闹病。

不要按「整体情况、田内不齐、和周围比、天气」四段标题来写。把遥感、天气、土壤里会改变本期读法的内容写成连贯判断。周围对照只在差得清楚时写一句，并说明周围作物未必相同。没有数的话题不要编。

缺了的资料只说一次。不要用同一句巡查套话收尾，也不要每段都劝人下地。出苗期如果已经能分出仍看不出苗或看不清的方位，field_notes 写重点看哪里、看什么（苗有没有出来、有没有断垄）。其他阶段只在某个写明的方位、或某件对不上的农事值得到地里看时才写。没有这样的点就给空数组。

数值升高或降低只表示大小，不等于好坏。哪几个字需要标出，由你决定，不要每句末尾都补同一个词。

正文是雅黑 10 磅。标记里面只放选中的字，不要把整句包进去：
- 加粗：<b>…</b>
- 往好处、值得注意的字：<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">…</font>
- 往差处、需要留意的字：<font face="YaHei-Bold" color="#9C2B1B" backColor="#F8D6D2">…</font>
- 某一具体位置值得核对的字：<font face="YaHei-Bold" color="#8A5A12" backColor="#F8E8C8">…</font>
没有把握的字保持正文。不要用别的颜色，不要用 Markdown。

metric_notes 是「指标说明」：每个指标按本期数值写成说明，讲清这个数在看什么、本期说明了什么。不要复读定义，不要写公式，每个指标不要写成同一句式。
conclusion 是印在「本期结论」下面的正文，种植户最先看这一段。事实够的时候写成六到十句，每句单独放进 conclusion 数组，把这一期写完整：这个阶段在看什么，全田和各方位的指数，跟上一次比是升是降，冷热和有效降雨，土壤底子，有播种日期和积温就写上，有农事就对照。不要收成一两句，也不要只剩一个指数和几个偏高方位。不要为了凑字把同一件事说两遍，也不要编参数里没有的数。事实很少时按已有的写，不要用空话补长。不要把 changes 里每个指标的升幅再报一遍。context_lines 还会再印很短的农事和土壤底子，不要把那些原句再抄一遍。
changes 每个指标一句，只写数值变化说明了什么，不要把周围、天气和农事再写进去。
infield、surrounding、weather 是后面各节的展开。结论里已经写过的判断，这些节不要换词再抄一遍；可以补结论里没展开的细节。没有可写的内容就给空数组。
数字和日期只能照抄参数里已经出现的写法。不要写「低值」「参考线」「预警线」「达标」「确诊」「灾损」「产量」「好转」「变差」，也不要提到模型或 AI。
field_patterns 和 water_risk_lines 对应的句子已经印在报告里，不要换词再抄一遍。结论里不要出现缺肥、病虫害、闹病、产量，也不要写「先别下结论」「先别急着」「不下结论」「不能下结论」「只能当参考」「仅供参考」「不判定」「现在还不能判断」。
不要输出思考过程，只输出 JSON：
{"summary":"一段话","changes":[{"metric":"ndvi","judgment":"better|worse|unclear","sentence":"带标记的句子"}],"conclusion":["带标记的句子"],"field_notes":["带标记的句子"],"metric_notes":["带标记的句子"],"infield":[{"metric":"ndre","text":"带标记的句子"}],"surrounding":["带标记的句子"],"weather":["带标记的句子"]}
changes 和 infield 的 metric 只能是 metrics 里的键。judgment 只能是 better、worse、unclear。句中的颜色由你选，不必和某个固定词对应。有指标时 metric_notes 覆盖每一个指标。weather、surrounding、infield、field_notes 没有可写的内容就给空数组，不要用套话填。
"""

# 跟参数一起交给模型，只解释含义，不另给可以引用的数字。
PARAMETER_GUIDE = {
    "metrics": "每个指标的全田结果。name 是正式名称，正文用其中的缩写。meaning 只说明这个数在看什么，不要照抄，也不要改叫绿度、叶片含水或长势。latest 是最近一次有效观测的数值，baseline 是拿来对比的上一次，delta 是 latest 减 baseline。value_direction 只表示数值升高、降低或持平。role 是本期主要指标还是辅助指标。",
    "used_observations": "本期采用的观测日期。",
    "rejected_observations": "卫星过境但没有采用的日期。reason 是未采用的原因，cloud_pct 是地块云量的百分比。没有未采用日期就不要写。",
    "infield": "田块内部。median、p10、p90 是整块地的水平，写的时候直接说指数数值，不要说中位数、百分位、像元。zones 含有这一指标各方位的水平：level=lower 更低，higher 更高，similar 接近整块地，unreadable 是这一方位多数地方这次读不到。出苗期 similar 且全田仍低，就是仍看不出苗的方位，要把方位名写出来，不要写成「其余方位」。spatial_split=even 表示方位之间没有拉开，不要写某一块偏弱，也不要逐个报数；unreadable 表示有方位看不清；split 表示高低分开了。area_percent 是这一方位占整块地的百分比，valid_percent 是这一方位里能读到的百分比。delta_median 是这一方位相对上一次的变化。regions.name 是连成片偏低的位置，share_percent 是这些偏低地方里这一方位占的百分比。出苗期把 higher 写成已有植被信号，把 lower 写成仍看不出苗或更弱、要重点看，把 unreadable 写成不能判断出苗。成熟期和灌浆期：lower 且降幅明显时可以写成更像已经收获或覆盖已经掉下去，没有一起掉的方位写成仍像未收；拉不开就不要编收获范围。",
    "surrounding": "这块地和周围大约一公里农田比。field_minus_nearby 是这块地减去周围，正数表示这块地更高。差得清楚再写，并说明周围作物未必相同。不要每篇都写同一句免责，也不要写只能当参考。不要说超绿、EXG。没有比较就不要写这一节。",
    "weather": "地块附近的天气，不是田间实测。这些数要写进结论，不要只留在 weather 数组。有气温就写冷热和比去年高还是低；有降水毫米数就直接说有效降雨比去年多还是少，并说明对当前生育阶段意味着什么。缺哪一项就跳过。weather 为空就不要写天气，不要写没有可用、没有记录或无从对照。不要说格点、ET0、雨水账、雨水或账。min_c、max_c、mean_c 是气温摄氏度，precipitation_mm 是有效降雨的毫米数。antecedent_precipitation_mm 是本期开始前一段时间的有效降雨。prior_year 是去年同一段日期。weather 数组再写气象条件这一节，不要和结论用同一句话。",
    "sowing": "精量播种记录。sown_on 且 date_source 为播种日期时，这天才是播种当天，结论要写出这一天和 days_to_latest。date_source 为填写日期时只有 recorded_on，那是表单填写日，不要说成播种当天。variety 是品种，可以带一句，不要据此评价品种好坏。没有这项就不要写播种日期。",
    "gdd": "用逐日气温算出的积温，不是田间实测。base_c 是基数，正文不用写公式。period 是本期合计，since_sowing 是播种日到本期结束。sum 是积温，days 是有气温的天数，from 和 to 是起止日期。covers_sowing_day 为 false 时，积温不是从播种当天算起，要写出 from。出苗期积温偏低可以说出苗会慢。不要编一个该出苗的积温标准。没有这项就不要写积温。积温与 autumn_emergence 相互独立，缺出苗结果时仍要写已有的积温。",
    "autumn_emergence": "ww_autumn_soil_baseline_v1 对冬小麦秋季出苗的算法判定，不是人工目测，也不是精确出苗日或苗数。status 为 emerged、emerging、not_emerged 或 uncertain，正文用 status_zh。emerged_flag 表示已出苗。first_emerged_date 是算法确认持续抬升的日期。sowing_date 是出苗分析用的播种日。class_shares 是像元类别占比：within_noise 仍在本底噪声内，above_noise 已抬升但尚未持续，sustained 连续两日高于噪声。median_increment 是相对土壤本底的中位增量。share_ndvi_ge_0_30 只是对照比例，不参与判定。有这项就作为出苗判断的算法证据，和方位指数、天气积温一起看。没有这项就不要写算法出苗结论，也不要用它的缺席否定积温或天气。",
    "key_nodes": "人工填写的农事。task_name 是事项，submitted_on 是填写日期，answers 是人能看懂的填写内容。compare_with 是对照提示，不是要抄进正文的句子：用它判断指数和这件事对得上、还看不出来，还是对不上。没有记录就不要写农事，也不要写“没有关键节点”。",
    "soil": "地块原来的土壤底子，不是这次新测的土。status 为 ready 时写进结论：用土质、酸碱和能存多少水，说明它和本期出苗或干湿怎么一起看。有 reader_sentence 就用这句的意思改写，不要原句复述。status 不是 ready 就不要写土壤。不要出现缺肥、病虫害这些词。",
    "photo_review": "现场照片的画面检查。status 不是 reviewed 时，不要写照片。",
    "context_lines": "结论下面还会印的短句。不要原句复述。",
    "field_patterns": "持续偏低和均匀度的结构化结果，不是要复述的句子。persistent.regions 是连续几期都偏低并且连成片的方位；scattered 为 true 表示有偏低但没有连成一片；status 不是 ok 就不要写成持续偏弱。uniformity.direction：wider 是各处拉开，narrower 是更接近，similar 是看不出拉开。history_status 不是 compared 时，不要在结论里再写历史暂不评价。图下已经印有对应句子，结论里改用方位和指数写本期焦点。",
    "water_risk_lines": "已经写好的水分观测。只用这些数判断偏干还是偏湿，以及要不要留意浇水或排水。不要原句复述，也不要把「不判定」写进结论。看不准就说看不准，不要写成没有风险，也不要写现在还不能判断、先别下结论、只能当参考。不要把没有可用、没有记录或证据不足写进结论。",
    "stage": "种植方案里的计划生育阶段，不是田间实测的物候。",
    "stage_context": "当前生育阶段的对照，不是要写进正文的句子，也不是对本田的结论。focus 是这一阶段通常该看的问题，expected 是什么算常态、什么才值得写，ask 是这一阶段要回答的问题，background 是季节走向上的一句背景。不要复述这些句子。按这个阶段用遥感、天气和土壤写出本期不一样的地方。",
    "conclusion_task": "本期结论要回答的问题，不是要抄的句子。按这些问题用事实写成完整的一段，事实够时用六到十句。没有列出的资料不要编，也不要用空话把结论垫长。",
    "gap_days": "最近一次观测和对比基准之间相隔的天数。",
}


# 生育阶段对照：告诉模型这一阶段什么算常态、什么才值得写。不是结论模板。
_STAGE_CONTEXT: dict[str, dict[str, str]] = {
    "播种期": {
        "focus": "播种之后是否已经能看出苗的迹象",
        "expected": "播种当周通常仍看不到成片植被。低 NDVI 本身不是苗弱。只有已经能按方位分出有信号和没有信号时，才写出苗不齐的位置。",
        "ask": "看播种之后有没有方位已经出现植被信号。低覆盖本身不写苗弱。有天气和土壤时，说明它们对出苗是顺还是紧。有播种日期和积温时，用来看播了几天、热量够不够，不要编该出苗的积温标准。有秋季出苗算法结果时，优先用它判断有没有出苗迹象。",
    },
    "出苗期": {
        "focus": "出苗是否形成，以及何处没有植被信号或这次看不清",
        "expected": "覆盖低、NDVI 整体偏低是常态。值得写的是地块内部：哪些方位已经高出其余地方，更像已经出苗；哪些方位接近全田或仍明显更低，仍看不出苗；哪些方位这次读不到、不能判断出苗。有天气和土壤时，一起说明出苗条件和要重点看的方位。",
        "ask": "分清哪些方位已经出苗、哪些还没有、哪些看不清，并点出要重点看的方位。全田偏低只是背景。冷热、有效降雨和土壤能存多少水，用来说明出苗条件。有播种日期就写哪一天播的、到最近观测过了几天。积温偏低就说明出苗会慢，不要把这时的低 NDVI 写成苗弱，也不要编该出苗的积温标准。有秋季出苗算法结果时，优先用算法的 status 和首次确认日期，再对照方位。",
    },
    "分蘖期": {
        "focus": "覆盖是否在增加，以及增加是否只发生在部分方位",
        "expected": "覆盖通常缓慢增加。全田一起上升点明即可。只有部分方位上升、另一部分仍很低时，写出没有跟上的方位。",
        "ask": "看覆盖增加是全田一起，还是只在部分方位。没跟上的方位要写出来。天气和土壤用来看这期水分和出苗基础，不要只报指数升了多少。",
    },
    "越冬期": {
        "focus": "越冬期间指数是否稳定，有没有成片异常低的方位",
        "expected": "生长基本停滞，指数持平或小幅下降是常态。不要把小幅下降写成苗情问题。只有方位之间拉开，或有成片持续偏低时，才作为本期重点。",
        "ask": "看指数是否稳住。冷热和有效降雨是这期要写的。小幅下降不要写成苗情问题。只有方位拉开或成片持续偏低，才作为空间重点。",
    },
    "返青期": {
        "focus": "恢复生长是否开始，哪些方位还没有回升",
        "expected": "指数通常由低位回升。回升如果只出现在部分方位，写出还没回升的方位。全田一起回升就不要再写成不齐。",
        "ask": "看恢复是全田回升，还是有方位还没起来。冷热和有效降雨用来看回升的条件。没回升的方位要写出来。",
    },
    "拔节孕穗期": {
        "focus": "冠层是否在快速形成，偏低是否集中在某些方位",
        "expected": "生物量通常快速增加，NDVI 容易饱和，NDRE 和 EVI 更能看出差别。全田上升是常态。只有明显落后的方位，或该升未升，才作为本期重点。",
        "ask": "看冠层是否在形成。NDRE 和 EVI 上哪些方位落后要写出来。有效降雨和土壤保水用来看这期水分条件，不要只写指数。",
    },
    "抽穗期": {
        "focus": "冠层是否维持在高位，有没有成片掉下去的方位",
        "expected": "冠层通常在全季高位附近。小幅波动不必展开。成片明显低于其余方位时，写出方位和哪个指数偏低。",
        "ask": "看冠层是否还在高位，有没有方位已经掉下去。天气只写真有的冷热和有效降雨，并说明和冠层怎么一起看。",
    },
    "灌浆期": {
        "focus": "下降是全田同步，还是局部已经更像低覆盖",
        "expected": "叶片逐步衰老，指数由高位缓慢下降是常态。一部分方位已经落到明显低位、另一部分仍高时，按方位说明哪些更像已经收获或覆盖已经掉下去，哪些仍像未收。分不开就说明分不开。",
        "ask": "看下降是全田一起，还是有的方位已经更像低覆盖、有的仍高。热和有效降雨用来看灌浆条件。分不开就写分不开。",
    },
    "成熟期": {
        "focus": "哪些方位更像已经收获，哪些仍保持冠层",
        "expected": "转黄和含水下降会让 NDVI、NDRE、NDMI 一起下降，这是成熟本身。能分开时，用方位上的高低和相对上期的降幅说明更像已收还是仍未收。拉不开或有效观测不够时，直接说这次分不出收获范围。",
        "ask": "看哪些方位更像已经收获，哪些仍有冠层。拉不开就写这次分不出收获范围。天气不要改写成产量。",
    },
}

# 农事对照提示。只解释记录和指数该怎么看，不规定结论原句。
_TASK_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("收获", "收割"),
        "收获后相应范围的 NDVI、NDRE 应落到低覆盖。用方位上的高低和降幅对照记录里的时间；对不上就写对不上。",
    ),
    (
        ("出苗水", "灌溉", "浇水", "灌水", "滴灌", "喷灌", "EMERGENCE_WATER", "OVERWINTER_WATER"),
        "灌溉后 NDMI 不一定当天变化。对照的是其后水分指数和干湿证据是否与这次灌溉相符。",
    ),
    (
        ("出苗", "EMERGENCE_CHECK"),
        "这是人填写的出苗情况。用它对照哪些方位已有植被信号、哪些仍低或看不清，不要把它说成卫星测到的苗数。",
    ),
    (
        ("播种", "补种", "PRECISION_SOWING"),
        "播种之后要过一段时间才应出现成片植被信号。优先用 sowing 里的播种日期和过了几天，再用积温看热量够不够。对照的是其后 NDVI 是否按方位出现，而不是播种当周偏低本身。",
    ),
    (
        ("施肥", "追肥", "基肥", "BASE_FERTILIZER"),
        "记录只说明施过肥，不能据此诊断缺肥。施肥后若主要指数上升，可以说时间上对得上；没有变化就只说指数上还看不出。",
    ),
    (
        ("打药", "喷药", "植保"),
        "记录只说明做过植保。不要诊断病虫害，也不要建议再打药。只在指数变化和时间对得上或对不上时提一句。",
    ),
    (
        ("镇压", "POST_SOWING_ROLL"),
        "镇压不直接改变植被指数，不要从指数评价镇压质量。",
    ),
)


def bailian_settings() -> dict[str, str]:
    return {
        "api_key": (os.environ.get("BAILIAN_API_KEY") or "").strip(),
        "base_url": (os.environ.get("BAILIAN_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        ),
        "model": (os.environ.get("BAILIAN_MODEL") or DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
    }


def _bounded_int(name: str, default: int, floor: int, ceiling: int) -> tuple[int, bool]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default, False
    try:
        value = int(raw)
    except ValueError:
        return default, True
    applied = min(ceiling, max(floor, value))
    return applied, applied != value


def _is_deepseek(model: str) -> bool:
    return "deepseek" in (model or "").lower()


def output_ceiling(model: str) -> int:
    """Documented completion cap for this model name. Not a universal provider limit."""
    if _is_deepseek(model):
        return DEEPSEEK_OUTPUT_MAX
    return OTHER_OUTPUT_MAX


def completion_budget(model: str) -> tuple[int, bool]:
    """Tokens requested for one completion, after the floor and the model ceiling."""
    ceiling = output_ceiling(model)
    default = min(DEFAULT_MAX_TOKENS, ceiling)
    floor = min(MIN_MAX_TOKENS, ceiling)
    return _bounded_int("BAILIAN_MAX_TOKENS", default, floor, ceiling)


def thinking_requested(model: str) -> bool | None:
    """DeepSeek hybrid thinking switch. None means do not send the field.

    Bailian documents enable_thinking for deepseek-v4.1-flash. The 1017 snapshot
    is that hybrid model and thinks by default. thinking_budget is not supported
    on the snapshot, so the switch is how the JSON keeps the completion budget.
    """
    if not _is_deepseek(model):
        return None
    raw = (os.environ.get("BAILIAN_ENABLE_THINKING") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def reasoning_effort() -> str:
    """Used only while thinking is on. Bailian accepts low, high, and max."""
    raw = (os.environ.get("BAILIAN_REASONING_EFFORT") or "low").strip().lower()
    if raw in {"low", "high", "max"}:
        return raw
    return "low"


def completion_timeout_s(
    explicit: float | None = None, tokens: int = DEFAULT_MAX_TOKENS
) -> tuple[float, bool]:
    """Client wait. Unset env scales with the completion budget, capped at 600s."""
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError):
            return 180.0, True
        applied = min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, value))
        return float(applied), applied != value
    raw = (os.environ.get("BAILIAN_TIMEOUT_S") or "").strip()
    if not raw:
        scaled = min(MAX_TIMEOUT_S, max(180, 30 + int(tokens) // 64))
        return float(scaled), False
    seconds, clamped = _bounded_int(
        "BAILIAN_TIMEOUT_S", 180, MIN_TIMEOUT_S, MAX_TIMEOUT_S
    )
    return float(seconds), clamped


def _next_budget(current: int, ceiling: int) -> int:
    return min(ceiling, max(current * 2, current + 8192))


def _content_text(content: Any) -> str:
    """Assistant content only. Reasoning parts are not text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "text")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def interpret_chat_completion(body: Any) -> tuple[str, dict[str, Any]]:
    """Return content text and a diagnostic that never includes reasoning text.

    HTTP 200 is not a successful body. ``reasoning_content`` is counted and
    then discarded. It is not parsed as the report.
    """
    choice: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
        if isinstance(body.get("usage"), dict):
            usage = body["usage"]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = _content_text(message.get("content"))
    reasoning = message.get("reasoning_content")
    tokens = usage.get("completion_tokens")
    return text, {
        "finish_reason": choice.get("finish_reason"),
        "completion_tokens": tokens if isinstance(tokens, int) else None,
        "content_chars": len(text.strip()),
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
    }


def _section_has_facts(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        return any(_section_has_facts(item) for item in value.values())
    if isinstance(value, list):
        return any(_section_has_facts(item) for item in value)
    return True


def missing_required_sections(
    normalized: dict[str, Any] | None, payload: dict[str, Any]
) -> list[str]:
    """Sections the PDF would leave blank even though the facts asked for them."""
    narrative = normalized or {}
    missing: list[str] = []
    if not narrative.get("conclusion"):
        missing.append("conclusion")
    # field_notes 可以是空的：没有具体方位或对不上的农事时，不要求用巡查套话填一节。
    changes = narrative.get("changes") or []
    has_change = any(isinstance(row, dict) and row.get("sentence") for row in changes)
    if payload.get("metrics") and not has_change:
        missing.append("changes")
    if payload.get("metrics") and not narrative.get("metric_notes"):
        missing.append("metric_notes")
    if _section_has_facts(payload.get("weather")) and not narrative.get("weather"):
        missing.append("weather")
    if _section_has_facts(payload.get("surrounding")) and not narrative.get(
        "surrounding"
    ):
        missing.append("surrounding")
    if _section_has_facts(payload.get("infield")) and not narrative.get("infield"):
        missing.append("infield")
    return missing


def _unusable_kind(
    text: str,
    parsed: dict[str, Any] | None,
    normalized: dict[str, Any] | None,
    diag: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[str | None, list[str]]:
    """Name why this completion is not a complete report. None means the sections are filled."""
    length = diag.get("finish_reason") == "length"
    if not text.strip():
        if length:
            return "truncated_response", ["conclusion", "field_notes"]
        if diag.get("reasoning_chars"):
            return "empty_with_reasoning", ["conclusion", "field_notes"]
        return "empty_content", ["conclusion", "field_notes"]
    if parsed is None:
        kind = "truncated_response" if length else "invalid_json"
        return kind, ["conclusion", "field_notes"]
    if not _has_wording(normalized or {}):
        kind = "truncated_response" if length else "empty_fields"
        return kind, missing_required_sections(normalized, payload)
    missing = missing_required_sections(normalized, payload)
    if missing:
        kind = "truncated_response" if length else "incomplete_fields"
        return kind, missing
    return None, []


def _safe_disconnect_reason(exc: BaseException, secret: str = "") -> str:
    """保留一句断连原因。去掉密钥、换行和过长原文。"""
    text = " ".join(str(exc).split())
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|authorization)\s*[=:]\s*\S+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    return text[:180]


def _disconnect_detail(exc: BaseException, secret: str = "") -> tuple[str, str | None]:
    """异常类型之外，再留底层原因的类型和一句安全说明。"""
    reason = _safe_disconnect_reason(exc, secret)
    cause = exc.__cause__
    if cause is None or cause is exc:
        return reason, None
    extra = _safe_disconnect_reason(cause, secret)
    if extra and extra not in reason:
        reason = (f"{reason} ({extra})" if reason else extra)[:180]
    return reason, type(cause).__name__


def _log_unusable(error: str, diag: dict[str, Any], *, model: str) -> None:
    # 只记长度、异常类型和截断后的断连原因。不记密钥、正文或 reasoning 原文。
    logger.warning(
        "biweekly_narrative_unusable",
        error=error,
        model=model,
        finish_reason=diag.get("finish_reason"),
        completion_tokens=diag.get("completion_tokens"),
        content_chars=diag.get("content_chars"),
        reasoning_chars=diag.get("reasoning_chars"),
        max_tokens=diag.get("max_tokens"),
        max_tokens_clamped=diag.get("max_tokens_clamped"),
        http_status=diag.get("http_status"),
        timeout_s=diag.get("timeout_s"),
        attempts=diag.get("attempts"),
        exception_type=diag.get("exception_type"),
        disconnect_reason=diag.get("disconnect_reason"),
        cause_type=diag.get("cause_type"),
    )


def _soil_for_model(facts: dict[str, Any]) -> dict[str, Any] | None:
    soil = facts.get("soil_context")
    if not isinstance(soil, dict):
        return None
    if soil.get("status") != "ready":
        return {"status": soil.get("status") or "absent"}
    from app.reports.biweekly.conclusion_context import _texture_zh, soil_reader_line

    body = {
        "status": "ready",
        "texture": _texture_zh(soil.get("texture")) or None,
        "ph": soil.get("ph"),
        "som_g_kg": soil.get("som_g_kg"),
        "rootzone_awc_mm": soil.get("rootzone_awc_mm"),
    }
    sentence = soil_reader_line(facts)
    if sentence:
        body["reader_sentence"] = sentence
    return body



def _autumn_emergence_for_model(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Optional algorithm evidence. Missing emergence must not drop weather or 积温."""
    from agric_satellite_analysis_common.autumn_emergence import (
        daily_fields_from_products,
        summary_for_model,
    )

    raw = facts.get("autumn_emergence")
    if isinstance(raw, dict) and raw.get("source") == "algorithm" and "pixels" not in raw:
        return raw
    fields = facts.get("autumn_emergence_days")
    if not isinstance(fields, dict) or not fields:
        fields = daily_fields_from_products(facts.get("products") or [])
    latest = raw if isinstance(raw, dict) and raw.get("status") else None
    return summary_for_model(
        daily_fields=fields if isinstance(fields, dict) else None,
        latest=latest,
        timeseries=raw.get("timeseries") if isinstance(raw, dict) else None,
        land=facts.get("field") if isinstance(facts.get("field"), dict) else facts,
    )


def narrative_input(facts: dict[str, Any]) -> dict[str, Any]:
    """Compact, already-computed facts. No imagery and no judgment."""
    from app.reports.biweekly.report_text import period_text, stage_summary

    comparison = facts.get("comparison") or {}
    plan = facts.get("report_metrics") or {}
    stage = stage_summary(facts)
    metrics: dict[str, Any] = {}
    for role, keys in (
        ("primary", plan.get("primary")),
        ("supporting", plan.get("supporting")),
    ):
        for key in keys or []:
            item = (comparison.get("metrics") or {}).get(key) or {}
            spec = METRIC_SPECS.get(key)
            metrics[key] = {
                "name": spec.label if spec else metric_abbr(key),
                "meaning": spec.purpose if spec else "",
                "role": "主要" if role == "primary" else "辅助",
                "latest": _index(item.get("latest")),
                "baseline": _index(item.get("baseline")),
                "delta": _index(item.get("delta")),
                "value_direction": {"up": "升高", "down": "降低", "flat": "持平"}.get(
                    item.get("direction") or ""
                ),
            }
    quality = facts.get("data_quality") or {}
    payload = {
        "parameter_guide": PARAMETER_GUIDE,
        "period": period_text(facts),
        "stage": stage.get("text"),
        "stage_note": stage.get("source"),
        "stage_context": _stage_context(stage, facts),
        "latest_date": comparison.get("latest_date"),
        "baseline_date": comparison.get("baseline_date"),
        "gap_days": comparison.get("gap_days"),
        "metrics": metrics,
        "used_observations": quality.get("used_dates"),
        "rejected_observations": _rejected_observations(quality),
        "infield": _infield_facts(facts),
        "surrounding": _surrounding_facts(facts),
        "weather": _weather_facts(facts),
        "sowing": sowing_for_model(facts),
        "gdd": gdd_for_model(facts),
        "autumn_emergence": _autumn_emergence_for_model(facts),
        "key_nodes": _key_nodes_for_model(facts),
        "soil": _soil_for_model(facts),
        "photo_review": {
            "status": (facts.get("photo_review") or {}).get("status"),
            "count": (facts.get("photo_review") or {}).get("count") or 0,
            "note": (facts.get("photo_review") or {}).get("note"),
        },
        "context_lines": list(
            (facts.get("conclusion_context") or {}).get("lines") or []
        ),
        "field_patterns": _pattern_facts(facts),
        "water_risk_lines": _without_missing_weather_lines(
            (facts.get("water_risk") or {}).get("lines")
        ),
    }
    payload["conclusion_task"] = _conclusion_task(payload)
    return payload


def _listed_stages(facts: dict[str, Any]) -> list[str]:
    """Stage names on the facts, even when the plan source sentence is absent."""
    phase = facts.get("phase") or {}
    rows = [
        row
        for row in (phase.get("observation_stages") or phase.get("planned_days") or [])
        if isinstance(row, dict) and row.get("stage_code")
    ]
    return list(dict.fromkeys(str(row["stage_code"]) for row in rows))


def _stage_context(stage: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any] | None:
    """Stage reading brief. Empty when the period has no planned stage."""
    latest = str(stage.get("latest_stage") or "")
    stages = [str(item) for item in (stage.get("stages") or []) if item]
    if not latest:
        stages = _listed_stages(facts)
        latest = stages[-1] if stages else ""
    if not latest:
        return None
    background = STAGE_BACKGROUND.get(latest) or {}
    reading = _STAGE_CONTEXT.get(latest) or {}
    body: dict[str, Any] = {"stage": latest}
    if stages:
        body["stages"] = stages
    if stage.get("kind") == "split":
        body["spans_stages"] = True
    if background.get("text"):
        body["background"] = background["text"]
    if reading.get("focus"):
        body["focus"] = reading["focus"]
    if reading.get("expected"):
        body["expected"] = reading["expected"]
    if reading.get("ask"):
        body["ask"] = reading["ask"]
    return body


def _conclusion_task(payload: dict[str, Any]) -> list[str]:
    """Questions for this period. The model answers them; it does not copy them."""
    stage = str((payload.get("stage_context") or {}).get("stage") or "")
    lines: list[str] = []
    ask = (_STAGE_CONTEXT.get(stage) or {}).get("ask")
    if ask:
        lines.append(ask)
    lines.append(
        "结论写完整。事实够的时候用六到十句，把方位、指数变化、天气、土壤和已有的播种、积温或农事连成判断，不要收成一两句。"
    )
    if _weather_has_numbers(payload.get("weather")):
        lines.append(
            "天气里有本期冷热或有效降雨。写进结论，并说明对这个生育阶段意味着什么。有去年同期就比较。"
        )
    sowing = payload.get("sowing")
    if isinstance(sowing, dict) and (sowing.get("sown_on") or sowing.get("recorded_on")):
        lines.append(
            "有播种记录。播种日期就写播种那天和到最近观测过了几天；只有填写日期就不要说成播种当天。"
        )
    if isinstance(payload.get("gdd"), dict):
        lines.append(
            "有积温。出苗期用来说明出苗会不会慢，写出合计、天数和起止日期。不要编该出苗的积温标准。没有出苗算法结果时，积温仍要写。"
        )
    emergence = payload.get("autumn_emergence")
    if isinstance(emergence, dict) and emergence.get("status"):
        lines.append(
            "有秋季出苗算法结果（ww_autumn_soil_baseline_v1）。按 status 和 first_emerged_date 写，这是算法证据，不要写成苗数或精确出苗日。没有天气或积温时，算法结果仍要写。"
        )
    soil = payload.get("soil")
    if isinstance(soil, dict) and soil.get("status") == "ready":
        lines.append(
            "土壤底子是现成的。用土质、酸碱和能存多少水，跟本期遥感和干湿一起看，不要另作诊断。"
        )
    return lines


def _task_hint(node: dict[str, Any]) -> str | None:
    """Short comparison note for a recorded task. Not a sentence to copy."""
    name = str(node.get("task_name") or "")
    code = str(node.get("task_code") or "").upper()
    tokens = {part for part in re.split(r"[^A-Za-z0-9]+", code) if part}
    if code:
        tokens.add(code)
    for keys, hint in _TASK_HINTS:
        for key in keys:
            if re.search(r"[\u4e00-\u9fff]", key):
                if key in name:
                    return hint
            elif key.upper() in tokens:
                return hint
    return None


def _index(value: Any) -> str | None:
    """Two decimals, same as the evidence table, so the prose does not dump raw floats."""
    if value is None:
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _rejected_observations(quality: dict[str, Any]) -> list[dict[str, Any]]:
    from app.reports.biweekly.report_text import REJECT_REASONS

    rows = []
    for row in quality.get("rejected") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "date": row.get("date"),
                "reason": REJECT_REASONS.get(row.get("reason") or "", "质量不符合要求"),
                "cloud_pct": _whole(row.get("cloud_pct")),
            }
        )
    return rows


def _infield_facts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in facts.get("infield") or []:
        stats = item.get("stats") or {}
        relative = item.get("relative") or {}
        regions = []
        for region in relative.get("regions") or []:
            if not region.get("name"):
                continue
            entry: dict[str, Any] = {"name": region["name"]}
            if region.get("share") is not None:
                entry["share_percent"] = _whole(float(region["share"]) * 100)
            regions.append(entry)
        zones = []
        for zone in item.get("zones") or []:
            if not isinstance(zone, dict) or not zone.get("name"):
                continue
            packed: dict[str, Any] = {
                "name": zone["name"],
                "level": zone.get("level"),
            }
            if zone.get("median") is not None:
                packed["median"] = _index(zone.get("median"))
            if zone.get("delta_median") is not None:
                packed["delta_median"] = _index(zone.get("delta_median"))
            if zone.get("valid_fraction") is not None:
                packed["valid_percent"] = _whole(float(zone["valid_fraction"]) * 100)
            if zone.get("area_fraction") is not None:
                packed["area_percent"] = _whole(float(zone["area_fraction"]) * 100)
            zones.append(packed)
        row: dict[str, Any] = {
            "metric": item.get("metric"),
            "median": _index(stats.get("median")),
            "p10": _index(stats.get("p10")),
            "p90": _index(stats.get("p90")),
            "regions": regions,
        }
        if item.get("spatial_split"):
            row["spatial_split"] = item.get("spatial_split")
        if zones:
            row["zones"] = zones
        rows.append(row)
    return rows


def _surrounding_facts(facts: dict[str, Any]) -> dict[str, Any]:
    meta = facts.get("surrounding_comparison_meta") or {}
    rows = []
    for row in meta.get("comparisons") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "date": row.get("date"),
                "field": _index(row.get("field")),
                "nearby_1km": _index(row.get("surrounding_1km")),
                "field_minus_nearby": _index(row.get("difference")),
            }
        )
    cover = meta.get("cover_2km")
    return {
        "comparisons": rows,
        "cover_2km_percent": None if cover is None else _whole(float(cover) * 100),
    }


def _prior_year_weather(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("mean_c") is None and summary.get("precipitation_mm") is None:
        return None
    return {
        "start": summary.get("start"),
        "end": summary.get("end"),
        "min_c": _index(summary.get("min_c")),
        "max_c": _index(summary.get("max_c")),
        "mean_c": _index(summary.get("mean_c")),
        "precipitation_mm": _index(summary.get("precipitation_mm")),
    }


def _key_nodes_for_model(facts: dict[str, Any]) -> list[dict[str, Any]]:
    from app.reports.biweekly.conclusion_context import select_key_nodes

    payload = facts.get("key_node_records") or {}
    period = facts.get("period") or {}
    phase = facts.get("phase") or {}
    names = [
        str(row.get("stage_code") or "")
        for row in (phase.get("observation_stages") or phase.get("planned_days") or [])
        if isinstance(row, dict)
    ]
    selected = select_key_nodes(
        list(payload.get("nodes") or []),
        period_start=str(period.get("start") or "")[:10],
        period_end=str(period.get("end_inclusive") or "")[:10],
        stage_text="、".join(dict.fromkeys(name for name in names if name)),
    )
    compact = []
    for node in selected:
        item = {
            "task_name": node.get("task_name"),
            "submitted_on": node.get("submitted_on"),
            "relevance": "本期" if node.get("relevance") == "in_period" else "当前阶段",
            "answers": [
                {"label": answer.get("label"), "value": answer.get("value")}
                for answer in (node.get("answers") or [])
            ],
        }
        hint = _task_hint(node)
        if hint:
            item["compare_with"] = hint
        compact.append(item)
    return compact


def _pattern_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Structured within-field pattern. The printed sentences stay off the prompt."""
    patterns = facts.get("field_patterns") or {}
    persistent = []
    for item in patterns.get("persistent") or []:
        if not isinstance(item, dict):
            continue
        regions = [
            region.get("name")
            for region in (item.get("regions") or [])
            if isinstance(region, dict) and region.get("name")
        ]
        persistent.append(
            {
                "metric": metric_abbr(str(item.get("metric") or "")),
                "status": item.get("status"),
                "dates": list(item.get("dates") or []),
                "regions": regions,
                "clustered": bool(item.get("n_persistent_clustered")),
                "scattered": bool(item.get("n_persistent"))
                and not item.get("n_persistent_clustered"),
            }
        )
    uniformity = []
    for item in patterns.get("uniformity") or []:
        if not isinstance(item, dict):
            continue
        spread = item.get("spread")
        uniformity.append(
            {
                "metric": metric_abbr(str(item.get("metric") or "")),
                "status": item.get("status"),
                "direction": item.get("direction"),
                "dates": list(item.get("dates") or []),
                "spread": [_index(value) for value in spread] if spread else None,
            }
        )
    history = patterns.get("history") if isinstance(patterns.get("history"), dict) else {}
    return {
        "persistent": persistent,
        "uniformity": uniformity,
        "history_status": history.get("status"),
    }


def _weather_facts(facts: dict[str, Any]) -> dict[str, Any] | None:
    period = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    body = {
        "period_days": period.get("days"),
        "min_c": _index(period.get("min_c")),
        "max_c": _index(period.get("max_c")),
        "mean_c": _index(period.get("mean_c")),
        "temperature_days": period.get("temperature_days"),
        "precipitation_mm": _index(period.get("precipitation_mm")),
        "precipitation_days": period.get("precipitation_days"),
        "antecedent_days": antecedent.get("days"),
        "antecedent_precipitation_mm": _index(antecedent.get("precipitation_mm")),
        "prior_year": _prior_year_weather(facts.get("weather_same_period_prior_year")),
    }
    # 没有气温和降水时不把一串空值交给模型，避免结论写成「没有记录」。
    if not _weather_has_numbers(body):
        return None
    return body


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def _fact_blob(payload: dict[str, Any]) -> str:
    """Digits may be copied from parameter values, not from the explanatory guide."""
    values = {key: value for key, value in payload.items() if key != "parameter_guide"}
    return json.dumps(values, ensure_ascii=False)


def _visible_text(sentence: str) -> str:
    return re.sub(r"<[^>]+>", "", sentence)


def _numbers_are_copied(sentence: str, blob: str) -> bool:
    """A digit is allowed when the same value already appears in the facts JSON."""
    visible = _visible_text(sentence)
    known = [float(token) for token in _NUMBER.findall(blob)]
    for token in _NUMBER.findall(visible):
        if token in blob:
            continue
        try:
            value = float(token)
        except ValueError:
            return False
        if not any(
            math.isclose(value, item, rel_tol=0, abs_tol=1e-6) for item in known
        ):
            return False
    return True


_MISSING_WEATHER_TALK = (
    "没有可用",
    "没有记录",
    "无从对照",
    "未取得",
    "证据不足",
)


def _without_missing_weather_lines(lines: Any) -> list[str]:
    """缺测说明留给说明页，不交给模型改写成结论。"""
    kept: list[str] = []
    for line in lines or []:
        text = str(line)
        if any(word in text for word in _MISSING_WEATHER_TALK):
            continue
        kept.append(text)
    return kept


def _drop_missing_weather_talk(text: str) -> str:
    """删掉「没有可用记录」这类分句，留下同一句里已经写出的数值。"""
    sentences = re.split(r"(?<=。)", text)
    kept_sentences: list[str] = []
    for sentence in sentences:
        parts = re.split(r"([，,；;])", sentence)
        rebuilt: list[str] = []
        index = 0
        while index < len(parts):
            piece = parts[index]
            sep = parts[index + 1] if index + 1 < len(parts) else ""
            if any(word in piece for word in _MISSING_WEATHER_TALK):
                index += 2
                continue
            rebuilt.append(piece)
            if sep:
                rebuilt.append(sep)
            index += 2
        piece = "".join(rebuilt)
        piece = re.sub(r"^[，,；;\s]+", "", piece)
        piece = re.sub(r"[，,；;]+\s*(?=。)", "", piece)
        piece = piece.strip()
        if piece and piece not in {"。", "，"}:
            kept_sentences.append(piece)
    return "".join(kept_sentences)


_BANNED = (
    "低值",
    "参考线",
    "预警线",
    "达标",
    "确诊",
    "灾损",
    "产量",
    "好转",
    "变差",
    "模型",
    "人工智能",
)

# 模型会把推脱判断的话写进给种植户看的句子。删掉这些说法，不另补同一句巡查套话。
_CANNED_VISIT = re.compile(r"[，,；;、]?\s*建议去地里看看确认一下[。．.]?")
_HEDGE_RES = (
    re.compile(
        r"[，,；;]?\s*(?:但|不过|而且|并且)?\s*"
        r"(?:先别急着下结论|先别下结论|先不下结论|别急着下结论|"
        r"不要下结论|别下结论|不下结论|不能下结论|不先下结论|先别急着)"
        r"[^。]*"
    ),
    re.compile(
        r"[，,；;]?\s*(?:只能当个?参考|仅供参考|只供参考|只作本期参考|只作参考|这是本期参考)"
        r"[^。]*"
    ),
    re.compile(r"[，,；;]?\s*(?:这只说明位置[，,]?\s*)?不判定[^。]*"),
    re.compile(r"[，,；;]?\s*(?:现在)?还不能(?:下)?判断[^。]*"),
    re.compile(r"[，,；;]?\s*现在看不出来[^。]*"),
    re.compile(r"[，,；;]?\s*不能确认或排除[^。]*"),
    re.compile(
        r"[，,；;]?\s*(?:可能是|也许是|就是|这是|说)?(?:缺肥|病虫害|闹病|缺什么)[^。，；]*"
    ),
)
def _plain_voice(text: str) -> str:
    """删掉推脱判断的话和反复出现的巡查套话，不另写一句代替。"""
    text = _drop_missing_weather_talk(text)
    text = text.replace("雨水账", "有效降雨").replace("雨水", "有效降雨")
    for pattern in _HEDGE_RES:
        text = pattern.sub("", text)
    text = _CANNED_VISIT.sub("", text)
    text = re.sub(r"<font\b[^>]*>\s*</font>", "", text)
    text = re.sub(r"<b>\s*</b>", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[，,；;\s]+", "", text)
    text = re.sub(r"[，,；;]{2,}", "，", text)
    text = re.sub(r"[，,；;]\s*(。|$)", r"\1", text)
    text = text.strip()
    if not text or text in {"。", "，"}:
        return ""
    if not text.endswith("。"):
        text = f"{text.rstrip('，,；; ')}。"
    return text


def _asserts_diagnosis(sentence: str) -> bool:
    """缺肥、病虫害、闹病和积水面积不进给种植户看的句子。"""
    return any(
        word in sentence for word in ("病虫害", "缺肥", "闹病", "缺什么", "积水面积")
    )


def _clean_sentence(sentence: str, blob: str, limit: int) -> str:
    text = _plain_voice(sentence.strip())
    if (
        not text
        or any(word in text for word in _BANNED)
        or _asserts_diagnosis(text)
        or re.search(r"\bAI\b", text, re.IGNORECASE)
        or not _numbers_are_copied(text, blob)
    ):
        return ""
    text = re.sub(r"<[^>]*$", "", text[:limit])
    return text


def _clean_list(rows: Any, blob: str, limit: int = 420, keep: int = 8) -> list[str]:
    if isinstance(rows, str):
        rows = [rows]
    cleaned = []
    for row in rows or []:
        text = _clean_sentence(str(row or ""), blob, limit)
        if text:
            cleaned.append(text)
    return cleaned[:keep]


def normalize_judgment(
    parsed: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Keep model wording whose digits were copied. Drop anything the facts do not support."""
    blob = _fact_blob(payload)
    allowed = set((payload.get("metrics") or {}).keys())
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in (parsed or {}).get("changes") or []:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "").lower()
        judgment = str(row.get("judgment") or "")
        sentence = str(row.get("sentence") or "").strip()
        if metric not in allowed or metric in seen or judgment not in JUDGMENTS:
            continue
        sentence = _clean_sentence(sentence, blob, 420)
        seen.add(metric)
        changes.append({"metric": metric, "judgment": judgment, "sentence": sentence})
    infield: dict[str, str] = {}
    for row in (parsed or {}).get("infield") or []:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "").lower()
        text = _clean_sentence(str(row.get("text") or ""), blob, 420)
        if metric in allowed and metric not in infield and text:
            infield[metric] = text
    summary = _clean_sentence(str((parsed or {}).get("summary") or ""), blob, 700)
    return {
        "summary": summary,
        "changes": changes,
        "conclusion": _clean_list((parsed or {}).get("conclusion"), blob, 640, keep=12),
        "field_notes": _clean_list((parsed or {}).get("field_notes"), blob),
        "metric_notes": _clean_list((parsed or {}).get("metric_notes"), blob),
        "infield": infield,
        "surrounding": _clean_list((parsed or {}).get("surrounding"), blob),
        "weather": _clean_list((parsed or {}).get("weather"), blob),
    }


_READER_NOTES = {
    "truncated_response": "本期模型文字未完成：输出长度用尽。数值和图表仍是程序计算结果。",
    "empty_content": "本期模型文字未完成：没有返回正文。数值和图表仍是程序计算结果。",
    "empty_with_reasoning": "本期模型文字未完成：没有返回正文。数值和图表仍是程序计算结果。",
    "invalid_json": "本期模型文字未完成：返回内容无法解析。数值和图表仍是程序计算结果。",
    "empty_fields": "本期模型文字未完成：结论为空。数值和图表仍是程序计算结果。",
    "incomplete_fields": "本期模型文字未完成：部分段落缺失。数值和图表仍是程序计算结果。",
    "timeout": "本期模型文字未完成：请求超时。数值和图表仍是程序计算结果。",
    "connection_error": "本期模型文字未完成：模型连接中断。数值和图表仍是程序计算结果。",
    "http_error": "本期模型文字未完成：模型服务暂时没有返回。数值和图表仍是程序计算结果。",
}
_GENERIC_READER_NOTE = "本期模型文字未完成。数值和图表仍是程序计算结果。"
_HIDDEN_ERRORS = {"not_configured", "no_metrics"}


def reader_note(narrative: dict[str, Any] | None) -> str:
    """One fixed sentence for the PDF. Empty when the model text is complete or not configured."""
    if not isinstance(narrative, dict):
        return ""
    error = narrative.get("error")
    if not error or error in _HIDDEN_ERRORS:
        return ""
    stored = narrative.get("reader_note")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return _READER_NOTES.get(str(error), _GENERIC_READER_NOTE)


def _apply_reader_note(judgment: dict[str, Any]) -> dict[str, Any]:
    note = reader_note(judgment)
    if note:
        judgment["reader_note"] = note
    return judgment


def empty_judgment(
    *,
    configured: bool,
    error: str | None = None,
    model: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "llm_configured": configured,
        "model": model,
        "summary": "",
        "changes": [],
        "conclusion": [],
        "field_notes": [],
        "metric_notes": [],
        "infield": {},
        "surrounding": [],
        "weather": [],
        "error": error,
    }
    if diagnostics:
        out["diagnostics"] = diagnostics
    return out


def _filled_score(judgment: dict[str, Any]) -> int:
    score = 0
    for key in ("conclusion", "field_notes", "metric_notes", "weather", "surrounding"):
        score += len(judgment.get(key) or [])
    score += sum(
        1
        for row in judgment.get("changes") or []
        if isinstance(row, dict) and row.get("sentence")
    )
    score += len(judgment.get("infield") or {})
    if judgment.get("summary"):
        score += 1
    return score


_WEATHER_MARKS = ("有效降雨", "降雨", "气温", "温度", "偏冷", "偏热", "偏暖", "低温", "高温")
_SOIL_MARKS = ("土壤", "土质", "酸碱")
# 结论漏了已有事实时，第二次请求只补这几句，不另给数字。
_REPAIR_NOTES = {
    "weather": "把本期冷热和有效降雨写进结论，有去年同期就比较，并说明对当前生育阶段意味着什么",
    "soil": "用土壤底子的土质、酸碱和能存多少水，跟本期遥感和干湿一起看，不要另作缺肥诊断",
    "emergence": "写明哪些方位已经有植被信号、哪些方位仍看不出苗、哪些这次看不清，并点出要重点看的方位",
    "sowing": "写出播种日期和到最近观测过了几天；只有填写日期时不要把填写日说成播种当天",
    "gdd": "把积温合计、有气温的天数和起止日期写进结论。出苗期积温偏低就说明出苗会慢，不要编该出苗的积温标准",
    "autumn_emergence": "把秋季出苗算法的判定写进结论：已出苗、出苗中、未出苗或不确定，有首次确认日期就写上。这是算法证据，不要写成苗数",
    "length": "结论太短。用六到十句把已经给出的方位、指数、天气、土壤、播种、积温或出苗算法写成完整判断，不要收成一两句，也不要编新的数字",
}


def _weather_has_numbers(weather: Any) -> bool:
    if not isinstance(weather, dict):
        return False
    for key in (
        "min_c",
        "max_c",
        "mean_c",
        "precipitation_mm",
        "antecedent_precipitation_mm",
    ):
        if weather.get(key) not in (None, ""):
            return True
    prior = weather.get("prior_year")
    if isinstance(prior, dict) and any(
        prior.get(key) not in (None, "")
        for key in ("min_c", "max_c", "mean_c", "precipitation_mm")
    ):
        return True
    return False


def _conclusion_text(narrative: dict[str, Any]) -> str:
    parts = [str(narrative.get("summary") or "")]
    parts.extend(str(item) for item in (narrative.get("conclusion") or []))
    return _visible_text("\n".join(parts))


def _zone_names(payload: dict[str, Any], levels: set[str]) -> list[str]:
    names: list[str] = []
    for item in payload.get("infield") or []:
        if not isinstance(item, dict):
            continue
        for zone in item.get("zones") or []:
            if not isinstance(zone, dict):
                continue
            name = str(zone.get("name") or "")
            if name and str(zone.get("level") or "") in levels and name not in names:
                names.append(name)
    return names


def conclusion_gaps(
    narrative: dict[str, Any] | None, payload: dict[str, Any]
) -> list[str]:
    """Facts the conclusion still skipped. Empty when this draft can be printed.

    只检查结论正文。天气写在后面的气象节、土壤只印在灰色短句里，都算没写进结论。
    """
    if not narrative:
        return []
    text = _conclusion_text(narrative)
    gaps: list[str] = []
    if _weather_has_numbers(payload.get("weather")) and not any(
        mark in text for mark in _WEATHER_MARKS
    ):
        gaps.append("weather")
    soil = payload.get("soil")
    if isinstance(soil, dict) and soil.get("status") == "ready" and not any(
        mark in text for mark in _SOIL_MARKS
    ):
        gaps.append("soil")
    stage = str((payload.get("stage_context") or {}).get("stage") or "")
    if stage == "出苗期" and not _emergence_covered(text, payload):
        gaps.append("emergence")
    if stage in {"出苗期", "播种期"} and not _sowing_covered(text, payload.get("sowing")):
        gaps.append("sowing")
    if stage in {"出苗期", "播种期"} and not _gdd_covered(text, payload.get("gdd")):
        gaps.append("gdd")
    if not _autumn_emergence_covered(text, payload.get("autumn_emergence")):
        gaps.append("autumn_emergence")
    if _conclusion_is_thin(text, payload):
        gaps.append("length")
    return gaps


# 事实已经有好几类时，一两句结论印出来是空的。短过这一长度就再要一稿。
_THIN_CONCLUSION_CHARS = 160


def _plain_chars(text: str) -> int:
    plain = re.sub(r"<[^>]+>", "", text)
    return len(re.sub(r"\s+", "", plain))


def _conclusion_topic_count(payload: dict[str, Any]) -> int:
    count = 0
    if payload.get("metrics"):
        count += 1
    if payload.get("infield"):
        count += 1
    if _weather_has_numbers(payload.get("weather")):
        count += 1
    soil = payload.get("soil")
    if isinstance(soil, dict) and soil.get("status") == "ready":
        count += 1
    sowing = payload.get("sowing")
    if isinstance(sowing, dict) and (sowing.get("sown_on") or sowing.get("recorded_on")):
        count += 1
    if isinstance(payload.get("gdd"), dict):
        count += 1
    emergence = payload.get("autumn_emergence")
    if isinstance(emergence, dict) and emergence.get("status"):
        count += 1
    if payload.get("key_nodes"):
        count += 1
    return count


def _conclusion_is_thin(text: str, payload: dict[str, Any]) -> bool:
    """有多项事实却只写了一小段时，要求把结论写完整。"""
    if _conclusion_topic_count(payload) < 3:
        return False
    return _plain_chars(text) < _THIN_CONCLUSION_CHARS


def _sowing_covered(text: str, sowing: Any) -> bool:
    """出苗和播种阶段有播种当天时，结论要写出这一天。"""
    if not isinstance(sowing, dict) or not sowing.get("sown_on"):
        return True
    day = str(sowing["sown_on"])
    month = str(int(day[5:7]))
    dom = str(int(day[8:10]))
    has_day = day in text or (f"{month}月" in text and f"{dom}日" in text)
    return "播种" in text and has_day


def _gdd_covered(text: str, gdd: Any) -> bool:
    if not isinstance(gdd, dict):
        return True
    if not (gdd.get("period") or gdd.get("since_sowing")):
        return True
    return "积温" in text


def _autumn_emergence_covered(text: str, emergence: Any) -> bool:
    """算法结果在结论里要点出判定，不要求复述方法名。"""
    if not isinstance(emergence, dict) or not emergence.get("status"):
        return True
    label = str(emergence.get("status_zh") or "")
    if label and label in text:
        return True
    if "算法" in text and "出苗" in text:
        return True
    return False


def _emergence_covered(text: str, payload: dict[str, Any]) -> bool:
    """出苗期高低已经拉开时，结论要同时点出有信号的方位和仍低或看不清的方位。"""
    higher = _zone_names(payload, {"higher"})
    quiet = [
        name
        for name in _zone_names(payload, {"lower", "similar", "unreadable"})
        if name not in set(higher)
    ]
    split = any(
        isinstance(item, dict) and item.get("spatial_split") == "split"
        for item in (payload.get("infield") or [])
    )
    if split and higher and quiet:
        return any(name in text for name in higher) and any(name in text for name in quiet)
    if "出苗" in text:
        return True
    named = higher or quiet
    return any(name in text for name in named) if named else True


def _user_message(payload: dict[str, Any], repair: list[str] | None) -> str:
    text = (
        "下面是参数说明和本期参数。请按本期事实写成报告，只输出 JSON：\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    if repair:
        notes = "；".join(
            _REPAIR_NOTES[item] for item in repair if item in _REPAIR_NOTES
        )
        text += (
            "\n上一稿结论没有写全已经给出的事实。请重写 JSON，在 conclusion 里补上："
            + notes
            + "。数字和日期仍只能用参数里已有的。"
        )
    return text


def _chat_body(
    cfg: dict[str, str],
    payload: dict[str, Any],
    *,
    max_tokens: int,
    thinking: bool | None,
    send_thinking: bool,
    repair: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(payload, repair)},
        ],
    }
    # 原始 HTTP，不是 OpenAI SDK，所以 enable_thinking 放在正文顶层。
    # 不发送 thinking_budget：百炼参数表标明该快照不支持。
    if send_thinking and thinking is not None:
        body["enable_thinking"] = thinking
        if thinking:
            body["reasoning_effort"] = reasoning_effort()
    return body


def _base_diag(
    *,
    max_tokens: int,
    tokens_clamped: bool,
    timeout_s: float,
    timeout_clamped: bool,
    thinking: bool | None,
    send_thinking: bool,
) -> dict[str, Any]:
    return {
        "max_tokens": max_tokens,
        "max_tokens_clamped": tokens_clamped,
        "timeout_s": timeout_s,
        "timeout_clamped": timeout_clamped,
        "enable_thinking": thinking if send_thinking else None,
        "thinking_field_sent": send_thinking and thinking is not None,
    }


def generate_metric_judgment(
    facts: dict[str, Any],
    *,
    timeout: float | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Ask the configured model for wording. A failed body stays empty and named.

    Does not raise. Does not copy reasoning into the report. At most two calls:
    the second turns thinking off, raises the budget after a length stop,
    repeats once after timeout, a dropped connection, 429, or 5xx, or asks
    again when the conclusion skipped weather, soil, or emergence places that
    were already in the facts. It does not invent sentences.
    """
    cfg = bailian_settings()
    payload = narrative_input(facts)
    if not payload["metrics"]:
        return empty_judgment(
            configured=bool(cfg["api_key"]), error="no_metrics", model=cfg["model"]
        )
    if not cfg["api_key"]:
        return empty_judgment(configured=False, error="not_configured", model=None)
    configured_tokens, tokens_clamped = completion_budget(cfg["model"])
    tokens = configured_tokens
    ceiling = output_ceiling(cfg["model"])
    thinking = thinking_requested(cfg["model"])
    send_thinking = thinking is not None
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own_client = client is None
    import httpx

    # 客户端等待按本次预算上限准备，第二次加预算时不会被第一次的超时截断。
    http = client or httpx.Client(timeout=completion_timeout_s(timeout, ceiling)[0])
    best: dict[str, Any] | None = None
    best_score = -1
    repair: list[str] | None = None
    try:
        for attempt in range(1, MAX_MODEL_CALLS + 1):
            timeout_s, timeout_clamped = completion_timeout_s(timeout, tokens)
            body = _chat_body(
                cfg,
                payload,
                max_tokens=tokens,
                thinking=thinking,
                send_thinking=send_thinking,
                repair=repair,
            )
            # 加预算的第二次请求不是“被上限截断”。只有请求值被夹到模型上限时才记 clamped。
            at_model_cap = tokens == ceiling and configured_tokens > ceiling
            diag = _base_diag(
                max_tokens=tokens,
                tokens_clamped=tokens_clamped or at_model_cap,
                timeout_s=timeout_s,
                timeout_clamped=timeout_clamped,
                thinking=thinking,
                send_thinking=send_thinking,
            )
            diag["attempts"] = attempt
            try:
                response = http.post(
                    f"{cfg['base_url']}/chat/completions", headers=headers, json=body
                )
                response.raise_for_status()
            except httpx.TimeoutException:
                _log_unusable("timeout", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="timeout",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                diag["http_status"] = status
                if status == 400 and send_thinking and attempt < MAX_MODEL_CALLS:
                    # 该模型不接受 enable_thinking 时，去掉字段再试一次，不换模型。
                    send_thinking = False
                    diag["thinking_param_rejected"] = True
                    _log_unusable("http_error", diag, model=cfg["model"])
                    continue
                _log_unusable("http_error", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="http_error",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if attempt >= MAX_MODEL_CALLS or status not in _RETRYABLE_HTTP:
                    best = result
                    break
                continue
            except (httpx.NetworkError, httpx.ProtocolError, httpx.ProxyError) as exc:
                # 对端在完整响应前断开。再请求一次；仍失败就写明连接中断，不改用代码写结论。
                reason, cause_type = _disconnect_detail(exc, cfg["api_key"])
                diag["exception_type"] = type(exc).__name__
                diag["disconnect_reason"] = reason
                if cause_type:
                    diag["cause_type"] = cause_type
                _log_unusable("connection_error", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="connection_error",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            except Exception as exc:  # noqa: BLE001 — 模型失败时不改用代码判断好坏
                diag["exception_type"] = type(exc).__name__
                _log_unusable(type(exc).__name__, diag, model=cfg["model"])
                return _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error=type(exc).__name__,
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
            try:
                raw = response.json()
            except ValueError:
                diag["http_status"] = response.status_code
                diag["content_chars"] = 0
                diag["reasoning_chars"] = 0
                _log_unusable("invalid_json", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="invalid_json",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            text, parsed_diag = interpret_chat_completion(raw)
            diag.update(parsed_diag)
            diag["http_status"] = response.status_code
            parsed = _extract_json(text) if text.strip() else None
            normalized = (
                normalize_judgment(parsed, payload) if parsed is not None else None
            )
            kind, missing = _unusable_kind(text, parsed, normalized, diag, payload)
            if missing:
                diag["missing_sections"] = missing
            gaps = conclusion_gaps(normalized, payload)
            if gaps:
                # 正文结构完整但结论漏了天气、土壤或出苗方位时，用同一次重试补上。
                diag["conclusion_gaps"] = gaps
                if attempt < MAX_MODEL_CALLS:
                    repair = gaps
            if kind is None:
                if diag.get("finish_reason") == "length":
                    diag["truncated"] = True
                diag["recovered"] = attempt > 1
                result = {
                    "llm_configured": True,
                    "model": cfg["model"],
                    **(normalized or {}),
                    "error": None,
                    "diagnostics": diag,
                }
                if not gaps or attempt >= MAX_MODEL_CALLS:
                    return result
                score = _filled_score(result)
                if score > best_score:
                    best, best_score = result, score
                _log_unusable("conclusion_gaps", diag, model=cfg["model"])
                continue
            _log_unusable(kind, diag, model=cfg["model"])
            result = _apply_reader_note(
                empty_judgment(
                    configured=True,
                    error=kind,
                    model=cfg["model"],
                    diagnostics=diag,
                )
            )
            if normalized:
                for key, value in normalized.items():
                    result[key] = value
                result = _apply_reader_note(result)
            score = _filled_score(result)
            if score > best_score:
                best, best_score = result, score
            if attempt >= MAX_MODEL_CALLS:
                break
            if thinking is True and send_thinking:
                thinking = False
                continue
            if tokens < ceiling and (
                diag.get("finish_reason") == "length" or kind == "incomplete_fields"
            ):
                tokens = _next_budget(tokens, ceiling)
                continue
        chosen = best or empty_judgment(
            configured=True, error="empty_content", model=cfg["model"]
        )
        chosen.setdefault("diagnostics", {})["attempts"] = attempt
        return _apply_reader_note(chosen)
    finally:
        if own_client:
            http.close()


# ReportLab HexColor 需要 #RRGGBB。模型有时会丢掉 #，或写来路不明的底色。
# 这里只保留提示里的三套颜色，缺 # 就补上，非法属性丢掉，不要把整段标记原样送去排版。
_PALETTE_BACK = {
    "1F4D38": "D9F2E3",
    "9C2B1B": "F8D6D2",
    "8A5A12": "F8E8C8",
}
_HEX6 = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_FONT_ATTR = re.compile(
    r"""([A-Za-z]+)\s*=\s*(['\"])(.*?)\2""",
    re.DOTALL,
)


def _hex6(value: str | None) -> str | None:
    if not value:
        return None
    matched = _HEX6.fullmatch(str(value).strip())
    return matched.group(1).upper() if matched else None


def _safe_font_open(tag: str) -> str:
    """Rebuild a ReportLab-safe <font> tag from a model opening tag."""
    attrs = {name.lower(): value for name, _q, value in _FONT_ATTR.findall(tag)}
    color = _hex6(attrs.get("color"))
    back = _hex6(attrs.get("backcolor") or attrs.get("bgcolor"))
    face = (attrs.get("face") or "YaHei-Bold").strip() or "YaHei-Bold"
    if color not in _PALETTE_BACK:
        if "face" in attrs:
            return f'<font face="{face}">'
        return ""
    parts = [f'face="{face}"', f'color="#{color}"']
    allowed_back = _PALETTE_BACK[color]
    if back == allowed_back:
        parts.append(f'backColor="#{back}"')
    return "<font " + " ".join(parts) + ">"


_FONT_OPEN = re.compile(
    r"<\s*font\b[^>]*color\s*=\s*['\"]#?(1F4D38|9C2B1B|8A5A12)['\"][^>]*>",
    re.IGNORECASE,
)
_BOLD_FACE = re.compile(
    r"<\s*font\b(?![^>]*\bcolor\s*=)[^>]*face\s*=\s*['\"]YaHei-Bold['\"][^>]*>",
    re.IGNORECASE,
)
_FONT_CLOSE = re.compile(r"<\s*/\s*font\s*>", re.IGNORECASE)
_BOLD_OPEN = re.compile(r"<\s*b\s*>", re.IGNORECASE)
_BOLD_CLOSE = re.compile(r"<\s*/\s*b\s*>", re.IGNORECASE)


def _has_wording(narrative: dict[str, Any]) -> bool:
    return bool(
        narrative.get("summary")
        or narrative.get("changes")
        or narrative.get("conclusion")
        or narrative.get("field_notes")
        or narrative.get("metric_notes")
        or narrative.get("infield")
        or narrative.get("surrounding")
        or narrative.get("weather")
    )


def section_lines(narrative: dict[str, Any] | None, key: str) -> list[str]:
    """Model sentences for one report section. Empty when the model did not write them."""
    return [_present(item) for item in (narrative or {}).get(key) or []]


def infield_line(narrative: dict[str, Any] | None, metric: str) -> list[str]:
    text = ((narrative or {}).get("infield") or {}).get(metric)
    return [_present(text)] if text else []


def judgment_paragraphs(narrative: dict[str, Any] | None) -> list[str]:
    """Render the sentences the model wrote, including the emphasis it chose."""
    if not narrative:
        return []
    items: list[str] = []
    summary = str(narrative.get("summary") or "").strip()
    if summary:
        items.append(_present(summary))
    for row in narrative.get("changes") or []:
        sentence = str(row.get("sentence") or "").strip()
        if sentence:
            items.append(_present(sentence))
    return items


def _present(value: str) -> str:
    """Keep bold and the three palette colors the model applied. Do not choose the words."""
    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"\u0000{len(slots) - 1}\u0000"

    text = _FONT_OPEN.sub(lambda match: stash(_safe_font_open(match.group(0))), value)
    text = _BOLD_FACE.sub(lambda match: stash(match.group(0)), text)
    text = _FONT_CLOSE.sub(lambda _match: stash("</font>"), text)
    text = _BOLD_OPEN.sub(lambda _match: stash("<b>"), text)
    text = _BOLD_CLOSE.sub(lambda _match: stash("</b>"), text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\u0000(\d+)\u0000", lambda match: slots[int(match.group(1))], text)
