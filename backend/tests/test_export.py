# -*- coding: utf-8 -*-
"""
导出器测试。

覆盖三个导出入口：
- to_yaml   : 非空且能被 Screenplay.from_yaml 往返读回(round-trip)。
- to_fountain: 地点名/说话人名经 id 解析(不是裸 id)、slugline 格式、台词文本都在。
- to_pdf    : 写出文件且 size>0；中文字体缺失时也不抛异常(本机有字体则走中文路径)。
"""

import os

import pytest

from app.schema.models import (
    ActionElement,
    Character,
    DialogueElement,
    Heading,
    Location,
    Meta,
    Scene,
    Screenplay,
    SourceMeta,
    SourceRef,
    Span,
    StoryBible,
    TimePoint,
    TransitionElement,
)
from app.pipeline import export


@pytest.fixture()
def sample_screenplay() -> Screenplay:
    """
    构造一个最小但合法、含中英文、含三种 element、含 location/character 的剧本。

    设计点：
    - location id = "loc_office"，name = "总经理办公室"(中文)，验证 id->name 解析。
    - character id = "char_lin"，name = "林川"(中文)，验证说话人 id->name 解析。
    - elements 同时含 action / dialogue(带 parenthetical) / transition 三种。
    - 台词混入英文 "OK" 以验证中英混排不崩。
    """
    # 单一事实源：一个人物、一个地点、一个时间点。
    bible = StoryBible(
        characters=[Character(id="char_lin", name="林川")],
        locations=[Location(id="loc_office", name="总经理办公室")],
        timeline=[TimePoint(id="tp_1", label="第一天清晨", order=1)],
    )
    # 场级溯源(必填字段)。
    src = SourceRef(chapter=1, spans=[Span(start=0, end=10)])
    # 场景标题：内景 + 地点 id + 时间。
    heading = Heading(int_ext="INT", location_id="loc_office", time_of_day="日")
    # 三种元素，顺序承载叙事。
    elements = [
        ActionElement(type="action", text="林川推门而入，阳光洒满房间。"),
        DialogueElement(
            type="dialogue",
            character="char_lin",
            line="这件事就这么定了，OK？",
            parenthetical="冷笑",
        ),
        TransitionElement(type="transition", text="CUT TO"),
    ]
    scene = Scene(
        id="sc_001",
        heading=heading,
        source_ref=src,
        characters=["char_lin"],
        synopsis="开场",
        elements=elements,
    )
    meta = Meta(
        title="林川的抉择",
        source=SourceMeta(type="novel", chapters=[1]),
        target_medium="film",
    )
    return Screenplay(meta=meta, story_bible=bible, scenes=[scene])


def test_to_yaml_roundtrip(sample_screenplay):
    """to_yaml 输出非空，且能被 from_yaml 读回成等价对象(往返一致)。"""
    text = export.to_yaml(sample_screenplay)
    # 非空。
    assert text.strip() != ""
    # 往返：再解析回来。
    back = Screenplay.from_yaml(text)
    # 关键字段往返一致即认为 round-trip 成功。
    assert back.meta.title == sample_screenplay.meta.title
    assert back.story_bible.locations[0].name == "总经理办公室"
    assert back.scenes[0].elements[1].character == "char_lin"
    # 最稳的整体校验：两边再 dump 应完全相等。
    assert back.to_yaml() == text


def test_to_fountain_format(sample_screenplay):
    """to_fountain 含解析后的地点名/说话人名、slugline 格式正确、含台词文本。"""
    out = export.to_fountain(sample_screenplay)

    # slugline：INT. + 地点名(解析后) + 时间，整行大写。
    # 地点名 "总经理办公室" 是中文不受 upper 影响，前缀 "INT. " 必须出现。
    assert "INT. 总经理办公室 - 日" in out
    # 说话人显示的是解析后的 name "林川"，不是裸 id "char_lin"。
    assert "林川" in out
    assert "char_lin" not in out
    # 地点 id 也不应以裸 id 形式出现。
    assert "loc_office" not in out
    # parenthetical 用圆括号单独呈现。
    assert "(冷笑)" in out
    # 台词文本(含英文)在。
    assert "这件事就这么定了，OK？" in out
    # 动作行在。
    assert "林川推门而入，阳光洒满房间。" in out
    # 转场：大写 + 结尾冒号。
    assert "CUT TO:" in out
    # 标题页信息。
    assert "Title: 林川的抉择" in out


def test_to_fountain_missing_id_fallback():
    """location/character id 在 bible 里找不到时，降级为显示 id 本身，不抛异常。"""
    # 故意让 bible 为空，引用的 id 都查不到。
    bible = StoryBible(characters=[], locations=[], timeline=[])
    src = SourceRef(chapter=1, spans=[Span(start=0, end=5)])
    heading = Heading(int_ext="EXT", location_id="loc_unknown", time_of_day="夜")
    scene = Scene(
        id="sc_x",
        heading=heading,
        source_ref=src,
        characters=["char_ghost"],
        elements=[DialogueElement(type="dialogue", character="char_ghost", line="谁在那里？")],
    )
    sp = Screenplay(
        meta=Meta(title="测试", source=SourceMeta(type="novel", chapters=[1])),
        story_bible=bible,
        scenes=[scene],
    )
    out = export.to_fountain(sp)
    # 查不到时用裸 id 兜底。
    assert "loc_unknown".upper() in out
    assert "char_ghost".upper() in out


def test_to_pdf_writes_file(sample_screenplay, tmp_path):
    """to_pdf 写出文件且 size>0；即使中文字体缺失也不抛异常。"""
    out_path = os.path.join(str(tmp_path), "screenplay.pdf")
    returned = export.to_pdf(sample_screenplay, out_path)
    # 返回值就是传入的 path。
    assert returned == out_path
    # 文件存在且非空。
    assert os.path.isfile(out_path)
    assert os.path.getsize(out_path) > 0


def test_to_pdf_no_cjk_font_no_crash(sample_screenplay, tmp_path, monkeypatch):
    """模拟系统无 CJK 字体(探测返回空)，to_pdf 仍应正常写出文件不抛异常。"""
    # 把字体探测函数打桩成"找不到字体"，强制走降级路径。
    monkeypatch.setattr(export, "_find_cjk_font", lambda: "")
    out_path = os.path.join(str(tmp_path), "nofont.pdf")
    # 含中文的剧本在降级路径下也不应抛异常。
    export.to_pdf(sample_screenplay, out_path)
    assert os.path.isfile(out_path)
    assert os.path.getsize(out_path) > 0
