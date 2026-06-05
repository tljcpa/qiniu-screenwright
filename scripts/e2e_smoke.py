# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# scripts/e2e_smoke.py —— 全管线整合冒烟（end-to-end smoke test）
#
# 目的：验证 ingest -> bible -> segment -> generate -> 组装 Screenplay ->
#       annotate(连贯性回填) -> compute_metrics/format_report -> export 这一整条
#       离线管线，用真实 LLM 串起来真能出活。这是“所有 Pass 接得上”的总闸。
#
# 关键约定：
#   - 真实 LLM：脚本里不硬编码任何密钥，密钥靠运行前 `source .secrets/shared.env`
#     进环境（DEEPSEEK_* 等），由 app.llm.client.get_llm() 自行从环境读取。
#   - import 路径：业务模块是 app.*，需要 backend 在 sys.path 上。
#     本脚本主动把 backend 目录插到 sys.path 最前，这样无论从哪个 cwd 启动都能 import。
#   - 溯源自洽：每个场景 source_ref.spans 的偏移相对“对应章 chapter.text”，
#     脚本会逐场校验 chapter.text[start:end] 非空（创新点②的根基不变式）。
#
# 末尾要么打印 "E2E PASS"，要么在某一步抛出带明确信息的 AssertionError/异常。
# ----------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import time

# --- sys.path 修正：把 backend 目录加进来，保证 `import app.*` 能成功 ---------
# 本文件路径：<repo>/scripts/e2e_smoke.py
# backend 目录：<repo>/backend
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    # 插到最前，优先于其他同名包，避免 import 到错误的 app。
    sys.path.insert(0, _BACKEND_DIR)

# --- 业务模块 import（必须在 sys.path 修正之后） -----------------------------
from app.pipeline.ingest import ingest
from app.pipeline.bible import build_bible
from app.pipeline.segment import segment
from app.pipeline.generate import generate
from app.pipeline.continuity import annotate
from app.pipeline.metrics import compute_metrics, format_report
from app.pipeline.export import to_yaml, to_fountain, to_pdf
from app.schema.models import (
    Screenplay,
    Meta,
    SourceMeta,
)


# ----------------------------------------------------------------------------
# 计时辅助
# ----------------------------------------------------------------------------

def _banner(step_no: int, title: str) -> None:
    """打印一个分步小标题，便于人工在长输出里定位每一步。"""
    print("\n" + "=" * 70)
    print("[STEP %d] %s" % (step_no, title))
    print("=" * 70)


class _Timer:
    """
    简单的步骤计时器：with _Timer("xxx") as t: ...，退出时打印耗时。

    用 with 而不是手动记 t0/t1，是为了即使中途抛异常也能在退出时打印已耗时间，
    方便定位“是哪一步、跑了多久之后挂的”。
    """

    def __init__(self, label: str):
        self.label = label
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        dt = time.time() - self.t0
        # 不吞异常（返回 None/False），只负责打印耗时。
        print(">>> %s 耗时 %.2fs" % (self.label, dt))
        return False


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main() -> int:
    t_all0 = time.time()

    # --- STEP 1：读样本 ------------------------------------------------------
    _banner(1, "读取中文网文样本")
    sample_path = os.path.join(_BACKEND_DIR, "samples", "中文网文样本_旧城咖啡.txt")
    with _Timer("读样本"):
        with open(sample_path, "r", encoding="utf-8") as f:
            text = f.read()
    assert len(text) > 0, "样本文件为空：%s" % sample_path
    print("样本路径：%s" % sample_path)
    print("样本字符数：%d" % len(text))

    # --- STEP 2：ingest ------------------------------------------------------
    _banner(2, "ingest 分章")
    with _Timer("ingest"):
        novel = ingest(text)
    assert novel.chapters, "ingest 没有切出任何章"
    print("书名：%s" % novel.title)
    print("章数：%d" % len(novel.chapters))
    for ch in novel.chapters:
        print("  - 第%d章 标题=%s 正文长度=%d" % (ch.index, ch.title, len(ch.text)))

    # --- STEP 3：build_bible（真实 LLM） ------------------------------------
    _banner(3, "build_bible 抽取人物/地点/时间线（真实 LLM）")
    with _Timer("build_bible"):
        bible = build_bible(novel)
    print("人物数：%d  地点数：%d  时间线节点数：%d"
          % (len(bible.characters), len(bible.locations), len(bible.timeline)))
    for c in bible.characters:
        print("  - char id=%s 名字=%s 别名=%s" % (c.id, c.name, c.aliases))
    assert bible.characters, "bible 没抽到任何人物，下游 generate 难以映射说话人 id"

    # --- STEP 4：segment（真实 LLM） ----------------------------------------
    _banner(4, "segment 场景切分（真实 LLM）")
    with _Timer("segment"):
        stubs = segment(novel, bible)
    print("场景骨架数：%d" % len(stubs))
    for st in stubs:
        print("  - %s chapter=%d summary=%s"
              % (st.id, st.chapter_index, st.summary[:30]))
    assert stubs, "segment 没有切出任何场景骨架（SceneStub）"

    # --- STEP 5：generate（真实 LLM，逐场） ---------------------------------
    _banner(5, "generate 逐场生成（真实 LLM，medium=short_drama）")
    with _Timer("generate"):
        scenes = generate(novel, bible, stubs, medium="short_drama")
    print("生成场景数：%d" % len(scenes))
    assert scenes, "generate 没有生成任何 Scene"

    # --- STEP 6：组装 Screenplay --------------------------------------------
    # pydantic 构造本身即校验：任何字段不合法都会在这里抛 ValidationError。
    _banner(6, "组装 Screenplay（pydantic 构造即校验）")
    with _Timer("组装 Screenplay"):
        chapter_indexes = [ch.index for ch in novel.chapters]
        sp = Screenplay(
            meta=Meta(
                title=novel.title,
                source=SourceMeta(chapters=chapter_indexes),
                target_medium="short_drama",
            ),
            story_bible=bible,
            scenes=scenes,
        )
    print("Screenplay 构造成功：meta.title=%s target_medium=%s chapters=%s"
          % (sp.meta.title, sp.meta.target_medium, sp.meta.source.chapters))

    # --- STEP 7：annotate 回填连贯性 ----------------------------------------
    _banner(7, "annotate 连贯性回填")
    with _Timer("annotate"):
        sp = annotate(sp)
    flag_total = 0
    for sc in sp.scenes:
        flag_total += len(sc.continuity_flags)
    print("回填连贯性 flag 总数：%d" % flag_total)

    # --- STEP 8：断言/校验 ---------------------------------------------------
    _banner(8, "断言/校验（往返、计数、外化、溯源自洽）")
    with _Timer("断言校验"):
        # 8.1 YAML 往返：to_yaml -> from_yaml 必须成功（产物严守自家 schema）。
        yaml_text = sp.to_yaml()
        sp_round = Screenplay.from_yaml(yaml_text)
        assert isinstance(sp_round, Screenplay), "from_yaml 没有返回 Screenplay"
        assert len(sp_round.scenes) == len(sp.scenes), \
            "YAML 往返后场景数不一致：%d -> %d" % (len(sp.scenes), len(sp_round.scenes))
        print("[OK] YAML 往返成功，场景数一致=%d" % len(sp.scenes))

        # 8.2 scene_count > 0。
        scene_count = len(sp.scenes)
        assert scene_count > 0, "scene_count 必须 > 0，实际=%d" % scene_count
        print("[OK] scene_count=%d > 0" % scene_count)

        # 8.3 traceability_coverage > 0（创新点②：至少有内容行带 source_ref）。
        metrics_for_check = compute_metrics(sp, novel)
        cov = metrics_for_check["traceability_coverage"]
        assert cov > 0, \
            "traceability_coverage 必须 > 0，实际=%s（说明没有任何元素溯源命中原文）" % cov
        print("[OK] traceability_coverage=%.3f > 0" % cov)

        # 8.4 至少 1 个带 adaptation 的外化元素（创新点③）。
        adaptation_count = 0
        for sc in sp.scenes:
            for el in sc.elements:
                # transition 没有 adaptation 字段，用 getattr 安全取。
                if getattr(el, "adaptation", None) is not None:
                    adaptation_count += 1
        assert adaptation_count >= 1, \
            "至少要有 1 个带 adaptation 的外化元素，实际=%d" % adaptation_count
        print("[OK] 带 adaptation 的外化元素数=%d >= 1" % adaptation_count)

        # 8.5 每个场景 source_ref.spans 在对应章 text 上自洽：chapter.text[start:end] 非空。
        # 构造 章号->章 的索引，便于按 source_ref.chapter 定位。
        chapter_by_index = {}
        for ch in novel.chapters:
            chapter_by_index[ch.index] = ch
        checked_spans = 0
        for sc in sp.scenes:
            ch_no = sc.source_ref.chapter
            assert ch_no in chapter_by_index, \
                "场景 %s 的 source_ref.chapter=%d 在 novel 里不存在" % (sc.id, ch_no)
            ch = chapter_by_index[ch_no]
            assert sc.source_ref.spans, \
                "场景 %s 的 source_ref.spans 为空" % sc.id
            for span in sc.source_ref.spans:
                fragment = ch.text[span.start:span.end]
                assert len(fragment) > 0, \
                    ("场景 %s 在第%d章的 span[%d:%d] 切片为空，溯源不自洽"
                     % (sc.id, ch_no, span.start, span.end))
                checked_spans += 1
        print("[OK] 场景级溯源自洽：校验了 %d 个 span，chapter.text[start:end] 均非空"
              % checked_spans)

    # --- STEP 9：metrics + 报告 ---------------------------------------------
    _banner(9, "compute_metrics + format_report")
    with _Timer("metrics"):
        metrics = compute_metrics(sp, novel)
        report = format_report(metrics)
    print(report)

    # --- STEP 10：导出 YAML / Fountain / PDF --------------------------------
    _banner(10, "导出 YAML / Fountain / PDF")
    out_dir = os.path.join(_REPO_ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)
    yaml_path = os.path.join(out_dir, "screenplay.yaml")
    fountain_path = os.path.join(out_dir, "screenplay.fountain")
    pdf_path = os.path.join(out_dir, "screenplay.pdf")
    with _Timer("导出"):
        # to_yaml/to_fountain 返回文本，自己落盘；to_pdf 直接写文件并返回 path。
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(to_yaml(sp))
        with open(fountain_path, "w", encoding="utf-8") as f:
            f.write(to_fountain(sp))
        to_pdf(sp, pdf_path)
    print("导出完成：")
    print("  YAML    -> %s (%d bytes)" % (yaml_path, os.path.getsize(yaml_path)))
    print("  Fountain-> %s (%d bytes)" % (fountain_path, os.path.getsize(fountain_path)))
    print("  PDF     -> %s (%d bytes)" % (pdf_path, os.path.getsize(pdf_path)))

    # --- STEP 11：打印第一场 YAML 片段供人工查看 ----------------------------
    _banner(11, "第一场 YAML 片段（人工查看）")
    first_scene = sp.scenes[0]
    # 复用 Screenplay.to_yaml 的同款序列化习惯（by_alias + exclude_none），
    # 直接把单场 dump 成 YAML，让 adaptation 的 from_ 正确输出为 "from"。
    import yaml as _yaml
    first_scene_data = first_scene.model_dump(by_alias=True, exclude_none=True)
    first_scene_yaml = _yaml.safe_dump(
        first_scene_data, allow_unicode=True, sort_keys=False
    )
    print(first_scene_yaml)

    # --- 收尾 ----------------------------------------------------------------
    total_dt = time.time() - t_all0
    print("\n" + "#" * 70)
    print("全流程总耗时 %.2fs" % total_dt)
    print("E2E PASS")
    print("#" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
