"""Bilingual presentation metadata; never transform source values or stored summaries."""

ENGLISH = {
    "心率": "Heart rate",
    "呼吸率": "Respiratory rate",
    "在床标志原值": "Bed occupancy flag",
    "在床状态": "Bed occupancy",
    "睡眠状态原值": "Sleep state code",
    "睡眠状态": "Sleep state",
    "呼吸暂停持续时间": "Apnea duration",
    "体动": "Movement",
    "体动状态": "Movement state",
    "Wi-Fi 信号": "Wi-Fi signal",
    "血氧": "Blood oxygen",
    "体温": "Body temperature",
    "环境温度": "Ambient temperature",
    "收缩压": "Systolic pressure",
    "舒张压": "Diastolic pressure",
    "电池电量": "Battery level",
    "总步数": "Total steps",
    "区间步数": "Interval steps",
    "热量原值": "Energy (raw)",
    "信号幅度": "Signal amplitude",
    "充电状态": "Charging state",
    "累计距离": "Total distance",
    "运动时长": "Activity duration",
    "累计热量": "Total energy",
    "设备状态": "Device status",
    "活动": "Activity",
    "常用": "Common metrics",
    "更多指标": "More metrics",
    "原值": "Raw value",
    "原值 ×0.1 kcal": "Raw value ×0.1 kcal",
    "状态编号": "State code",
    "次/分钟": "breaths/min",
    "秒": "s",
    "分钟": "min",
    "步": "steps",
    "未确认": "Unconfirmed",
    "原值 · 单位/时区/枚举未确认": "Raw value; units, timezone and codes unconfirmed",
    "在床有效标志": "Bed occupancy validity flag",
    "睡眠有效标志": "Sleep validity flag",
    "报告创建时间": "Report creation time",
    "监测开始时间": "Monitoring start time",
    "上床时间": "Bedtime",
    "离床时间": "Time out of bed",
    "入睡时间": "Sleep onset",
    "醒来时间": "Wake time",
    "在床时长": "Time in bed",
    "睡眠时长": "Sleep duration",
    "清醒时长": "Awake duration",
    "醒来次数": "Awakenings",
    "浅睡时长": "Light sleep duration",
    "深睡时长": "Deep sleep duration",
    "离床次数": "Times out of bed",
    "呼吸暂停次数": "Apnea episodes",
    "最长呼吸暂停时长": "Longest apnea duration",
    "平均呼吸暂停时长": "Average apnea duration",
    "体动次数": "Movement count",
    "体动指数": "Movement index",
    "入睡潜伏期": "Sleep latency",
    "睡眠效率": "Sleep efficiency",
    "最高心率": "Maximum heart rate",
    "最低心率": "Minimum heart rate",
    "平均心率": "Average heart rate",
    "最高呼吸率": "Maximum respiratory rate",
    "最低呼吸率": "Minimum respiratory rate",
    "平均呼吸率": "Average respiratory rate",
    "睡眠评分": "Sleep score",
    "呼吸评分": "Respiration score",
    "呼吸暂停起点": "Apnea start points",
    "呼吸暂停长度": "Apnea lengths",
    "离床区间": "Out-of-bed intervals",
    "心率序列": "Heart rate series",
    "呼吸率序列": "Respiratory rate series",
    "序列时间分段": "Series time segments",
    "睡眠阶段序列": "Sleep stage series",
    "体动序列": "Movement series",
    "在床原值；非负整数 % 2": "Bed occupancy (raw); non-negative integer % 2",
    "睡眠状态原值；非负整数 % 8": "Sleep state (raw); non-negative integer % 8",
    "热量原值；除以 10 得 kcal": "Energy (raw); divide by 10 for kcal",
    "距离原值；除以 100 得 km": "Distance (raw); divide by 100 for km",
    "充电状态；0 未充电 / 1 充电 / 2 完成": "Charging state; 0 not charging / 1 charging / 2 full",
    "有效标志为零或缺失": "Validity flags are zero or missing",
    "报告字段不完整": "Incomplete report fields",
    "报警上报": "Reported alarm",
    "上游原始代码；事件含义尚未确认": "Original upstream code; event meaning is unconfirmed",
}


def bilingual(value: dict, *fields: str) -> dict:
    """Append English siblings to explicit presentation fields, leaving originals intact."""
    return {
        **value,
        **{
            f"{field}_en": ENGLISH.get(value[field], value[field])
            for field in fields
            if field in value
        },
    }


def summary_presentation(summary: dict) -> dict:
    """Enrich legacy summaries at read time, with no projection version change or DB write."""
    result = bilingual(summary, "label", "reason", "note")
    if "fields" in summary:
        result["fields"] = []
        for field in summary["fields"]:
            translated = bilingual(field, "label", "unit")
            if field["label"] == field["key"]:
                translated["label_en"] = field["key"]
            result["fields"].append(translated)
    return result
