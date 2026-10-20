#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库入库内容自动校验脚本模板
================================
按「五要素 + 元数据」规范校验待入库的知识条目（YAML 格式），输出校验报告。

- ERROR : 阻断入库，必须修复
- WARN  : 不阻断，建议人工复审

用法:
    pip install pyyaml
    python kb_validator.py cases/                 # 批量校验目录下所有 .yaml/.yml
    python kb_validator.py case.yaml --strict     # 严格模式，WARN 也视为阻断

集成:
    可接入 Git pre-commit 钩子或 CI 流水线，退出码 0 = 通过，1 = 阻断。

依赖: Python 3.8+，PyYAML
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("[致命] 缺少 PyYAML 依赖，请先执行: pip install pyyaml")


# ------------------------- 可配置的校验规则 -------------------------

# 顶层必填字段
REQUIRED_FIELDS = [
    "id", "title", "domain", "tags",
    "background", "problem", "solution", "implementation", "verification",
    "lessons", "meta",
]

# 五要素（必填字段的子集，缺失任一项为阻断错误）
FIVE_ELEMENTS = ["background", "problem", "solution", "implementation", "verification"]

# 每个要素的最少字符数（低于该值视为描述不充分）
MIN_SECTION_LEN = 20

# ID 命名规范：CASE-年份-序号（可按团队规范调整）
ID_PATTERN = re.compile(r"^CASE-\d{4}-\d{3,}$")

# meta 子字段与允许的状态取值
META_REQUIRED = ["version", "date", "owner", "status"]
STATUS_ENUM = {"草稿", "生效", "已废弃"}

# 日期格式：YYYY-MM
DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 时效性阈值：超过该月数提示复审
STALE_MONTHS = 24

# 效果验证量化检测：验证部分应至少出现一个数字型指标
QUANT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|ms|s|倍|QPS|qps|万|千|个|条|次)?")

# 模糊表述关键词（出现即警告）
VAGUE_WORDS = ["大幅提升", "显著优化", "效果很好", "明显好转", "性能更好", "基本解决"]

# 敏感信息检测（命中为阻断错误）
SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"), "疑似明文凭据"),
    (re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"), "内网 IP 地址"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
]

# 标题查重相似度阈值（0~1）
SIMILARITY_THRESHOLD = 0.85


# ------------------------- 数据结构 -------------------------

@dataclass
class Issue:
    level: str    # ERROR / WARN
    file: str
    field: str
    message: str


# ------------------------- 单条目校验 -------------------------

def validate_entry(data: dict, path: Path) -> list[Issue]:
    issues: list[Issue] = []
    err = lambda f, m: issues.append(Issue("ERROR", str(path), f, m))
    warn = lambda f, m: issues.append(Issue("WARN", str(path), f, m))

    if not isinstance(data, dict):
        err("(根节点)", "文件内容不是有效的 YAML 映射结构")
        return issues

    # 1. 字段完整性：必填字段缺失或为空
    for f in REQUIRED_FIELDS:
        if f not in data or data[f] in (None, "", [], {}):
            kind = "五要素字段" if f in FIVE_ELEMENTS else "必填字段"
            err(f, f"{kind}缺失或为空")

    # 2. 要素内容充分性
    for f in FIVE_ELEMENTS:
        v = str(data.get(f) or "").strip()
        if v and len(v) < MIN_SECTION_LEN:
            warn(f, f"内容过短（{len(v)} 字符，建议不少于 {MIN_SECTION_LEN}），可能描述不充分")

    # 3. 效果验证必须量化
    verification = str(data.get("verification") or "").strip()
    if verification and not QUANT_PATTERN.search(verification):
        warn("verification", "未检测到量化指标，建议补充延迟/错误率/吞吐等前后对比数据")

    # 4. 模糊表述检测
    full_text = "\n".join(str(data.get(f) or "") for f in FIVE_ELEMENTS + ["lessons"])
    for w in VAGUE_WORDS:
        if w in full_text:
            warn("(全文)", f"检测到模糊表述「{w}」，请替换为量化描述")

    # 5. 敏感信息检测
    for pattern, desc in SENSITIVE_PATTERNS:
        if pattern.search(full_text):
            err("(全文)", f"检测到{desc}，入库前必须脱敏")

    # 6. ID 命名规范
    eid = str(data.get("id") or "").strip()
    if eid and not ID_PATTERN.match(eid):
        warn("id", f"ID 不符合命名规范 CASE-年份-序号: {eid}")

    # 7. tags 类型
    tags = data.get("tags")
    if tags not in (None, "", []) and not isinstance(tags, list):
        warn("tags", "tags 应为列表格式")

    # 8. meta 元数据校验
    meta = data.get("meta")
    if isinstance(meta, dict):
        for f in META_REQUIRED:
            if not meta.get(f):
                err(f"meta.{f}", "元数据缺失")
        date_v = str(meta.get("date") or "").strip()
        if date_v and not DATE_PATTERN.match(date_v):
            warn("meta.date", f"日期格式应为 YYYY-MM，当前值: {date_v}")
        status_v = str(meta.get("status") or "").strip()
        if status_v and status_v not in STATUS_ENUM:
            warn("meta.status", f"未知状态「{status_v}」，允许值: {'/'.join(sorted(STATUS_ENUM))}")
        # 时效性检查
        if DATE_PATTERN.match(date_v):
            try:
                entry_date = datetime.strptime(date_v, "%Y-%m")
                now = datetime.now()
                months = (now.year - entry_date.year) * 12 + now.month - entry_date.month
                if months > STALE_MONTHS:
                    warn("meta.date", f"内容已产出 {months} 个月，建议先复审时效性再入库")
            except ValueError:
                pass

    return issues


# ------------------------- 跨条目校验 -------------------------

def validate_cross(entries: list[tuple[Path, dict]]) -> list[Issue]:
    """ID 唯一性 + 标题相似度查重。"""
    issues: list[Issue] = []

    seen_ids: dict[str, str] = {}
    for path, data in entries:
        eid = str(data.get("id") or "").strip()
        if not eid:
            continue
        if eid in seen_ids:
            issues.append(Issue("ERROR", str(path), "id", f"ID 与 {seen_ids[eid]} 重复"))
        else:
            seen_ids[eid] = path.name

    titled = [(p, str(d.get("title") or "")) for p, d in entries if d.get("title")]
    for i in range(len(titled)):
        for j in range(i + 1, len(titled)):
            ratio = difflib.SequenceMatcher(None, titled[i][1], titled[j][1]).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                issues.append(Issue(
                    "WARN", str(titled[j][0]), "title",
                    f"与 {titled[i][0].name} 的标题相似度 {ratio:.0%}，疑似重复入库，请人工仲裁",
                ))

    return issues


# ------------------------- 主流程 -------------------------

def collect_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*") if p.suffix.lower() in {".yaml", ".yml"})


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库入库内容自动校验")
    parser.add_argument("target", type=Path, help="知识条目 YAML 文件，或包含条目的目录")
    parser.add_argument("--strict", action="store_true", help="严格模式：WARN 也视为阻断错误")
    args = parser.parse_args()

    if not args.target.exists():
        print(f"[致命] 路径不存在: {args.target}")
        return 1

    files = collect_files(args.target)
    if not files:
        print(f"[致命] 未在 {args.target} 找到 YAML 文件")
        return 1

    all_issues: list[Issue] = []
    entries: list[tuple[Path, dict]] = []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            all_issues.append(Issue("ERROR", str(f), "(解析)", f"YAML 语法错误: {e}"))
            continue
        if data is None:
            all_issues.append(Issue("ERROR", str(f), "(根节点)", "空文件"))
            continue
        entries.append((f, data))
        all_issues.extend(validate_entry(data, f))

    all_issues.extend(validate_cross(entries))

    errors = [i for i in all_issues if i.level == "ERROR"]
    warns = [i for i in all_issues if i.level == "WARN"]

    print(f"共扫描 {len(files)} 个文件，解析成功 {len(entries)} 个；"
          f"ERROR {len(errors)} 项，WARN {len(warns)} 项")
    print("-" * 60)
    for i in all_issues:
        print(f"[{i.level:5s}] {Path(i.file).name} :: {i.field} :: {i.message}")
    print("-" * 60)

    blocked = bool(errors) or (args.strict and bool(warns))
    print("结论: " + ("阻断入库，请修复全部 ERROR" + ("（严格模式含 WARN）" if args.strict else "")
                    if blocked else "允许入库（WARN 项建议人工复审）"))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
