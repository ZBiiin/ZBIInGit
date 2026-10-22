#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库入库内容自动校验器
========================
按「案例型五要素」与「事实卡七字段」两套规范校验待入库条目（YAML），输出结构化报告。

- ERROR : 阻断入库，必须修复
- WARN  : 不阻断，建议人工复审；`--strict` 下同样阻断

用法:
    pip install pyyaml
    python kb_validator.py cases/
    python kb_validator.py domains/climbing/
    python kb_validator.py case.yaml --strict
    python kb_validator.py . --json > report.json

集成:
    Git pre-commit / CI：退出码 0 通过，1 阻断，2 参数或路径错误。

依赖: Python 3.9+，PyYAML
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("[致命] 缺少 PyYAML 依赖，请先执行: pip install pyyaml")


# ============================ 可配置规则区 ============================

# ---- 必填字段 ----
COMMON_REQUIRED = ["id", "title", "meta"]

CASE_REQUIRED = [
    "domain", "tags",
    "background", "problem", "solution", "implementation", "verification",
    "lessons",
]

FACT_REQUIRED = ["category", "issuer", "channel", "cost", "validity", "scope", "source"]

# 五要素（案例型必填字段的子集）
FIVE_ELEMENTS = ["background", "problem", "solution", "implementation", "verification"]

# ---- 内容充分性 ----
MIN_SECTION_LEN = 20

# ---- 命名与枚举 ----
ID_PATTERN = re.compile(r"^(CASE|FACT)-\d{4}-\d{3,}$")
META_REQUIRED = ["version", "date", "owner", "status"]
STATUS_ENUM = {"草稿", "生效", "已废弃"}
DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# ---- 时效性 ----
STALE_MONTHS = 24

# ---- 量化验证：数字 + 单位，或前后对比句式 ----
QUANT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|ms|毫秒|秒|分钟|min|h|小时|倍|QPS|qps|rps|req/s|"
    r"万|千|百|元|次|条|个|台|GB|MB|KB)"
)
COMPARE_PATTERN = re.compile(r"\d[^\n]{0,12}(?:→|->|降至|提升至|增长至|下降至|从\s*\d)")

# ---- 模糊表述 ----
VAGUE_WORDS = [
    "大幅提升", "显著优化", "效果很好", "明显好转", "性能更好", "基本解决",
    "一定程度", "有所改善",
]

# ---- 敏感信息 ----
PLACEHOLDER = re.compile(r"^(?:<.*>|\*{3,}|x{3,}|X{3,}|your[_-].*|example.*|\[.*\]|\$\{.*\})$", re.I)

SENSITIVE_PATTERNS = [
    (re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*"
        r"(?P<val>[^\s,;'\"）)]{6,})"
     ), "疑似明文凭据"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
    (re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"), "内网 IP 地址"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GitHub Token"),
]

# ---- 事实卡来源 ----
OFFICIAL_HINT = re.compile(r"官网|官方|通知|办法|条例|标准|指南|文件|公告|证书|http", re.I)

# ---- 查重 ----
SIMILARITY_THRESHOLD = 0.85

# ---- 文件筛选 ----
YAML_SUFFIXES = {".yaml", ".yml"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
TEMPLATE_HINT = re.compile(r"template|模板", re.I)


# ============================ 数据结构 ============================

@dataclass
class Issue:
    level: str      # ERROR / WARN
    file: str
    field: str
    message: str


# ============================ 工具函数 ============================

def _text(value) -> str:
    """把 YAML 字段（字符串 / 列表 / 字典）统一压成可检索文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_text(v)}" for k, v in value.items())
    return str(value)


def _months_since(yyyymm: str) -> int | None:
    try:
        d = datetime.strptime(yyyymm, "%Y-%m")
    except ValueError:
        return None
    now = datetime.now()
    return (now.year - d.year) * 12 + (now.month - d.month)


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value.strip()))


# ============================ 单条目校验 ============================

def validate_entry(data: dict, path: Path) -> list[Issue]:
    issues: list[Issue] = []

    def err(field: str, msg: str) -> None:
        issues.append(Issue("ERROR", str(path), field, msg))

    def warn(field: str, msg: str) -> None:
        issues.append(Issue("WARN", str(path), field, msg))

    if not isinstance(data, dict):
        err("(根节点)", "文件内容不是有效的 YAML 映射结构")
        return issues

    is_fact = str(data.get("type") or "").strip().lower() == "fact"
    required = COMMON_REQUIRED + (FACT_REQUIRED if is_fact else CASE_REQUIRED)

    # 1) 必填字段完整性
    for field in required:
        value = data.get(field)
        if value in (None, "", [], {}):
            if field in FACT_REQUIRED:
                kind = "事实卡字段"
            elif field in FIVE_ELEMENTS:
                kind = "五要素字段"
            else:
                kind = "必填字段"
            err(field, f"{kind}缺失或为空")

    # 2) 内容充分性（仅案例型；事实卡字段是原子事实）
    if not is_fact:
        for field in FIVE_ELEMENTS:
            text = _text(data.get(field)).strip()
            if text and len(text) < MIN_SECTION_LEN:
                warn(field, f"内容过短（{len(text)} 字符，建议 ≥ {MIN_SECTION_LEN}），可能描述不充分")

        verification = _text(data.get("verification")).strip()
        if verification and not (QUANT_PATTERN.search(verification) or COMPARE_PATTERN.search(verification)):
            warn("verification", "未检测到量化指标（数字+单位或前后对比），建议补充延迟/错误率/吞吐等数据")

    # 3) 全文模糊表述
    fields_for_scan = FACT_REQUIRED if is_fact else FIVE_ELEMENTS + ["lessons"]
    full_text = "\n".join(_text(data.get(f)) for f in fields_for_scan)
    for word in VAGUE_WORDS:
        if word in full_text:
            warn("(全文)", f"检测到模糊表述「{word}」，请替换为可核查的量化描述")

    # 4) 敏感信息（扫描整份文档，含未知自定义字段）
    sensitive_text = _text(data)
    for pattern, desc in SENSITIVE_PATTERNS:
        for match in pattern.finditer(sensitive_text):
            captured = match.groupdict().get("val") if "val" in match.groupdict() else None
            if captured and _is_placeholder(captured):
                continue
            err("(全文)", f"检测到{desc}，入库前必须脱敏")
            break

    # 5) ID 规范与类型一致性
    entry_id = str(data.get("id") or "").strip()
    if entry_id:
        if not ID_PATTERN.match(entry_id):
            warn("id", f"ID 不符合规范 (CASE|FACT)-年份-序号: {entry_id}")
        elif is_fact and not entry_id.startswith("FACT-"):
            warn("id", f"事实卡 ID 应以 FACT- 开头，当前为 {entry_id}")
        elif not is_fact and not entry_id.startswith("CASE-"):
            warn("id", f"案例条目 ID 应以 CASE- 开头，当前为 {entry_id}")

    # 6) tags 结构（案例型必查）
    tags = data.get("tags")
    if tags not in (None, "", []) and not isinstance(tags, list):
        warn("tags", "tags 应为列表格式，例如 [证书, 攀岩]")
    elif isinstance(tags, list) and any(not isinstance(t, str) for t in tags):
        warn("tags", "tags 列表内应全部为字符串")

    # 7) meta 元数据
    meta = data.get("meta")
    if not isinstance(meta, dict):
        err("meta", "meta 必须是映射结构（version/date/owner/status）")
    else:
        for field in META_REQUIRED:
            if not meta.get(field):
                err(f"meta.{field}", "元数据缺失")
        date_value = str(meta.get("date") or "").strip()
        if date_value and not DATE_PATTERN.match(date_value):
            warn("meta.date", f"日期格式应为 YYYY-MM，当前值: {date_value}")
        status_value = str(meta.get("status") or "").strip()
        if status_value and status_value not in STATUS_ENUM:
            warn("meta.status", f"未知状态「{status_value}」，允许值: {'/'.join(sorted(STATUS_ENUM))}")
        months = _months_since(date_value)
        if months is not None and months > STALE_MONTHS:
            warn("meta.date", f"内容已产出 {months} 个月（阈值 {STALE_MONTHS}），建议先复审时效性")

    # 8) 事实卡来源可追溯性
    if is_fact:
        source = _text(data.get("source")).strip()
        if source and not OFFICIAL_HINT.search(source):
            warn("source", "来源未包含官网/通知/文件/URL 等可追溯线索，请补充出处")

    return issues


# ============================ 跨条目校验 ============================

def validate_cross(entries: list[tuple[Path, dict]]) -> list[Issue]:
    issues: list[Issue] = []

    # ID 唯一性
    seen: dict[str, str] = {}
    for path, data in entries:
        entry_id = str(data.get("id") or "").strip()
        if not entry_id:
            continue
        if entry_id in seen:
            issues.append(Issue("ERROR", str(path), "id", f"ID 与 {seen[entry_id]} 重复"))
        else:
            seen[entry_id] = path.name

    # 标题相似度查重
    titled = [(p, str(d.get("title") or "").strip()) for p, d in entries if d.get("title")]
    for i in range(len(titled)):
        for j in range(i + 1, len(titled)):
            left_path, left_title = titled[i]
            right_path, right_title = titled[j]
            ratio = difflib.SequenceMatcher(None, left_title, right_title).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                issues.append(Issue(
                    "WARN", str(right_path), "title",
                    f"与 {left_path.name} 的标题相似度 {ratio:.0%}，疑似重复入库，请人工仲裁",
                ))

    return issues


# ============================ 文件收集 ============================

def collect_files(target: Path, include_templates: bool = False) -> list[Path]:
    if target.is_file():
        return [target]

    files: list[Path] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in YAML_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not include_templates and TEMPLATE_HINT.search(path.name):
            continue
        files.append(path)
    return files


# ============================ 主流程 ============================

def main() -> int:
    parser = argparse.ArgumentParser(description="知识库入库内容自动校验（案例型 + 事实卡型）")
    parser.add_argument("targets", type=Path, nargs="+", metavar="TARGET",
                        help="知识条目 YAML 文件，或包含条目的目录；可一次传多个")
    parser.add_argument("--strict", action="store_true", help="严格模式：WARN 也视为阻断")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告（便于 CI 消费）")
    parser.add_argument("--include-templates", action="store_true",
                        help="同时校验文件名含 template/模板 的文件（默认跳过）")
    args = parser.parse_args()

    missing = [str(t) for t in args.targets if not t.exists()]
    if missing:
        print(f"[致命] 路径不存在: {', '.join(missing)}", file=sys.stderr)
        return 2

    files: list[Path] = []
    seen: set[Path] = set()
    for target in args.targets:
        for path in collect_files(target, include_templates=args.include_templates):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)

    if not files:
        joined = ", ".join(str(t) for t in args.targets)
        print(f"[致命] 未在 {joined} 找到 YAML 条目文件", file=sys.stderr)
        return 2

    issues: list[Issue] = []
    entries: list[tuple[Path, dict]] = []

    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(Issue("ERROR", str(path), "(编码)", "文件不是 UTF-8 编码"))
            continue
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            issues.append(Issue("ERROR", str(path), "(解析)", f"YAML 语法错误: {exc}"))
            continue
        if data is None:
            issues.append(Issue("ERROR", str(path), "(根节点)", "空文件"))
            continue

        issues.extend(validate_entry(data, path))
        if isinstance(data, dict):
            entries.append((path, data))

    issues.extend(validate_cross(entries))

    errors = [i for i in issues if i.level == "ERROR"]
    warns = [i for i in issues if i.level == "WARN"]
    blocked = bool(errors) or (args.strict and bool(warns))

    if args.json:
        print(json.dumps({
            "scanned": len(files),
            "parsed": len(entries),
            "errors": len(errors),
            "warnings": len(warns),
            "blocked": blocked,
            "strict": args.strict,
            "issues": [asdict(i) for i in issues],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"共扫描 {len(files)} 个文件，解析成功 {len(entries)} 个；"
              f"ERROR {len(errors)} 项，WARN {len(warns)} 项")
        print("-" * 72)
        for issue in issues:
            print(f"[{issue.level:5s}] {Path(issue.file).name} :: {issue.field} :: {issue.message}")
        print("-" * 72)
        if blocked:
            tail = "（严格模式：WARN 亦阻断）" if (args.strict and not errors) else "，请修复全部 ERROR"
            print("结论: 阻断入库" + tail)
        else:
            print("结论: 允许入库（WARN 项建议人工复审）")

    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
