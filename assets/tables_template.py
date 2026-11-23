# -*- coding: utf-8 -*-
"""设定表模板：槽位顺序 = 参考图编号顺序（场景 → 人物 → 道具）。

复制到工作目录的 tables.py 后，按本项目实际内容重写。
只写「看得见」的东西：外貌写体态/脸型/服饰/武器，发质单独一条规则。
"""

STYLE_BASE = (
    "16:9，东方古风武侠写实CG，次世代3D渲染，HDR光追，32K超清质感："
    "皮肤毛孔、单根睫毛、唇纹与唇部湿润高光、织物经纬与针脚清晰可辨；"
    "浅景深，背景虚化成柔光斑。"
)
STYLE_TAIL = "全员发质真实柔软，不油亮、不结壳、无定型感，发丝滞后头部动作约零点二秒跟随。"
CANVAS_RULE = "画面无字幕、无文字、无水印。"

# key → (显示名, 文件名, 光照与色调, 环境音)
# 光照必须写死方向与色温，12 段共用同一场景，曝光跨段锁定。
SCENES = {
    "场景一": ("场景一·外景", "P01_场景一",
        "主光为画面左上方上午日光（约5400K），地面反暖光，背景压暗虚化，人物面部明暗清楚，曝光跨段锁定。",
        "风声、衣料摩擦、远处鸟啼"),
}

# key → (显示名, 外貌描述, 发质规则)
CHARS = {
    "主角": ("主角",
        "青年男性，身形挺拔，眉目沉静；素色劲装束皮护腕，腰悬长刀。",
        "黑长发束高马尾，发丝干燥蓬松，风里滞后半拍轻摆"),
}

# key → (显示名, 文件名, 描述)；没有就留空 {}
PROPS = {
}

# 哑角：可以出场，但绝不能有台词
MUTE = []

# 原文称呼 / 描述性说法 → 表内 key
SPEAKER_ALIAS = {
}

# 原文地点词 → 场景 key（同一地点多种叫法时必填，否则模型会自创场景）
LOCATION_HINTS = {
}

# 允许保留的英文（原剧拟声 / 台标）；默认白名单始终生效，这里只做追加
ALLOWED_EN = {"Ma", "Ha", "Ri", "Yu", "TVBS", "TVIS", "please", "yes",
              "STOP", "VIP", "NO", "KO", "OK"}

# key → (role_id, 中文名, 描述, language, age, gender, pitch, pace)
# age: young_adult / adult / middle_age / elder；gender: male / female / unknown
# pitch: very_low / low / medium_low / medium / medium_high；pace: slow / natural / fast
VOICES = {
    "主角": ("role01", "主角", "青年男性声音，清朗克制，语速自然，重音在后半句。",
             "Chinese", "young_adult", "male", "medium", "natural"),
}


def music_line(state: str) -> str:
    """全片固定情绪音乐层；state ∈ open/keep/tense/soft/close。"""
    return {
        "open": "低音弦乐长音打底，加入稀疏鼓点，音量与情绪克制，跨段持续不停。",
        "keep": "同一段弦乐长音延续，鼓点不变，未换曲、未起新段。",
        "tense": "同一段音乐加入低音鼓的短击，音量略升，旋律不换，情绪收紧。",
        "soft": "同一段音乐收进低音区，弦乐变薄，只留一条长音，情绪转缓。",
        "close": "同一段音乐收束为一次低音，余音收，不从头另起一首。",
    }[state]
