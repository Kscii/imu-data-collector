"""Portable temporal-label contract; vendored byte-for-byte by its consumers.

Authority: imu-motion-simulator/contracts/temporal.py. No platform or ML imports.
Times are integer nanoseconds relative to the selected motion's first IMU sample.
The document is a proposal, never evidence of human acceptance on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from itertools import pairwise

SCHEMA = "imu.temporal_annotation.v1"
SEMANTIC_SCHEMA = "imu.temporal_annotation.v2"
TAXONOMY_ID = "motion-temporal"
TAXONOMY_VERSION = "1.0.0"
PERIOD_NS = 40_000_000
PRIMARY = (
    "standing",
    "walking",
    "running",
    "sitting",
    "lying",
    "squatting",
    "sit_down",
    "stand_up",
    "lie_down",
    "get_up",
    "squatting_down",
    "stair_up",
    "stair_down",
    "bend_or_pick_up",
    "stumble_recovery",
    "jumping",
    "turning",
    "crawling",
    "kneeling",
    "forward_fall",
    "backward_fall",
    "left_lateral_fall",
    "right_lateral_fall",
    "fall_unspecified",
)
PRODUCTION_TAXONOMY_VERSION = "2.0.0"
PRODUCTION_PRIMARY = PRIMARY[:-5] + ("fall",)
FALLS = frozenset((*PRIMARY[-5:], "fall"))
PRODUCTION_RULES_VERSION = "2026-10-09.3"
CURRENT_SCHEMA = "imu.temporal_annotation.v3"
CURRENT_TAXONOMY_VERSION = "3.0.0"
CURRENT_RULES_VERSION = "2026-10-10.1"
CURRENT_PRIMARY = (
    "standing",
    "sitting",
    "lying",
    "squatting",
    "kneeling",
    "ground_support",
    "walking",
    "running",
    "stair_up",
    "stair_down",
    "posture_transition",
    "bending",
    "jumping",
    "stumble_recovery",
    "fall",
    "other_activity",
)
SUPPORT_POSTURES = (
    "standing",
    "sitting",
    "lying",
    "squatting",
    "kneeling",
    "ground_support",
)


def primary_codes(version=TAXONOMY_VERSION):
    if version == TAXONOMY_VERSION:
        return PRIMARY
    if version == PRODUCTION_TAXONOMY_VERSION:
        return PRODUCTION_PRIMARY
    if version == CURRENT_TAXONOMY_VERSION:
        return CURRENT_PRIMARY
    raise ValueError(f"Unsupported taxonomy version: {version}")


# Operational definitions are versioned separately from the unchanged 24-code
# wire taxonomy. Existing accepted documents retain their original interpretation.
ACTIVITY_RULES_VERSION = "2026-10-09.2"
IMU_TRAINING_PROFILE = "imu-primary-20-v1"
_ACTIVITY_RULES = {
    "standing": (
        "站立",
        "足部承重、没有连续踏步的站姿。",
        "单足站立、摆臂、重心调整和小动作。",
        "连续步行；明显下蹲、弯腰或姿态转换。",
        "站姿支撑形成时开始，主活动或支撑姿态改变时结束。",
        "像走路但保持不动的姿势，不因外形而标为行走。",
    ),
    "walking": (
        "行走",
        "连续交替踏步的步行活动。",
        "倒走、侧走、原地踏步、边走边转及同时挥臂。",
        "跑动、登阶、原地转向；单次站姿调整。",
        "步行序列开始到停止或另一主活动开始，不逐步切分。",
        "持械挥臂时若下肢持续步行，主活动仍为行走。",
    ),
    "running": (
        "跑步",
        "连续跑动或慢跑步态。",
        "不同速度的跑动和慢跑。",
        "仅速度较快的步行；单次跳跃。",
        "跑动步态开始到停止或步态改变；结合连续周期判断。",
        "速度和单帧腾空都不能单独决定走或跑。",
    ),
    "sitting": (
        "坐姿",
        "臀部在椅、地面等支撑物上承重的坐姿。",
        "椅坐、地坐、盘腿、翘腿以及坐姿中的上肢小动作。",
        "正在坐下、离座起立；躯干已进入卧姿。",
        "承重坐姿形成到离开该支撑状态。",
        "盘腿坐在地上仍是坐姿，不要求全身静止。",
    ),
    "lying": (
        "卧姿",
        "躯干由床、地面等表面支撑的卧姿。",
        "仰卧、侧卧、俯卧，以及卧姿内的小幅调整。",
        "手膝撑起的爬行；躺下过程；连续翻滚。",
        "卧姿支撑稳定形成到离开卧姿或另一独立活动开始。",
        "趴着可以归卧姿，手膝支撑爬动不能归卧姿。",
    ),
    "squatting": (
        "蹲姿",
        "髋膝屈曲、身体降低，主要由足部承重的低位姿态。",
        "不同深度的稳定蹲姿及其中小动作。",
        "膝部着地承重的跪姿；下降和起身过程。",
        "低位蹲姿形成到开始离开该姿态。",
        "单膝跪地不按外观相近标为蹲姿。",
    ),
    "sit_down": (
        "坐下",
        "从其他支撑姿态进入臀部承重坐姿的连续转换。",
        "坐到椅上或地面，过程中的必要屈膝和扶撑。",
        "已有坐姿内的小动作；没有完成标准坐下的复杂坐位转移。",
        "开始离开原支撑姿态到承重坐姿形成。",
        "椅子转地面坐姿未经过明确站立时，先保留为特殊转移待讨论。",
    ),
    "stand_up": (
        "起立（坐／蹲／跪姿→站立）",
        "从坐姿、蹲姿或直跪／半跪姿态站起，允许扶物或手部辅助。",
        "椅坐、地坐、盘腿坐、蹲姿及直跪／半跪起立；起立后直接行走。",
        "从卧姿或手膝／手足爬撑开始的起身；站姿弯腰回正；跳跃落地恢复。",
        "为这次起立开始持续前倾、移重或抬身，到足部站立支撑形成；"
        "按整次连续转换的起始姿态分类，中途短暂经过其他姿态不另拆。",
        "盘腿坐着用手撑地站起仍属起立；直跪／半跪时临时扶地不因此归为爬撑。",
    ),
    "lie_down": (
        "躺下",
        "主动进入躯干受支撑卧姿的连续转换。",
        "向后、侧向或向前趴卧；途中短暂手膝支撑。",
        "下地后保持手膝支撑进入爬行；失衡跌倒。",
        "开始向卧姿转换到卧姿支撑形成。",
        "由蹲姿向前趴卧可用躺下，但所有趴下动作不能一概归入。",
    ),
    "get_up": (
        "起身（卧姿／爬撑→站立）",
        "从卧姿或手膝／手足爬撑姿态恢复站立，过程中可以经过坐、跪、蹲姿。",
        "卧姿、爬行及静止爬行式支撑到站立；不要求实际爬动或必须借助手部。",
        "从坐姿、蹲姿、直跪／半跪开始的起立；卧到坐且停留；站姿弯腰回正；落地后已站立。",
        "开始持续离开卧姿或爬撑起身，到足部站立支撑形成；"
        "中途短暂坐、跪、蹲不另拆，形成独立停留后再分别分类。",
        "卧姿连续经过跪姿再站起仍属起身；手膝撑着不动再站起也属起身。",
    ),
    "squatting_down": (
        "下蹲",
        "主动降低身体进入主要由足部承重的蹲姿。",
        "连续屈髋屈膝下降至蹲姿。",
        "跪下、坐下、躺下；紧接着跳跃的蓄力。",
        "开始下降进入蹲姿到低位蹲姿形成。",
        "所有身体降低并不都属于下蹲。",
    ),
    "stair_up": (
        "上楼梯",
        "通过连续登阶向更高支撑面移动。",
        "连续上阶及同一登阶序列内的交替支撑。",
        "平地步行、坡道步行、起立或跳跃引起的身体升高。",
        "开始踏上首级台阶到末级登阶完成；平台步行另标。",
        "结合支撑脚高度与交替踏步，不能只看骨盆升高。",
    ),
    "stair_down": (
        "下楼梯",
        "通过连续下阶向更低支撑面移动。",
        "连续下阶及同一序列内的交替支撑。",
        "平地或坡道步行、下蹲、跌倒。",
        "开始首级下阶到末级下阶完成；平台步行另标。",
        "身体下降不等于下楼梯。",
    ),
    "bend_or_pick_up": (
        "弯腰／拾物",
        "明显躯干弯曲或拾物主导的一次动作。",
        "站姿弯腰、取物及紧接着的回正。",
        "普通摆臂伸手；坐卧内小动作；下蹲、跪地或起身主导的活动。",
        "明显弯腰取物开始到该动作完成；稳定中间姿态形成时再切。",
        "不能因为手接近地面就把整个跪地过程都标为拾物。",
    ),
    "stumble_recovery": (
        "失衡恢复",
        "发生可见失衡和补偿动作后恢复控制，没有完成跌倒。",
        "补偿跨步、踉跄后恢复平衡。",
        "普通加速或跨步；已发生跌倒撞击。",
        "失衡开始到恢复受控支撑和运动。",
        "受到推动后连续补偿步可能属于失衡恢复，不应全标普通行走。",
    ),
    "jumping": (
        "跳跃",
        "以蹬伸、腾空和落地为核心的跳跃动作。",
        "紧接着的蓄力和落地恢复；连续跳跃。",
        "独立的下蹲或站立；地面连续翻滚。",
        "直接服务于该次跳跃的蓄力开始，到落地恢复或下一主活动开始。",
        "落地后已站稳的范围归站立，不再标起立。",
    ),
    "turning": (
        "原地转向",
        "以改变身体朝向为主的原地转动或换步。",
        "原地转身、换步转身。",
        "只转头；保持步行的转弯。",
        "身体转向开始到目标朝向形成或开始独立步行。",
        "转身后继续走路时应在主活动变化处切分。",
    ),
    "crawling": (
        "爬行",
        "以手膝、手足等低位支撑进行连续移动。",
        "多种低位爬动方式以及同一爬动序列内的小调整。",
        "明显下地准备、静止跪撑、躯干受支撑的卧姿、起身。",
        "爬动序列开始到停止形成独立状态或开始起身。",
        "进入手膝支撑的准备过程不能全部提前标成爬行。",
    ),
    "kneeling": (
        "跪姿",
        "单膝或双膝与支撑面接触承重的低位姿态。",
        "单膝跪、双膝跪及手部辅助支撑。",
        "主要由足部承重的蹲姿；连续爬动、下降或起身转换。",
        "膝部承重姿态形成到离开该姿态或开始独立活动。",
        "跪下过程没有专用类别时保留具体语义并待讨论，不强塞下蹲。",
    ),
}
for _fall_code, _fall_name in (
    ("forward_fall", "向前跌倒"),
    ("backward_fall", "向后跌倒"),
    ("left_lateral_fall", "向左跌倒"),
    ("right_lateral_fall", "向右跌倒"),
    ("fall_unspecified", "跌倒（方向不明）"),
):
    _ACTIVITY_RULES[_fall_code] = (
        _fall_name,
        "从失去有效支撑、倒向地面或较低支撑面，经撞击到随后稳定的一次跌倒。",
        "同一跌倒内的失衡、倒地、撞击和紧接着的稳定过程。",
        "主动坐下或躺下；未倒地的失衡恢复；独立主动翻滚。",
        "失衡起点到撞击后稳定；撞击必须位于事件内部，后续起身另标。",
        "方向相对事件开始时身体朝向；证据不足用方向不明，不推断医学原因。",
    )


_ACTIVITY_RULES["fall"] = (
    "跌倒",
    *_ACTIVITY_RULES["fall_unspecified"][1:5],
    "新标注不区分方向；来源方向保留在历史证据中，不推断医学原因。",
)


def imu_training_code(code):
    """The fixed 24 -> 20 projection; unknown is masked, never non-fall."""
    if code is None:
        return None
    if code not in PRIMARY and code != "fall":
        raise ValueError(f"Unknown primary activity: {code}")
    return "fall" if code in FALLS else code


def activity_rules(version=TAXONOMY_VERSION):
    """One portable source for documentation, the catalog and training maps."""
    if version == CURRENT_TAXONOMY_VERSION:
        return current_activity_rules()
    codes = primary_codes(version)
    fields = ("name_zh", "definition", "includes", "excludes", "boundaries", "example")
    return {
        "id": "primary-activity-rules",
        "version": (
            ACTIVITY_RULES_VERSION if version == TAXONOMY_VERSION else PRODUCTION_RULES_VERSION
        ),
        "taxonomy_id": TAXONOMY_ID,
        "taxonomy_version": version,
        "hdf5_version": "3.3.0",
        "principles": [
            "每一时刻仅一个主活动；辅助标签不在本轮范围。",
            "姿态允许变体和小动作；明显转换独立，短暂中间姿态不机械细拆。",
            "根据连续动作和支撑关系判断，不用单帧外形或单一速度、高度阈值判类。",
            "起立／起身按整次连续转换开始前的姿态分类，不按是否用手分类；"
            "中途短暂经过其他姿态不另拆。",
            "时间为首个所选 IMU 样本起算的 [start, stop)，切点在 25 Hz 网格上。",
            "无统一最短时长；同类连续已确认区间合并，保留原备注范围和独立跌倒事件。",
            "未知、词表未覆盖或边界未定保留语义并屏蔽训练，不作为负样本或其他动作类。",
            "复杂坐位转移、跪下、下地准备、卧到坐等未覆盖转换暂不强行映射。",
            "跌倒优先于其过程中的姿态；无跌倒的失衡恢复优先于补偿行走；完整转换不拆瞬时姿态。",
            "来源提示和模型预测不是人工真值；既有真实标签和来源原标签保持可追溯。",
        ],
        "primary": [
            {
                "code": code,
                "is_fall": code in FALLS,
                **dict(zip(fields, _ACTIVITY_RULES[code], strict=True)),
            }
            for code in codes
        ],
        "training_profile": {
            "id": IMU_TRAINING_PROFILE,
            "version": "1.0.0",
            "codes": [c for c in PRIMARY if c not in FALLS] + ["fall"],
            "mapping": {c: imu_training_code(c) for c in codes},
            "real_source_mapping": {"syncope_fall": "fall"},
            "unknown_policy": "mask",
        },
        "rolling": {
            "status": "experimental",
            "formal_code": None,
            "definition": "地面连续翻滚；区分跳跃、卧姿小幅翻身和跌倒后滚动。",
            "promotion": {
                "positive_events": 20,
                "positive_groups": 5,
                "hard_negatives": 40,
                "precision": 0.90,
                "recall": 0.80,
                "median_boundary_error_s": 0.20,
            },
        },
    }


def activity_rules_evidence(version=TAXONOMY_VERSION):
    rules = activity_rules(version)
    return {
        "kind": "activity-rules",
        "version": rules["version"],
        "sha256": document_sha256(rules),
        "rules": rules,
    }


def render_activity_rules_markdown():
    rules = activity_rules(PRODUCTION_TAXONOMY_VERSION)
    lines = [
        "# 主活动统一分类与切分规范",
        "",
        f"规则版本：`{PRODUCTION_RULES_VERSION}`。新标注为 motion-temporal 2.0.0 / 20 类，"
        "兼容历史 24 类；默认 "
        f"`{IMU_TRAINING_PROFILE}` 为 20 类。HDF5 保持正式 3.3。",
        "",
        "本文件由 portable temporal contract 中的 activity_rules() 生成；"
        "修改规则源后重新生成，勿单独修改本文。",
        "",
        "## 统一原则",
        "",
    ]
    lines += [f"- {x}" for x in rules["principles"]]
    lines += ["", "## 标签定义", ""]
    for item in rules["primary"]:
        lines += [
            f"### {item['name_zh']} `{item['code']}`",
            "",
            f"{item['definition']}",
            "",
            f"- 包括：{item['includes']}",
            f"- 不包括：{item['excludes']}",
            f"- 切分：{item['boundaries']}",
            f"- 例子：{item['example']}",
            "",
        ]
    lines += [
        "## 训练映射与来源兼容",
        "",
        "历史 `forward_fall`、`backward_fall`、`left_lateral_fall`、`right_lateral_fall`、"
        "`fall_unspecified` 派生投影为 `fall`；其余 19 类保持原代码。"
        "未知范围屏蔽，不映射成非跌倒。",
        "真实数据保留原代码、名称、活动状态、版本及历史标注；公共数据保留原标签，"
        "只有明确语义等价时才映射。来源粒度不足时不得补猜方向或姿态转换。",
        "真实来源已有的 `syncope_fall` 仅在明确声明真实来源时映射为训练 `fall`，"
        "不加入合成主活动词表，也不从动捕推断晕厥原因。",
        "新生产使用 motion-temporal 2.0.0；历史 1.0.0 文档保留原代码与含义；"
        "新的修订在 source_evidence 中绑定完整规则及哈希，既有接受文档不回写。",
        "2026-10-09.2 起立包含坐、蹲、直跪／半跪到站立；起身限卧姿／爬撑到站立。"
        "旧版本起立／起身须重新核对，不能仅按代码直接改释；计算专项椅子起立指标仍须筛选椅坐起点。",
        "",
        "## 翻滚封版门槛",
        "",
        "至少 20 个翻滚事件、5 个独立来源组、40 个易混淆负例。"
        "代理参考在看模型预测前冻结，来源组与既有独立验证隔离。",
        "区间识别精确率 ≥90%、召回率 ≥80%，起止误差中位数 ≤0.20 秒；"
        "另报告胸部 IMU 分组可分性和混淆。缺少证据、样本不足或系统性混淆跌倒均不通过。",
        "小样本估算不能冒充人工独立准确率。未通过时翻滚保留语义、不参与正式训练；"
        "通过需发布新的规则和词表版本，不能静默修改已接受文档。",
        "",
        "## 参考",
        "",
        "- [UCI HAPT：基础活动与姿态转换](https://archive.ics.uci.edu/dataset/341/smartphone+based+recognition+of+human+activities+and+postural+transitions)",
        "- [Transition-aware human activity recognition](https://upcommons.upc.edu/entities/publication/4fe86892-c341-41c0-8593-24be87b5d4e3)",
        "",
    ]
    return "\n".join(lines)


# Ordered, frozen tie-break for independently supported, simultaneous auxiliaries.
AUXILIARY = (
    "backward_movement",
    "sideways_movement",
    "turning",
    "kicking",
    "punching",
    "throwing",
    "catching",
    "lifting_object",
    "carrying_object",
    "placing_object",
    "stretching",
    "reaching",
    "waving",
    "clapping",
    "pointing",
)
RESOLUTIONS = {"identified", "unrecognized", "uncovered", "uncertain_boundary"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
RUN_SCHEMA = "imu.temporal_run.v1"
PRIMARY_RUN_SCHEMA = "imu.temporal_run.v2"
SEMANTIC_RUN_SCHEMA = "imu.temporal_run.v3"
SOURCE_PREFIX = re.compile(r"^synthetic-motion/(?:prod|dev/[A-Za-z0-9][A-Za-z0-9._-]*)/v1$")


def canonical_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def document_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def taxonomy(version=TAXONOMY_VERSION):
    return {
        "id": TAXONOMY_ID,
        "version": version,
        "primary": [
            {"code": code, "name": code.replace("_", " "), "is_fall": code in FALLS}
            for code in primary_codes(version)
        ],
        "auxiliary": [
            {"code": code, "name": code.replace("_", " "), "priority": i}
            for i, code in enumerate(AUXILIARY)
        ],
    }


def run_prefix(run):
    return (
        f"synthetic-motion/dev/{run['run_id']}/v1"
        if run["phase"] == "pilot"
        else "synthetic-motion/prod/v1"
    )


def run_root(run):
    return f"{run_prefix(run)}/temporal/runs/{run['run_id']}"


def validate_run(run):
    _require(
        isinstance(run, dict)
        and set(run)
        == {
            "schema",
            "run_id",
            "phase",
            "source_prefix",
            "created_at_utc",
            "seed",
            "candidates",
            "full_from",
        }
        and run.get("schema") in (RUN_SCHEMA, PRIMARY_RUN_SCHEMA, SEMANTIC_RUN_SCHEMA),
        "Run fields/schema",
    )
    _require(isinstance(run["run_id"], str) and IDENTIFIER.fullmatch(run["run_id"]), "Run ID")
    _require(
        run["phase"] in ("pilot", "full")
        and isinstance(run["source_prefix"], str)
        and SOURCE_PREFIX.fullmatch(run["source_prefix"]),
        "Run scope",
    )
    _require(
        type(run["seed"]) is int and isinstance(run["created_at_utc"], str),
        "Run metadata",
    )
    if run["phase"] == "pilot":
        _require(run["full_from"] is None, "Pilot must not impersonate full-run approval")
    else:
        _require(
            isinstance(run["full_from"], dict)
            and set(run["full_from"]) == {"pilot_run_id", "approval_sha256"}
            and IDENTIFIER.fullmatch(run["full_from"]["pilot_run_id"])
            and SHA256.fullmatch(run["full_from"]["approval_sha256"]),
            "Full run needs pilot approval",
        )
    entries = run["candidates"]
    primary_only = run["schema"] in (PRIMARY_RUN_SCHEMA, SEMANTIC_RUN_SCHEMA)
    limit = 60 if run["schema"] == SEMANTIC_RUN_SCHEMA else 40 if primary_only else 30
    if primary_only:
        _require(run["phase"] == "pilot", "Primary learning runs are isolated pilots")
    _require(
        isinstance(entries, list) and entries and (run["phase"] == "full" or len(entries) <= limit),
        "Pilot candidate limit exceeded",
    )
    identities, reference_groups = set(), {}
    for entry in entries:
        _require(
            isinstance(entry, dict)
            and set(entry)
            == {
                "candidate",
                "clock",
                "source_dataset",
                "source_member",
                "group_key",
                "stratum",
                "reference_role",
            },
            "Run candidate fields",
        )
        identity, clock = entry["candidate"], entry["clock"]
        example = empty_document(
            **identity,
            sample_count=clock["sample_count"],
            source_start_ns=clock["source_start_ns"],
            source_rate_hz=clock["source_rate_hz"],
        )
        _require(example["clock"] == clock, "Run clock differs")
        key = (identity["candidate_id"], identity["version_id"])
        _require(key not in identities, "Duplicate candidate in run")
        identities.add(key)
        _require(
            all(
                isinstance(entry[k], str) and entry[k]
                for k in ("source_dataset", "source_member", "group_key", "stratum")
            ),
            "Source grouping",
        )
        role = entry["reference_role"]
        _require(
            role
            in (
                ("development", "validation")
                if primary_only
                else ("none", "adjustment", "validation")
            ),
            "Reference role",
        )
        if role != "none":
            group = entry["group_key"]
            _require(
                group not in reference_groups or reference_groups[group] == role,
                "Reference adjustment/validation source groups overlap",
            )
            reference_groups[group] = role
    canonical_bytes(run)
    return run


def empty_document(
    *,
    candidate_id,
    version_id,
    commit_sha256,
    sample_count,
    input_hashes,
    source_start_ns=0,
    source_rate_hz=25.0,
    reason="Awaiting temporal annotation",
    producer=None,
):
    """A fully covered, explicitly unresolved draft, not a negative recording."""
    document = {
        "schema": SCHEMA,
        "candidate": {
            "candidate_id": candidate_id,
            "version_id": version_id,
            "commit_sha256": commit_sha256,
            "input_hashes": input_hashes,
        },
        "clock": {
            "origin": "selection_first_imu_sample",
            "source_start_ns": source_start_ns,
            "source_rate_hz": source_rate_hz,
            "sample_count": sample_count,
            "duration_ns": sample_count * PERIOD_NS,
        },
        "taxonomy": taxonomy(),
        "auxiliary_mode": "temporal",
        "segments": [
            {
                "segment_id": "segment-0001",
                "start_ns": 0,
                "stop_ns": sample_count * PERIOD_NS,
                "primary_code": None,
                "auxiliary_code": None,
                "auxiliary_status": "unrecognized",
                "resolution": "unrecognized",
                "reason": reason,
                "fall_event_id": None,
            }
        ],
        "fall_events": [],
        "producer": producer or {"kind": "unresolved"},
        "source_evidence": [],
    }
    validate_document(document)
    return document


def validate_document(document, *, expected_candidate=None, expected_count=None):
    _require(
        isinstance(document, dict)
        and document.get("schema") in (SCHEMA, SEMANTIC_SCHEMA, CURRENT_SCHEMA),
        "Unsupported temporal annotation schema",
    )
    semantic = document["schema"] in (SEMANTIC_SCHEMA, CURRENT_SCHEMA)
    current = document["schema"] == CURRENT_SCHEMA
    _require(
        current == (document["taxonomy"]["version"] == CURRENT_TAXONOMY_VERSION),
        "Annotation schema and taxonomy version differ",
    )
    _require(
        set(document)
        == {
            "schema",
            "candidate",
            "clock",
            "taxonomy",
            "auxiliary_mode",
            "segments",
            "fall_events",
            "producer",
            "source_evidence",
        },
        "Unexpected temporal annotation fields",
    )
    candidate = document["candidate"]
    _require(
        isinstance(candidate, dict)
        and set(candidate) == {"candidate_id", "version_id", "commit_sha256", "input_hashes"},
        "Candidate fields",
    )
    _require(
        isinstance(candidate["candidate_id"], str)
        and IDENTIFIER.fullmatch(candidate["candidate_id"]),
        "Invalid candidate ID",
    )
    for field in ("version_id", "commit_sha256"):
        _require(
            isinstance(candidate[field], str) and SHA256.fullmatch(candidate[field]),
            "Invalid candidate hash",
        )
    hashes = candidate["input_hashes"]
    _require(
        isinstance(hashes, dict)
        and set(hashes) == {"motion", "sensors", "selection"}
        and all(isinstance(x, str) and SHA256.fullmatch(x) for x in hashes.values()),
        "Invalid input hashes",
    )
    if expected_candidate is not None:
        _require(candidate == expected_candidate, "Temporal annotation candidate differs")
    clock = document["clock"]
    _require(
        isinstance(clock, dict)
        and set(clock)
        == {
            "origin",
            "source_start_ns",
            "source_rate_hz",
            "sample_count",
            "duration_ns",
        },
        "Invalid temporal clock fields",
    )
    _require(
        clock["origin"] == "selection_first_imu_sample"
        and type(clock["source_start_ns"]) is int
        and clock["source_start_ns"] >= 0,
        "Invalid temporal time origin",
    )
    count = clock["sample_count"]
    _require(
        type(count) is int
        and count >= 2
        and type(clock["duration_ns"]) is int
        and clock["duration_ns"] == count * PERIOD_NS,
        "Temporal duration must match the 25 Hz sample extent",
    )
    _require(
        type(clock["source_rate_hz"]) in (float, int) and 0 < clock["source_rate_hz"] < 100_000,
        "Invalid source rate",
    )
    if expected_count is not None:
        _require(count == expected_count, "Temporal annotation sample count differs")
    expected_taxonomy = taxonomy(document["taxonomy"]["version"])
    _require(document["taxonomy"] == expected_taxonomy, "Unfrozen or unknown temporal taxonomy")
    codes = primary_codes(document["taxonomy"]["version"])
    _require(document["auxiliary_mode"] in ("temporal", "segment_summary"), "Auxiliary mode")
    _require(
        isinstance(document["producer"], dict)
        and document["producer"].get("kind")
        and isinstance(document["source_evidence"], list),
        "Missing annotation evidence",
    )
    segments, events = document["segments"], document["fall_events"]
    _require(
        isinstance(segments, list) and segments and isinstance(events, list),
        "Temporal segments/events must be lists",
    )
    cursor, ids, event_segments = 0, set(), {}
    for segment in segments:
        _require(
            isinstance(segment, dict)
            and set(segment)
            == {
                "segment_id",
                "start_ns",
                "stop_ns",
                "primary_code",
                "auxiliary_code",
                "auxiliary_status",
                "resolution",
                "reason",
                "fall_event_id",
            }
            | ({"semantic_text", "review_state", "pending_reasons", "notes"} if semantic else set())
            | ({"transition"} if current else set()),
            "Segment fields",
        )
        sid = segment["segment_id"]
        _require(
            isinstance(sid, str) and IDENTIFIER.fullmatch(sid) and sid not in ids,
            "Duplicate or invalid segment ID",
        )
        ids.add(sid)
        start, stop = segment["start_ns"], segment["stop_ns"]
        _require(
            type(start) is int
            and type(stop) is int
            and start == cursor
            and start < stop <= clock["duration_ns"],
            "Segment coverage gap/overlap",
        )
        cursor = stop
        if current:
            _require(start % PERIOD_NS == stop % PERIOD_NS == 0, "切点必须位于 25 Hz 时间网格")
            transition = segment["transition"]
            if segment["primary_code"] == "posture_transition":
                _require(
                    isinstance(transition, dict)
                    and set(transition) == {"from_posture", "to_posture"}
                    and all(v is None or v in SUPPORT_POSTURES for v in transition.values()),
                    "姿态转换需要有效的起止姿态，未知端点可留空",
                )
            else:
                _require(transition is None, "非转换区间不能携带转换端点")
        if semantic:
            _require(isinstance(segment["semantic_text"], str), "Interval semantic text")
            state, pending = segment["review_state"], segment["pending_reasons"]
            _require(state in ("pending", "confirmed", "excluded"), "Interval review state")
            _require(
                isinstance(pending, list)
                and len(pending) == len(set(pending))
                and set(pending) <= {"class", "boundary", "vocabulary"}
                and (bool(pending) if state == "pending" else not pending),
                "Pending interval needs a discussion reason",
            )
            _require(
                state != "confirmed" or segment["resolution"] == "identified",
                "Only identified intervals can be confirmed",
            )
            _require(
                state != "excluded" or segment["resolution"] != "identified",
                "Explicit exclusions need a reason and no formal class",
            )
            _require(isinstance(segment["notes"], list), "Interval notes")
            note_ids = set()
            for note in segment["notes"]:
                _require(
                    isinstance(note, dict)
                    and set(note) == {"note_id", "start_ns", "stop_ns", "text"},
                    "Interval note fields",
                )
                nid = note["note_id"]
                _require(
                    isinstance(nid, str) and IDENTIFIER.fullmatch(nid) and nid not in note_ids,
                    "Interval note identity",
                )
                note_ids.add(nid)
                _require(
                    type(note["start_ns"]) is int
                    and type(note["stop_ns"]) is int
                    and 0 <= note["start_ns"] < note["stop_ns"] <= clock["duration_ns"]
                    and isinstance(note["text"], str),
                    "Note original range/text",
                )
        resolution, code = segment["resolution"], segment["primary_code"]
        _require(
            resolution in (RESOLUTIONS | {"invalid_data"} if current else RESOLUTIONS),
            "Unknown resolution",
        )
        if resolution == "identified":
            _require(code in codes and segment["reason"] is None, "Unresolved primary code")
        else:
            _require(
                code is None and isinstance(segment["reason"], str) and segment["reason"].strip(),
                "Exclusion needs an explicit reason",
            )
        auxiliary, status = segment["auxiliary_code"], segment["auxiliary_status"]
        _require(status in ("identified", "none", "unrecognized"), "Auxiliary status")
        _require(
            (status == "identified" and auxiliary in AUXILIARY)
            or (status != "identified" and auxiliary is None),
            "Unresolved auxiliary code",
        )
        _require(
            resolution == "identified" or auxiliary is None,
            "Excluded segment cannot carry a formal auxiliary",
        )
        event_id = segment["fall_event_id"]
        if code in FALLS and (not semantic or segment["review_state"] == "confirmed"):
            _require(
                isinstance(event_id, str) and IDENTIFIER.fullmatch(event_id),
                "Fall segment needs an event ID",
            )
            event_segments.setdefault(event_id, []).append(segment)
        else:
            _require(event_id is None, "Only confirmed falls can reference a fall event")
    _require(cursor == clock["duration_ns"], "Incomplete temporal coverage")
    seen = set()
    for event in events:
        _require(
            isinstance(event, dict)
            and set(event) == {"event_id", "code", "start_ns", "impact_ns", "stop_ns", "evidence"},
            "Fall event fields",
        )
        eid = event["event_id"]
        _require(eid in event_segments and eid not in seen, "Orphan or duplicate fall event")
        seen.add(eid)
        parts = event_segments[eid]
        _require(
            event["code"] in FALLS and all(p["primary_code"] == event["code"] for p in parts),
            "Inconsistent fall event class",
        )
        _require(
            type(event["start_ns"]) is int
            and type(event["stop_ns"]) is int
            and event["start_ns"] == parts[0]["start_ns"]
            and event["stop_ns"] == parts[-1]["stop_ns"]
            and all(a["stop_ns"] == b["start_ns"] for a, b in pairwise(parts)),
            "Fall event must cover contiguous segments",
        )
        _require(
            (
                (current and event["impact_ns"] is None)
                or (
                    type(event["impact_ns"]) is int
                    and event["start_ns"] < event["impact_ns"] < event["stop_ns"]
                )
            )
            and isinstance(event["evidence"], str)
            and event["evidence"].strip(),
            "Fall needs an evidenced impact strictly inside the event",
        )
    _require(seen == set(event_segments), "Missing fall event/impact")
    canonical_bytes(document)  # Reject NaN and unserializable evidence.
    return document


def semantic_draft(document):
    """Convert legacy evidence into an unconfirmed draft; never import acceptance."""
    validate_document(document)
    result = deepcopy(document)
    if result["schema"] in (SEMANTIC_SCHEMA, CURRENT_SCHEMA):
        return result
    result["schema"] = SEMANTIC_SCHEMA
    if result["fall_events"]:
        result["source_evidence"].append(
            {"kind": "legacy-fall-proposals", "fall_events": result["fall_events"]}
        )
    result["fall_events"] = []
    for segment in result["segments"]:
        segment.update(
            semantic_text="",
            review_state="pending",
            pending_reasons=["class", "boundary"],
            notes=[],
            fall_event_id=None,
        )
    return validate_document(result)


def confirmed(segment, document):
    return segment["resolution"] == "identified" and (
        document["schema"] == SCHEMA or segment["review_state"] == "confirmed"
    )


def _append_note(notes, note):
    nid, copy = note["note_id"], 2
    while nid in notes:
        if all(notes[nid][k] == note[k] for k in ("start_ns", "stop_ns", "text")):
            return
        nid, copy = f"{note['note_id']}-{copy}", copy + 1
    notes[nid] = {**note, "note_id": nid}


def normalize_semantic(document):
    """Coalesce confirmed actions while retaining original note ranges and fall IDs."""
    validate_document(document)
    result = deepcopy(document)
    if result["schema"] != SEMANTIC_SCHEMA:
        return result
    merged = []
    for segment in result["segments"]:
        previous = merged[-1] if merged else None
        if (
            previous
            and confirmed(previous, result)
            and confirmed(segment, result)
            and all(
                previous[k] == segment[k]
                for k in (
                    "primary_code",
                    "auxiliary_code",
                    "auxiliary_status",
                    "fall_event_id",
                )
            )
        ):
            # Archive both semantic descriptions before removing the artificial boundary.
            notes = {n["note_id"]: n for n in previous["notes"]}
            for part in (previous, segment):
                if part["semantic_text"].strip():
                    note = {
                        "note_id": "semantic-" + part["segment_id"],
                        "start_ns": part["start_ns"],
                        "stop_ns": part["stop_ns"],
                        "text": "原语义：" + part["semantic_text"],
                    }
                    _append_note(notes, note)
                for note in part["notes"]:
                    _append_note(notes, note)
            previous["notes"] = list(notes.values())
            previous["semantic_text"] = ""
            previous["stop_ns"] = segment["stop_ns"]
        else:
            merged.append(segment)
    result["segments"] = merged
    return validate_document(result)


def confirmed_spans(document):
    """Single conservative projection shared by training, metrics and HDF5 export."""
    if document["schema"] == CURRENT_SCHEMA:
        return classification_spans(document)
    return [
        (start, stop, code)
        for _, kind, start, stop, code in project_annotations(document)
        if kind == "activity"
    ]


def _ceil_sample(ns):
    return (ns + PERIOD_NS - 1) // PERIOD_NS


def project_annotations(document, *, sequence_index=0):
    """Project to the unchanged HDF5 core; merge auxiliary splits, not fall events.

    Uncertain ranges round outwards, so no unrecognized time leaks into a training
    sample. Recognized activity boundaries/onset use ceil; impact uses half-up.
    Empty quantized segments disappear, but an unrepresentable fall is rejected.
    """
    validate_document(document)
    _require(
        not partial_fall_events(document),
        "已确认跌倒缺少撞击点：保留分类监督，暂不导出完整 HDF5 3.3 时序录制",
    )
    count = document["clock"]["sample_count"]
    events = {e["event_id"]: e for e in document["fall_events"]}
    ranges = []
    for segment in document["segments"]:
        excluded = not confirmed(segment, document)
        start = segment["start_ns"] // PERIOD_NS if excluded else _ceil_sample(segment["start_ns"])
        stop = min(count, _ceil_sample(segment["stop_ns"]))
        ranges.append(
            [
                start,
                stop,
                "exclude" if excluded else "activity",
                "other" if excluded else segment["primary_code"],
                None if excluded else segment["fall_event_id"],
            ]
        )
    # Conservatively expand exclusions to overlapping grid cells, trimming neighbours.
    excluded_ranges = [(a, b) for a, b, kind, _, _ in ranges if kind == "exclude"]
    grid = []
    for a, b, kind, code, eid in ranges:
        if kind == "activity":
            pieces = [(a, b)]
            for left, right in excluded_ranges:
                pieces = [
                    (x, y)
                    for start, stop in pieces
                    for x, y in ((start, min(stop, left)), (max(start, right), stop))
                    if x < y
                ]
            if eid and pieces != [(a, b)]:
                raise ValueError(
                    "Exclusion overlaps a fall after quantization; exclude the entire event"
                )
        else:
            pieces = [(a, b)]
        grid.extend([x, y, kind, code, eid] for x, y in pieces if x < y)
    grid.sort(key=lambda r: (r[0], r[1]))
    merged = []
    for row in grid:
        if merged and row[2:] == merged[-1][2:] and row[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], row[1])
        else:
            merged.append(row)
    cursor, rows, used = 0, [], set()
    for start, stop, kind, code, eid in merged:
        _require(start == cursor, "Quantized coverage gap/overlap")
        cursor = stop
        rows.append((sequence_index, kind, start, stop, code))
        if eid:
            event = events[eid]
            impact = (event["impact_ns"] + PERIOD_NS // 2) // PERIOD_NS
            _require(
                eid not in used and start < impact < stop,
                "Fall event cannot be represented on the 25 Hz grid",
            )
            used.add(eid)
            rows.extend(
                (
                    (sequence_index, "onset", start, start, code),
                    (sequence_index, "impact", impact, impact, code),
                )
            )
    _require(cursor == count and used == set(events), "Quantized event/coverage mismatch")
    order = {"activity": 0, "onset": 1, "impact": 2, "exclude": 3}
    return sorted(rows, key=lambda r: (r[0], r[2], order[r[1]], r[3], r[4]))


def formal_labels(document):
    validate_document(document)
    # Freeze the full finite vocabulary, including an all-excluded recording.
    return [
        {
            **row,
            "taxonomy_id": TAXONOMY_ID,
            "taxonomy_version": document["taxonomy"]["version"],
            "origin": "temporal-review",
            "verification": "human",
        }
        for row in document["taxonomy"]["primary"]
    ]


def validate_embedded(refs, annotations, *, sequence_index, sample_count):
    """Offline verification of proposal, acceptance binding, and core projection."""
    embedded = refs.get("temporal_annotation")
    _require(
        isinstance(embedded, dict) and set(embedded) == {"sha256", "document"},
        "Missing embedded temporal annotation",
    )
    document = embedded["document"]
    validate_document(document, expected_count=sample_count)
    _require(
        document_sha256(document) == embedded["sha256"],
        "Temporal annotation hash mismatch",
    )
    for role in ("motion", "sensors"):
        _require(
            document["candidate"]["input_hashes"][role] == refs[role]["sha256"],
            "Temporal input lineage mismatch",
        )
    review = refs.get("review", {})
    raw = review.get("document_json")
    _require(
        isinstance(raw, str) and hashlib.sha256(raw.encode()).hexdigest() == review.get("sha256"),
        "Missing or mismatched embedded human review",
    )
    accepted = json.loads(raw)
    if document["producer"].get("kind") == "inherited-motion-review" or any(
        e.get("kind") == "motion-acceptance" for e in document["source_evidence"]
    ):
        from .motion_annotations import validate_inheritance

        validate_inheritance(document, accepted.get("reviewer"))
    _require(
        accepted.get("decision") == "accepted"
        and accepted.get("reviewer")
        and accepted.get("automatic_qa_passed") is True
        and accepted.get("temporal_annotation_sha256") == embedded["sha256"],
        "Temporal annotation is not bound to human acceptance",
    )
    expected = project_annotations(document, sequence_index=sequence_index)
    actual = []

    def decode(value):
        return value.decode() if isinstance(value, bytes) else str(value)

    for row in annotations:
        actual.append(
            (
                int(row["sequence_index"]),
                decode(row["kind"]),
                int(row["start_sample"]),
                int(row["stop_sample"]),
                decode(row["code"]),
            )
        )
    _require(actual == expected, "Embedded temporal annotation differs from HDF5 core")


# Version 3 is opt-in. Historical taxonomies, rules and experimental APIs above
# retain their exact meaning; motion review uses this new derived vocabulary.
_CURRENT_DEFINITIONS = {
    "standing": ("站立", "足部支撑、没有持续步行；允许小动作和单次挪步。", "日常站立统计"),
    "sitting": ("坐姿", "臀部在椅子、地面等表面承重，包括盘腿坐。", "坐姿时长与坐站转换"),
    "lying": (
        "卧姿",
        "躯干由表面支撑，包括仰卧、侧卧、俯卧，不等同于睡眠。",
        "卧姿时长与跌倒后姿态",
    ),
    "squatting": ("蹲姿", "屈髋屈膝降低身体，主要由足部承重。", "保留真实低位姿态"),
    "kneeling": (
        "跪姿",
        "单膝或双膝形成主要支撑，包括跪坐；临时扶地不自动变成爬撑。",
        "区分蹲、跪及转换端点",
    ),
    "ground_support": (
        "爬撑／爬行",
        "四肢形成低位支撑、躯干离地，包括静止支撑和爬动。",
        "覆盖静止爬撑，区分卧姿",
    ),
    "walking": ("行走", "连续步行，包括倒走、侧走、原地踏步和换步转身。", "日常活动统计"),
    "running": ("跑步", "连续跑动步态；不能单凭移动速度判定。", "活动强度与跌倒难负例"),
    "stair_up": ("上楼梯", "连续向上登阶，不包括上坡或单纯身体升高。", "登阶活动与能力趋势"),
    "stair_down": ("下楼梯", "连续向下登阶，不包括下坡或单纯身体降低。", "登阶活动与跌倒区分"),
    "posture_transition": (
        "姿态转换",
        "连续改变主要支撑姿态或支撑位置；记录起止姿态，允许复合转换及未完成尝试。",
        "以一个类别覆盖姿态转换",
    ),
    "bending": (
        "弯腰",
        "站姿支撑下的明显躯干前屈、保持前屈及回正，包括站着俯身拾物；未形成坐、蹲、跪、卧或爬撑等其他主要支撑姿态。",
        "区分普通弯腰与下地、跌倒",
    ),
    "jumping": (
        "跳跃",
        "以蹬伸、腾空、落地为核心，包括直接相关的蓄力和落地恢复。",
        "高运动强度活动及跌倒对照",
    ),
    "stumble_recovery": (
        "失衡恢复",
        "出现失衡和补偿动作后恢复控制，未完成跌倒。",
        "与普通踏步及跌倒区分",
    ),
    "fall": (
        "跌倒",
        "失去有效支撑，非计划地落至地面或更低支撑面；不要求高冲击或长时间卧倒。",
        "核心跌倒事件",
    ),
    "other_activity": (
        "其他活动",
        "明确发生但不属于上述类别的活动，填写区间语义；不能替代判断不确定或数据无效。",
        "覆盖侧手翻等长尾活动",
    ),
}


def current_activity_rules():
    return {
        "id": "primary-activity-rules",
        "version": CURRENT_RULES_VERSION,
        "taxonomy_id": TAXONOMY_ID,
        "taxonomy_version": CURRENT_TAXONOMY_VERSION,
        "hdf5_version": "3.3.0",
        "support_postures": list(SUPPORT_POSTURES),
        "principles": [
            "每个时刻仅一个主活动；不确定与数据无效独立保留，不充当其他活动真值。",
            "转换区间不直接相邻；有真实中间姿态就保留，无可辨认中间姿态则合并，不虚构短姿态。",
            "不同支撑姿态的30个有向组合均允许；相同端点需说明转移、往返或未完成尝试。",
            "转换允许直接衔接行走、被跌倒中断或被录制边界截断；未知端点不猜测。",
            "转换端点自动取紧邻区间；行走、跑步、上下楼及弯腰映射站姿，其余非支撑活动及片段边界保留未知。",
            "跌倒优先于过程中的姿态，失衡恢复优先于补偿踏步。",
            "同类连续普通动作合并，独立跌倒不合并；保留区间语义及备注原时间锚点。",
            "行走间不超过2秒的明确站立暂停仅在活动汇总中合并，保留原细段及净步行时长。",
            "跌倒分类监督与撞击定位分开；撞击未知不阻止人工检查通过，但不导出完整3.3事件。",
        ],
        "primary": [
            dict(
                code=c,
                is_fall=c == "fall",
                name_zh=_CURRENT_DEFINITIONS[c][0],
                definition=_CURRENT_DEFINITIONS[c][1],
                includes="",
                excludes="",
                boundaries="以可观察的活动或支撑变化切分，不强制最短停留时长。",
                example="",
                rationale=_CURRENT_DEFINITIONS[c][2],
            )
            for c in CURRENT_PRIMARY
        ],
        "training_profile": {
            "id": "imu-primary-16-v1",
            "version": "1.0.0",
            "codes": list(CURRENT_PRIMARY),
            "mapping": {c: c for c in CURRENT_PRIMARY},
            "real_source_mapping": {"syncope_fall": "fall"},
        },
    }


def support_posture(code):
    if code in SUPPORT_POSTURES:
        return code
    if code in ("walking", "running", "stair_up", "stair_down", "bending"):
        return "standing"
    return None


def derive_transition_endpoints(document):
    """Recompute editable v3 endpoints without changing the source revision."""
    result = deepcopy(document)
    if result["taxonomy"]["version"] != CURRENT_TAXONOMY_VERSION:
        return result
    parts = result["segments"]
    for i, part in enumerate(parts):
        part["transition"] = (
            {
                "from_posture": support_posture(parts[i - 1]["primary_code"]) if i else None,
                "to_posture": support_posture(parts[i + 1]["primary_code"])
                if i + 1 < len(parts)
                else None,
            }
            if part["primary_code"] == "posture_transition"
            else None
        )
    return result


def editor_document(document):
    """Latest editable view; historical projections and exports stay frozen."""
    return derive_transition_endpoints(current_document(document))


def current_document(document):
    """Derive a v3 review view, never replace acceptance or invent new human acts."""
    if document["taxonomy"]["version"] == CURRENT_TAXONOMY_VERSION:
        return deepcopy(document)
    result = semantic_draft(document) if document["schema"] == SCHEMA else deepcopy(document)
    if result["schema"] == SEMANTIC_SCHEMA:
        result["schema"] = CURRENT_SCHEMA
    result["taxonomy"] = taxonomy(CURRENT_TAXONOMY_VERSION)
    changes = []
    targets = {
        "sit_down": "sitting",
        "stand_up": "standing",
        "get_up": "standing",
        "lie_down": "lying",
        "squatting_down": "squatting",
    }
    for part in result["segments"]:
        old = part["primary_code"]
        part["transition"] = None
        reason = None
        if old in targets:
            part["primary_code"] = "posture_transition"
            part["transition"] = {"from_posture": None, "to_posture": None}
            reason = "核对转换起止姿态；原标签：" + _ACTIVITY_RULES[old][0]
        elif old == "turning":
            part.update(
                primary_code=None,
                resolution="unrecognized",
                reason="原转向区间需按踏步情况选择行走或站立",
            )
            reason = part["reason"]
        elif old == "crawling":
            part["primary_code"] = "ground_support"
        elif old == "bend_or_pick_up":
            part["primary_code"] = "bending"
        elif old in FALLS:
            part["primary_code"] = "fall"
        text = " ".join(
            [part.get("semantic_text", ""), *(n["text"] for n in part.get("notes", []))]
        )
        if old == "squatting" and "跪" in text:
            reason = "原蹲姿标签与跪姿语义需要复核"
        if old == "jumping" and any(w in text for w in ("非典型", "旋转", "侧手翻")):
            reason = "核对是否为其他活动，不能仅因腾空归为普通跳跃"
        if old == "lie_down":
            reason = "核对主动转换、静止卧姿或跌倒；撞击点未知仍可确认跌倒"
        if reason:
            part.update(review_state="pending", pending_reasons=["class"])
            changes.append(
                {
                    "segment_id": part["segment_id"],
                    "start_ns": part["start_ns"],
                    "stop_ns": part["stop_ns"],
                    "old_code": old,
                    "new_code": part["primary_code"],
                    "reason": reason,
                }
            )
    for event in result["fall_events"]:
        event["code"] = "fall"
    result["source_evidence"].append(
        {
            "kind": "posture-taxonomy-migration",
            "source_sha256": document_sha256(document),
            "source_taxonomy_version": document["taxonomy"]["version"],
            "target_taxonomy_version": CURRENT_TAXONOMY_VERSION,
            "rules_version": CURRENT_RULES_VERSION,
            "review_required": changes,
        }
    )
    return derive_transition_endpoints(result)


class AnnotationIssues(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("标注存在需要修改的问题：" + "；".join(i["message"] for i in issues))


def submission_issues(document):
    """Semantic checks on a displayed v3 recording before acceptance/normalization."""
    validate_document(document)
    if document["schema"] != CURRENT_SCHEMA:
        return []
    parts, issues = document["segments"], []

    def add(code, message, *segments):
        issues.append(
            {"code": code, "message": message, "segment_ids": [s["segment_id"] for s in segments]}
        )

    for i, part in enumerate(parts):
        code = part["primary_code"]
        note = " ".join([part["semantic_text"], *(n["text"] for n in part["notes"])])
        if code == "other_activity" and not note.strip():
            add("other_semantics", f"第 {i + 1} 段其他活动需要填写区间语义。", part)
        if code == "posture_transition":
            t = part["transition"]
            if (
                t["from_posture"] is not None
                and t["from_posture"] == t["to_posture"]
                and not note.strip()
            ):
                add(
                    "return_transition",
                    f"第 {i + 1} 段起止姿态相同，请说明转移、往返或未完成情况。",
                    part,
                )
            for side, neighbor in (("from_posture", i - 1), ("to_posture", i + 1)):
                if not 0 <= neighbor < len(parts):
                    continue
                pose = support_posture(parts[neighbor]["primary_code"])
                if pose and t[side] and pose != t[side]:
                    add(
                        "transition_endpoint",
                        f"第 {i + 1} 段转换端点与相邻姿态矛盾，请纠正或保留未知端点。",
                        part,
                        parts[neighbor],
                    )
        if i:
            prior = parts[i - 1]
            previous = prior["primary_code"]
            if previous == code == "posture_transition":
                add(
                    "adjacent_transitions",
                    f"第 {i}、{i + 1} 段转换直接相邻：请合并，或根据真实中间姿态切分。",
                    prior,
                    part,
                )
            elif previous in SUPPORT_POSTURES and code in SUPPORT_POSTURES and previous != code:
                add(
                    "missing_transition",
                    f"第 {i}、{i + 1} 段支撑姿态直接变化，请补充真实转换或明确待判范围。",
                    prior,
                    part,
                )
    return issues


def partial_fall_events(document):
    return [e for e in document["fall_events"] if e["impact_ns"] is None]


def classification_spans(document):
    """Classification-only targets; no HDF5 impact requirement, no unknown negatives."""
    validate_document(document)
    return [
        (_ceil_sample(s["start_ns"]), _ceil_sample(s["stop_ns"]), s["primary_code"])
        for s in document["segments"]
        if confirmed(s, document)
    ]


def walking_bouts(document, maximum_pause_ns=2_000_000_000):
    """A derived summary only; never rewrite fine annotations or count paused time as walking."""
    parts, bouts = document["segments"], []
    i = 0
    while i < len(parts):
        first = parts[i]
        if first["primary_code"] != "walking" or first.get("review_state") != "confirmed":
            i += 1
            continue
        start, stop = first["start_ns"], first["stop_ns"]
        active, ids = stop - start, [first["segment_id"]]
        j = i + 1
        while j < len(parts):
            current = parts[j]
            if current["primary_code"] == "walking" and current.get("review_state") == "confirmed":
                active += current["stop_ns"] - current["start_ns"]
                stop = current["stop_ns"]
                ids.append(current["segment_id"])
                j += 1
            elif (
                j + 1 < len(parts)
                and current["primary_code"] == "standing"
                and current.get("review_state") == "confirmed"
                and current["stop_ns"] - current["start_ns"] <= maximum_pause_ns
                and parts[j + 1]["primary_code"] == "walking"
                and parts[j + 1].get("review_state") == "confirmed"
            ):
                ids.append(current["segment_id"])
                j += 1
            else:
                break
        bouts.append(
            {
                "start_ns": start,
                "stop_ns": stop,
                "walking_ns": active,
                "pause_ns": stop - start - active,
                "segment_ids": ids,
            }
        )
        i = j
    return bouts
    return document
