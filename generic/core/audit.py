"""Structural audit rules for skill libraries (agent-agnostic, stdlib-only).

Five checks per skill directory:
  R1 monolith     — SKILL.md over char budget without externalized dirs
                    (references/ assets/ templates/) => fail; with them => warn
  R2 frontmatter  — must exist; `name` required; `name` must match dir name;
                    missing `description` => warn
  R3 broken-link  — quoted/marked paths into references|assets|templates|scripts
                    must exist on disk (fenced code blocks are skipped)
  R4 layout       — SKILL.md expected at <root>/<category>/<skill>/SKILL.md;
                    other depths flagged (root-level => warn, deeper => fail)
  registry        — optional: known_names set; skills absent from it => warn

Output is deterministic (sorted traversal, no timestamps) so reports can be
hashed or diffed in CI/pre-push hooks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

EXTERNAL_DIRS = ("references", "assets", "templates", "scripts")
# Paths mentioned in quotes or markdown links pointing into external dirs.
LINK_RE = re.compile(
    r"['\"]((?:references|assets|templates|scripts)/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]+)['\"]"
    r"|\]\(((?:references|assets|templates|scripts)/[^)\s]+)\)"
)
FENCE_RE = re.compile(r"```.*?```", re.S)
FRONTMATTER_RE = re.compile(r"\A[\ufeff]?---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-subset parse: flat `key: value` scalars between --- markers."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("'\"")
        if k.strip() and v:
            meta[k.strip()] = v
    return meta, text[m.end():]


def _check_monolith(meta: dict, body: str, sdir: Path, max_chars: int) -> list:
    total = len(body) + sum(len(k) + len(v) + 3 for k, v in meta.items())
    if total <= max_chars:
        return []
    externalized = any((sdir / d).is_dir() for d in EXTERNAL_DIRS)
    sev = "warn" if externalized else "fail"
    hint = "已外置但仍超预算，建议继续拆分" if externalized \
        else f"单体超限且未外置（缺 {'/'.join(EXTERNAL_DIRS[:2])} 目录），按渐进披露拆为路由页+分册"
    return [{"rule": "monolith", "severity": sev,
             "message": f"SKILL.md {total} 字符 > 预算 {max_chars}：{hint}"}]


def _check_frontmatter(meta: dict, sdir: Path) -> list:
    issues = []
    if not meta:
        return [{"rule": "frontmatter", "severity": "fail",
                 "message": "缺少合法的 YAML frontmatter（--- 头）"}]
    name = meta.get("name")
    if not name:
        issues.append({"rule": "frontmatter", "severity": "fail",
                       "message": "缺少 name 字段"})
    elif name != sdir.name:
        issues.append({"rule": "frontmatter", "severity": "fail",
                       "message": f"name '{name}' 与目录名 '{sdir.name}' 不一致"})
    if not meta.get("description"):
        issues.append({"rule": "frontmatter", "severity": "warn",
                       "message": "缺少 description（影响索引触发与压缩决策）"})
    return issues


def _check_broken_links(body: str, sdir: Path) -> list:
    issues = []
    cleaned = FENCE_RE.sub("", body)
    for m in LINK_RE.finditer(cleaned):
        rel = (m.group(1) or m.group(2) or "").split("#", 1)[0]  # drop anchors
        if not rel:
            continue
        # Skip documentation placeholders like references/xxx.md, <name>.md, *.md
        base = rel.rsplit("/", 1)[-1]
        if ("xxx" in base or "*" in base or "{" in base or "<" in base
                or ".." in rel):
            continue
        if not (sdir / rel).is_file():
            issues.append({"rule": "broken-link", "severity": "fail",
                           "message": f"'{rel}' 被引用但文件不存在"})
    return issues


def _check_layout(rel_parts: tuple) -> list:
    # rel_parts includes SKILL.md: ('cat', 'skill', 'SKILL.md')
    # 1-3 directory levels are legitimate (flat hub: <skill>/SKILL.md,
    # categorized: <cat>/<skill>/..., nested category: <cat>/<sub>/<skill>/...).
    n_dirs = len(rel_parts) - 1
    if 1 <= n_dirs <= 3:
        return []
    return [{"rule": "layout", "severity": "fail",
             "message": f"SKILL.md 嵌套过深（{'/'.join(rel_parts)}）"}]


def parse_known_names(path) -> set | None:
    """Parse a registry file into a set of skill names (stdlib-only).

    Accepts: .json (list of dicts/names, or {"skills": {...}}),
    YAML with {"skills": {...}} or {"layers": {...}} (needs pyyaml,
    optional), or plain text / MANIFEST-style lists (`- name` items,
    `name` per line). Returns None when the file is missing/unreadable.
    """
    p = Path(path)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except Exception:
            return set()
        if isinstance(data, list):
            return {s.get("name") for s in data
                    if isinstance(s, dict) and s.get("name")} | \
                   {n for n in data if isinstance(n, str)}
        if isinstance(data, dict):
            inner = data.get("skills")
            return set(inner) if isinstance(inner, dict) else set()
        return set()
    # Try real YAML first (optional dependency).
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            if isinstance(loaded.get("skills"), dict):
                return set(loaded["skills"])
            if isinstance(loaded.get("layers"), dict):
                names = set()
                stack = [loaded["layers"]]
                while stack:
                    node = stack.pop()
                    if isinstance(node, str):
                        if re.fullmatch(r"[a-z0-9][a-z0-9_\-]{1,60}", node):
                            names.add(node)  # plausible skill name only
                    elif isinstance(node, dict):
                        stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
                return names
    except ImportError:
        pass  # no pyyaml — fall through to the naive scan
    except Exception:
        pass
    # Naive scan: bare-name lines and "- name" list items.
    names = set()
    for m in re.finditer(r"^\s*-?\s*([A-Za-z0-9][A-Za-z0-9_.\-]{1,})\s*(?:#.*)?$",
                         text, re.M):
        tok = m.group(1)
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9]*[-_]?[a-z0-9]+)+|[a-z]{3,}", tok):
            names.add(tok)
    return names


def audit_skills_dir(root, max_chars: int = 20000,
                     known_names: set | None = None) -> dict:
    """Audit a skill library tree; returns a JSON-safe report dict."""
    root = Path(root)
    skills = []
    seen_dirs = set()

    for sm in sorted(root.rglob("SKILL.md")):
        rel_parts = sm.relative_to(root).parts
        if len(rel_parts) < 2:  # e.g. stray ./SKILL.md at root — not a library entry
            continue
        sdir = sm.parent
        try:
            text = sm.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skills.append({"name": sdir.name, "path": str(sm), "category": None,
                           "chars": 0,
                           "issues": [{"rule": "io", "severity": "fail",
                                       "message": f"无法读取: {e}"}]})
            continue

        meta, body = _parse_frontmatter(text)
        issues = []
        issues += _check_frontmatter(meta, sdir)
        issues += _check_monolith(meta, body, sdir, max_chars)
        issues += _check_broken_links(body, sdir)
        issues += _check_layout(rel_parts)

        if known_names is not None:
            probe = meta.get("name") or sdir.name
            if probe not in known_names:
                issues.append({"rule": "registry", "severity": "warn",
                               "message": f"'{probe}' 不在登记清单中"})

        skills.append({
            "name": meta.get("name") or sdir.name,
            "path": str(sm),
            "category": rel_parts[0] if len(rel_parts) >= 2 else None,
            "chars": len(text),
            "issues": issues,
        })
        seen_dirs.add(sdir)

    fail = sum(1 for s in skills for i in s["issues"] if i["severity"] == "fail")
    warn = sum(1 for s in skills for i in s["issues"] if i["severity"] == "warn")

    report = {
        "root": str(root),
        "skills": skills,
        "summary": {"total": len(skills), "fail": fail, "warn": warn},
        "ok": fail == 0,
    }
    if known_names is not None:
        disk = {s["name"] for s in skills}
        report["registry"] = {
            "missing_on_disk": sorted(n for n in known_names if n not in disk),
            "unregistered": sorted(n for n in disk if n not in known_names),
        }
    return report
