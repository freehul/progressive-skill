"""core 单元测试 — 验证抽取后行为与原插件逻辑等价。

覆盖：config 默认值、UsageTracker 衰减、demote 决策、budget 压缩。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generic.core import config as core_config
from generic.core.budget import apply_transforms, score_skill_lines
from generic.core.scorer import UsageTracker
from generic.core.selector import (
    compute_demote_set,
    infer_relevant_categories,
)


# --- config ------------------------------------------------------------

def test_config_defaults():
    core_config.init(None)
    assert core_config.get("decay_days") == 30.0
    assert core_config.get("promote_score") == 2.0
    assert "hermes" in core_config.get("always_relevant")


# --- scorer ------------------------------------------------------------

def test_tracker_record_and_decay(tmp_path):
    t = UsageTracker(tmp_path / "usage.json")
    t.record("skills/alpha")
    snap = t.snapshot()
    assert snap["skills/alpha"]["count"] == 1
    # 刚记录 → 无衰减，score = count
    assert t.decayed_score(1, time.time()) == 1.0
    # 很久以前 → 衰减趋近 0
    assert t.decayed_score(100, 0) < 1e-6


def test_tracker_save_load(tmp_path):
    f = tmp_path / "usage.json"
    t1 = UsageTracker(f)
    t1.record("skills/beta")
    t1.save()
    t2 = UsageTracker(f)
    t2.load()
    assert t2.snapshot()["skills/beta"]["count"] == 1


# --- selector ----------------------------------------------------------

def test_infer_relevant_toolset():
    rel = infer_relevant_categories(
        core_config.get, toolsets={"terminal"}, tools=None
    )
    assert "software-development" in rel
    assert "devops" in rel
    assert "hermes" in rel


def test_infer_relevant_tools():
    rel = infer_relevant_categories(core_config.get, tools={"web_search"})
    assert "research" in rel


def test_infer_relevant_empty_is_safety_net():
    assert infer_relevant_categories(core_config.get) == frozenset()


def test_compute_demote_set_basic():
    """无工具集信息时 → 只保留既有 compact；有工具集 → 非相关分类进 demote。"""
    all_cats = {"a", "b", "hermes", "software-development"}
    skill_map = {}
    usage = {}
    decay = lambda c, l: 0.0

    demote = compute_demote_set(
        core_config.get, all_cats, skill_map, usage, decay,
        available_toolsets=set(), available_tools=set(),
    )
    # 无工具集 → relevant 为空 → 返回空（安全网）
    assert demote == frozenset()

    demote2 = compute_demote_set(
        core_config.get, all_cats, skill_map, usage, decay,
        available_toolsets={"web"}, available_tools=set(),
    )
    # web → research/hermes 相关；a/b 被降级；hermes/software-development 永不降级
    assert "a" in demote2 and "b" in demote2
    assert "hermes" not in demote2
    assert "software-development" not in demote2


def test_compute_demote_usage_promotion():
    """高频使用的分类即使无工具集映射也被保留。"""
    all_cats = {"a", "b"}
    skill_map = {"skills/x": "b"}
    usage = {"skills/x": {"count": 50, "last_used": time.time()}}
    from generic.core.scorer import UsageTracker as UT
    t = UT()
    demote = compute_demote_set(
        core_config.get, all_cats, skill_map, usage, t.decayed_score,
        available_toolsets={"web"},
    )
    # "b" 被使用提升 → 不在 demote；"a" 在
    assert "b" not in demote
    assert "a" in demote


# --- budget ------------------------------------------------------------

def _decay_always(c, l):
    return float(c)


def test_budget_names_only_count_line():
    text = "  cat-a [names only]: s1, s2, s3\n  other\n"
    out = apply_transforms(core_config.get, {}, _decay_always, text, frozenset({"cat-a"}))
    assert "cat-a (3)" in out
    assert "[names only]" not in out


def test_budget_full_block_kept_when_fits():
    lines = ["    - skills/alpha: desc a", "    - skills/beta: desc b"]
    text = "\n".join(lines)
    out = apply_transforms(core_config.get, {}, _decay_always, text, frozenset({"x"}))
    assert "skills/alpha" in out and "skills/beta" in out


def test_budget_cap_keeps_scored_first():
    """预算不足时保留高使用频率的 skill 行。"""
    usage = {
        "skills/hot": {"count": 99, "last_used": time.time()},
        "skills/cold": {"count": 1, "last_used": time.time()},
    }
    big = "    - skills/hot: " + "x" * 3000
    small = "    - skills/cold: " + "y" * 10
    text = big + "\n" + small
    cfg = dict(core_config.DEFAULT_CONFIG)
    cfg["list_budget_chars"] = 40  # 只够小行
    core_config.init(None)
    out = apply_transforms(core_config.get, usage, lambda c, l: float(c), text, frozenset({"x"}))
    # hot(99) 优先，但 3000 字符超过 40 预算 → 装不下；cold 10 字符装得下
    assert "skills/cold" in out
