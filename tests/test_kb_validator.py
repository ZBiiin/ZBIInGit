#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_validator.py 单元测试
========================
运行:
    python -m unittest discover -s tests -v
    python tests/test_kb_validator.py -v

依赖: PyYAML（与被测模块相同）
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import kb_validator  # noqa: E402


# ------------------------- 基准样例 -------------------------

def base_case() -> dict:
    return {
        "id": "CASE-2026-001",
        "title": "网关限流改造降低误杀率",
        "domain": "后端/网关",
        "tags": ["限流", "高并发"],
        "background": "电商大促期间网关峰值 5 万 QPS，原单点限流会误杀正常请求。",
        "problem": "令牌桶全局共享导致热点接口挤占配额，误杀率达到百分之三。",
        "solution": "对比 Sentinel 与 Redis 滑动窗口，选择后者以支持按接口独立配额。",
        "implementation": "先灰度一成流量并双写对比一周，再全量切换，回滚使用开关降级。",
        "verification": "误杀率从 3% 降至 0.2%，P99 延迟增加 2ms，大促期间零故障。",
        "lessons": "冷启动需预热缓存；低于一千 QPS 的场景不值得引入。",
        "meta": {"version": "v1.0", "date": "2026-10", "owner": "张三", "status": "生效"},
    }


def base_fact() -> dict:
    return {
        "type": "fact",
        "id": "FACT-2026-001",
        "title": "社会体育指导员（攀岩）报考渠道与费用",
        "category": "证书",
        "issuer": "各省体育行业职业技能鉴定站，证书由人社部统一印制。",
        "channel": "经备案培训机构完成学时后，在 www.sportosta.org.cn 网上报名。",
        "cost": "鉴定费五级 150 至 315 元，培训费约 2400 元。",
        "validity": "五级晋升四级需持证并从业满 1 年。",
        "scope": "适用于攀岩指导从业，不含中登协指导员认证体系。",
        "source": "体育总局人力中心《体育行业国家职业资格考试报考指南》https://www.sport.gov.cn/rlzx/",
        "meta": {"version": "v1.0", "date": "2026-10", "owner": "ZBiiin", "status": "生效"},
    }


# ------------------------- 测试基类 -------------------------

class ValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- 辅助 ----
    def write(self, name: str, data, *, raw: str | None = None) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def run_cli(self, target: Path, *flags: str) -> tuple[int, str]:
        argv = ["kb_validator.py", str(target), *flags]
        buffer = io.StringIO()
        original = sys.argv
        sys.argv = argv
        try:
            with redirect_stdout(buffer):
                code = kb_validator.main()
        finally:
            sys.argv = original
        return code, buffer.getvalue()

    def issues_of(self, data: dict) -> list[kb_validator.Issue]:
        return kb_validator.validate_entry(data, Path("sample.yaml"))

    def levels(self, issues: list[kb_validator.Issue]) -> list[str]:
        return [i.level for i in issues]

    def messages(self, issues: list[kb_validator.Issue]) -> str:
        return "\n".join(f"{i.field}:{i.message}" for i in issues)


# ------------------------- 基准通过 -------------------------

class TestBaselinePass(ValidatorTestCase):
    def test_valid_case_passes(self):
        self.write("case.yaml", base_case())
        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("ERROR 0 项", out)
        self.assertIn("WARN 0 项", out)
        self.assertIn("允许入库", out)

    def test_valid_fact_passes(self):
        self.write("fact.yaml", base_fact())
        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("ERROR 0 项", out)
        self.assertIn("允许入库", out)


# ------------------------- 案例型字段 -------------------------

class TestCaseFields(ValidatorTestCase):
    def test_missing_verification_is_error(self):
        data = base_case()
        del data["verification"]
        issues = self.issues_of(data)
        self.assertIn("ERROR", self.levels(issues))
        self.assertIn("verification", [i.field for i in issues])

    def test_missing_domain_is_error(self):
        data = base_case()
        del data["domain"]
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "domain" for i in issues))

    def test_empty_tags_is_error(self):
        data = base_case()
        data["tags"] = []
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "tags" for i in issues))

    def test_short_section_warns(self):
        data = base_case()
        data["problem"] = "太短"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "WARN" and i.field == "problem" for i in issues))

    def test_verification_without_metric_warns(self):
        data = base_case()
        data["verification"] = "改造完成后整体运行稳定，未再出现相关客诉。"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "WARN" and i.field == "verification" for i in issues))

    def test_step_number_is_not_a_metric(self):
        data = base_case()
        data["verification"] = "按第 3 步完成上线，整体运行稳定无异常。"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "WARN" and i.field == "verification" for i in issues))

    def test_before_after_metrics_pass(self):
        data = base_case()
        data["verification"] = "错误率从 5% 降至 0.1%，恢复时间缩短 40 秒。"
        issues = self.issues_of(data)
        self.assertFalse(any(i.field == "verification" for i in issues))

    def test_vague_words_warn(self):
        data = base_case()
        data["lessons"] = "整体效果很好，性能大幅提升，后续可推广。"
        issues = self.issues_of(data)
        warn_text = self.messages([i for i in issues if i.level == "WARN"])
        self.assertIn("模糊表述", warn_text)


# ------------------------- 事实卡字段 -------------------------

class TestFactFields(ValidatorTestCase):
    def test_missing_source_is_error(self):
        data = base_fact()
        del data["source"]
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "source" for i in issues))

    def test_missing_validity_is_error(self):
        data = base_fact()
        del data["validity"]
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "validity" for i in issues))

    def test_source_without_official_hint_warns(self):
        data = base_fact()
        data["source"] = "朋友口述，没有文档"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "WARN" and i.field == "source" for i in issues))

    def test_fact_skips_section_length_rule(self):
        data = base_fact()
        data["scope"] = "从业"
        issues = self.issues_of(data)
        self.assertFalse(any(i.field == "scope" and i.level == "WARN" for i in issues))

    def test_fact_id_prefix_mismatch_warns(self):
        data = base_fact()
        data["id"] = "CASE-2026-999"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "id" and i.level == "WARN" for i in issues))


# ------------------------- 元数据 -------------------------

class TestMeta(ValidatorTestCase):
    def test_missing_owner_is_error(self):
        data = base_case()
        data["meta"]["owner"] = ""
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "meta.owner" for i in issues))

    def test_missing_meta_object_is_error(self):
        data = base_case()
        data["meta"] = None
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and i.field == "meta" for i in issues))

    def test_bad_date_format_warns(self):
        data = base_case()
        data["meta"]["date"] = "2026/10"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "meta.date" and i.level == "WARN" for i in issues))

    def test_unknown_status_warns(self):
        data = base_case()
        data["meta"]["status"] = "已上线"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "meta.status" and i.level == "WARN" for i in issues))

    def test_stale_date_warns(self):
        data = base_case()
        data["meta"]["date"] = "2020-01"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "meta.date" and "个月" in i.message for i in issues))

    def test_recent_date_not_stale(self):
        data = base_case()
        data["meta"]["date"] = "2026-10"
        issues = self.issues_of(data)
        self.assertFalse(any(i.field == "meta.date" and "个月" in i.message for i in issues))


# ------------------------- 命名与结构 -------------------------

class TestNaming(ValidatorTestCase):
    def test_bad_id_pattern_warns(self):
        data = base_case()
        data["id"] = "CASE-26-1"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "id" and i.level == "WARN" for i in issues))

    def test_case_id_prefix_mismatch_warns(self):
        data = base_case()
        data["id"] = "FACT-2026-001"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "id" and i.level == "WARN" for i in issues))

    def test_tags_string_warns(self):
        data = base_case()
        data["tags"] = "限流"
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "tags" and i.level == "WARN" for i in issues))

    def test_tags_with_non_string_warns(self):
        data = base_case()
        data["tags"] = ["限流", 123]
        issues = self.issues_of(data)
        self.assertTrue(any(i.field == "tags" and i.level == "WARN" for i in issues))


# ------------------------- 敏感信息 -------------------------

class TestSensitive(ValidatorTestCase):
    def test_plaintext_password_is_error(self):
        data = base_case()
        data["password"] = "Abc123456"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and "凭据" in i.message for i in issues))

    def test_internal_ip_is_error(self):
        data = base_case()
        data["db_host"] = "192.168.1.20"
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and "内网" in i.message for i in issues))

    def test_private_key_is_error(self):
        data = base_case()
        data["note"] = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        issues = self.issues_of(data)
        self.assertTrue(any(i.level == "ERROR" and "私钥" in i.message for i in issues))

    def test_placeholder_is_not_flagged(self):
        data = base_case()
        data["password"] = "<your-password>"
        issues = self.issues_of(data)
        self.assertFalse(any("凭据" in i.message for i in issues))

    def test_asterisk_placeholder_is_not_flagged(self):
        data = base_case()
        data["token"] = "********"
        issues = self.issues_of(data)
        self.assertFalse(any("凭据" in i.message for i in issues))


# ------------------------- 跨条目 -------------------------

class TestCrossEntries(ValidatorTestCase):
    def test_duplicate_id_is_error(self):
        first = base_case()
        self.write("a.yaml", first)
        second = base_case()
        second["title"] = "另一个完全不同的标题用于避免相似度检查"
        self.write("b.yaml", second)

        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 1)
        self.assertIn("ID 与 a.yaml 重复", out)

    def test_similar_titles_warn(self):
        first = base_case()
        self.write("a.yaml", first)

        second = base_case()
        second["id"] = "CASE-2026-002"
        second["title"] = "网关限流改造降低误杀率分析"
        self.write("b.yaml", second)

        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("标题相似度", out)


# ------------------------- 文件收集 -------------------------

class TestFileCollection(ValidatorTestCase):
    def test_template_files_skipped_by_default(self):
        self.write("case.yaml", base_case())
        self.write("entry_template.yaml", {"id": "CASE-0000-000"})
        self.write("fact_template.yaml", {"id": "FACT-0000-000"})

        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("共扫描 1 个文件", out)

    def test_include_templates_flag(self):
        self.write("case.yaml", base_case())
        self.write("entry_template.yaml", {"id": "CASE-0000-000"})

        code, out = self.run_cli(self.dir, "--include-templates")
        self.assertIn("共扫描 2 个文件", out)
        self.assertEqual(code, 1)
        self.assertIn("entry_template.yaml", out)

    def test_skipped_directories(self):
        self.write("case.yaml", base_case())
        self.write(".git/hidden.yaml", {"id": "BAD"})
        self.write("node_modules/pkg/x.yaml", {"id": "BAD"})

        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("共扫描 1 个文件", out)


# ------------------------- 解析异常 -------------------------

class TestParsing(ValidatorTestCase):
    def test_yaml_syntax_error_is_error(self):
        self.write("broken.yaml", None, raw="id: [1, 2\n  bad: {")
        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 1)
        self.assertIn("YAML 语法错误", out)

    def test_empty_file_is_error(self):
        self.write("empty.yaml", None, raw="")
        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 1)
        self.assertIn("空文件", out)

    def test_non_mapping_root_is_error(self):
        self.write("list.yaml", None, raw="- a\n- b\n")
        code, out = self.run_cli(self.dir)
        self.assertEqual(code, 1)
        self.assertIn("不是有效的 YAML 映射", out)


# ------------------------- CLI 行为 -------------------------

class TestCli(ValidatorTestCase):
    def test_nonexistent_path_returns_2(self):
        code, _ = self.run_cli(self.dir / "missing")
        self.assertEqual(code, 2)

    def test_empty_directory_returns_2(self):
        code, _ = self.run_cli(self.dir)
        self.assertEqual(code, 2)

    def test_strict_mode_blocks_on_warning(self):
        data = base_case()
        data["problem"] = "太短"
        self.write("case.yaml", data)

        code_normal, _ = self.run_cli(self.dir)
        self.assertEqual(code_normal, 0)

        code_strict, out = self.run_cli(self.dir, "--strict")
        self.assertEqual(code_strict, 1)
        self.assertIn("严格模式", out)

    def test_json_output_is_parseable(self):
        self.write("case.yaml", base_case())
        self.write("bad.yaml", {"id": "CASE-2026-002"})

        code, out = self.run_cli(self.dir, "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["blocked"], True)
        self.assertGreater(report["errors"], 0)
        self.assertEqual(report["errors"], len([i for i in report["issues"] if i["level"] == "ERROR"]))

    def test_issue_dataclass_fields(self):
        data = base_case()
        del data["verification"]
        issues = self.issues_of(data)
        issue = issues[0]
        self.assertIn(issue.level, {"ERROR", "WARN"})
        self.assertTrue(issue.file)
        self.assertTrue(issue.field)
        self.assertTrue(issue.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
