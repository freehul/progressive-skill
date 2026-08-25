"""audit 模块单元测试 — 技能库结构审计规则。

覆盖：R1 巨无霸单体、R2 frontmatter 合法性、R3 资源引用断链、
R4 目录布局、登记一致性（可选注入）。fixture 全部用 tmp_path 构造。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generic.core.audit import audit_skills_dir


GOOD_SKILL = """---
name: good-skill
category: demo
description: Use when demoing. A well-formed skill with external references.
version: 1.0.0
---

# Good Skill

Load 'references/conventions.md' before writing code.
Use 'assets/template.md' for output.
"""


def _make_skill(root: Path, cat: str, name: str, body: str,
                files: dict | None = None) -> Path:
    d = root / cat / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


@pytest.fixture
def good_tree(tmp_path):
    _make_skill(tmp_path, "demo", "good-skill", GOOD_SKILL, {
        "references/conventions.md": "# rules\n",
        "assets/template.md": "# tpl\n",
    })
    return tmp_path


# --- 整体行为 ------------------------------------------------------------

def test_all_good_passes(good_tree):
    report = audit_skills_dir(good_tree)
    assert report["summary"]["fail"] == 0
    assert report["ok"] is True
    assert len(report["skills"]) == 1
    assert report["skills"][0]["name"] == "good-skill"


def test_empty_dir_is_ok(tmp_path):
    report = audit_skills_dir(tmp_path)
    assert report["ok"] is True
    assert report["skills"] == []


# --- R1 巨无霸 -----------------------------------------------------------

def test_monolith_flagged(tmp_path):
    huge = "---\nname: big\ndescription: big one.\n---\n\n" + "x" * 5000
    _make_skill(tmp_path, "demo", "big", huge)
    report = audit_skills_dir(tmp_path, max_chars=1000)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "monolith"]
    assert issues and issues[0]["severity"] == "fail"
    assert report["ok"] is False


def test_big_but_externalized_only_warns(tmp_path):
    body = "---\nname: big2\ndescription: split me.\n---\n\n" + "x" * 5000
    _make_skill(tmp_path, "demo", "big2", body,
                {"references/part1.md": "# a\n"})
    report = audit_skills_dir(tmp_path, max_chars=1000)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "monolith"]
    assert issues and issues[0]["severity"] == "warn"
    assert report["ok"] is True


# --- R2 frontmatter ------------------------------------------------------

def test_missing_name_fails(tmp_path):
    bad = "---\ncategory: demo\ndescription: no name here.\n---\n\nbody\n"
    _make_skill(tmp_path, "demo", "noname", bad)
    report = audit_skills_dir(tmp_path)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "frontmatter"]
    assert issues and issues[0]["severity"] == "fail"


def test_name_mismatch_fails(tmp_path):
    bad = "---\nname: other-name\ndescription: mismatch.\n---\n\nbody\n"
    _make_skill(tmp_path, "demo", "mismatched", bad)
    report = audit_skills_dir(tmp_path)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "frontmatter"]
    assert any("name" in i["message"].lower() or "名称" in i["message"] for i in issues)


def test_no_frontmatter_fails(tmp_path):
    _make_skill(tmp_path, "demo", "raw", "just text, no yaml header\n")
    report = audit_skills_dir(tmp_path)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "frontmatter"]
    assert issues and issues[0]["severity"] == "fail"


# --- R3 断链 -------------------------------------------------------------

def test_broken_reference_link_fails(tmp_path):
    body = ("---\nname: linker\ndescription: refs.\n---\n\n"
            "See 'references/missing.md' for details.\n")
    _make_skill(tmp_path, "demo", "linker", body)
    report = audit_skills_dir(tmp_path)
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "broken-link"]
    assert issues and issues[0]["severity"] == "fail"


# --- R4 目录布局 ---------------------------------------------------------

def test_nested_skill_layout_fails(tmp_path):
    """SKILL.md 超过两级分类深度（root/a/b/c/d/SKILL.md）→ 布局违规。"""
    deep = tmp_path / "cat_a" / "outer" / "inner" / "deeper"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text(
        "---\nname: deep-one\ndescription: too deep.\n---\n\nx\n", encoding="utf-8")
    report = audit_skills_dir(tmp_path)
    issues = [i for s in report["skills"] for i in s["issues"]
              if i["rule"] == "layout"]
    assert issues and issues[0]["severity"] == "fail"


def test_two_level_category_is_legitimate(tmp_path):
    """Hermes 官方允许二级分类（cat/subcat/skill）→ 不报。"""
    d = tmp_path / "mlops" / "evaluation" / "some-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: some-skill\ndescription: fine.\n---\n\nx\n", encoding="utf-8")
    report = audit_skills_dir(tmp_path)
    assert [i for s in report["skills"] for i in s["issues"]
            if i["rule"] == "layout"] == []


def test_placeholder_links_not_flagged(tmp_path):
    """文档中的占位符引用（references/xxx.md）不算断链。"""
    body = ("---\nname: doccer\ndescription: docs.\n---\n\n"
            "Use skill_view('doccer', 'references/xxx.md') to load.\n"
            "Template at 'references/<your-file>.md'.\n")
    _make_skill(tmp_path, "demo", "doccer", body)
    report = audit_skills_dir(tmp_path)
    assert [i for s in report["skills"] for i in s["issues"]
            if i["rule"] == "broken-link"] == []


def test_orphan_skill_md_without_frontmatter_dir(tmp_path):
    """分类根下直接躺一个散文件不算技能；空目录不算技能，均不崩。"""
    (tmp_path / "weird").mkdir()
    (tmp_path / "weird" / "README.txt").write_text("hi", encoding="utf-8")
    report = audit_skills_dir(tmp_path)
    assert isinstance(report["ok"], bool)


# --- 登记一致性（注入式，可选） -------------------------------------------

def test_registry_drift_warns(good_tree):
    report = audit_skills_dir(good_tree, known_names={"some-other-skill"})
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "registry"]
    assert issues and issues[0]["severity"] == "warn"


def test_registry_match_no_issue(good_tree):
    report = audit_skills_dir(good_tree, known_names={"good-skill"})
    issues = [i for i in report["skills"][0]["issues"] if i["rule"] == "registry"]
    assert not issues


# --- 输出形状 ------------------------------------------------------------

def test_json_serializable(good_tree, tmp_path):
    import json
    report = audit_skills_dir(good_tree)
    json.dumps(report, ensure_ascii=False)  # 不抛异常即通过
