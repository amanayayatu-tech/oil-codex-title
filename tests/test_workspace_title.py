"""工作区标题后缀格式与替换测试，不访问宿主元数据或调用模型。"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as title


class WorkspaceTitleTests(unittest.TestCase):
    def test_ascii_width_boundaries(self):
        self.assertEqual(title.workspace_suffix("abcdefghi"), "（abcdefghi）")
        self.assertEqual(title.workspace_suffix("abcdefghij"), "（abcdefghij）")
        self.assertEqual(title.workspace_suffix("abcdefghijk"), "（abcdefghij...）")

    def test_cjk_width_boundaries(self):
        self.assertEqual(title.workspace_suffix("中文项目"), "（中文项目）")
        self.assertEqual(title.workspace_suffix("中文项目甲"), "（中文项目甲）")
        self.assertEqual(title.workspace_suffix("中文项目甲乙"), "（中文项目甲...）")

    def test_mixed_width_boundary_keeps_only_fitting_characters(self):
        self.assertEqual(title.workspace_suffix("abcdef中文"), "（abcdef中文）")
        self.assertEqual(title.workspace_suffix("abcdefg中文"), "（abcdefg中...）")

    def test_combining_mark_stays_with_prefix_without_byte_slicing(self):
        name = "abcdefghi\u0301Zx"
        self.assertEqual(title.workspace_suffix(name), "（abcdefghi\u0301Z...）")
        self.assertEqual(title.workspace_suffix("e\u0301"), "（e\u0301）")
        self.assertEqual(title.workspace_suffix("⚙️"), "（⚙️）")

    def test_none_means_known_local_project(self):
        self.assertEqual(title.workspace_suffix(None), "（本地项目）")
        self.assertEqual(
            title.format_workspace_title("🎬 对象｜目标", None),
            "🎬 对象｜目标（本地项目）",
        )

    def test_repeated_formatting_replaces_current_suffix_once(self):
        core = "🎬 对象｜目标（保留语义括号）"
        formatted = title.format_workspace_title(core, "abcdefghijk")
        self.assertEqual(formatted, "🎬 对象｜目标（保留语义括号）（abcdefghij...）")
        self.assertEqual(title.format_workspace_title(formatted, "abcdefghijk"), formatted)

    def test_known_previous_suffix_is_replaced(self):
        core = "🧩 对象｜目标（保留括号）"
        old_suffix = title.workspace_suffix("旧工作区")
        old_title = core + old_suffix
        self.assertEqual(
            title.format_workspace_title(old_title, "新工作区", previous_suffix=old_suffix),
            core + title.workspace_suffix("新工作区"),
        )

    def test_explicit_previous_none_replaces_known_local_project_suffix(self):
        core = "📝 对象｜目标"
        self.assertEqual(
            title.format_workspace_title(
                core + title.workspace_suffix(None), "新工作区", previous_suffix=None
            ),
            core + title.workspace_suffix("新工作区"),
        )

    def test_unrelated_or_unknown_parentheses_are_preserved(self):
        core = "🔎 对象｜目标（普通括号）"
        self.assertEqual(
            title.format_workspace_title(core, "工作区"),
            core + title.workspace_suffix("工作区"),
        )
        old_title = core + "（未知旧标签）"
        self.assertEqual(
            title.format_workspace_title(old_title, "工作区"),
            old_title + title.workspace_suffix("工作区"),
        )

    def test_empty_and_invalid_inputs_are_rejected(self):
        for value in ("", 42):
            with self.subTest(workspace_name=value), self.assertRaises((TypeError, ValueError)):
                title.workspace_suffix(value)
        with self.assertRaises(ValueError):
            title.format_workspace_title("", "工作区")
        with self.assertRaises(TypeError):
            title.format_workspace_title(42, "工作区")
        with self.assertRaises(ValueError):
            title.format_workspace_title("核心标题", "工作区", previous_suffix="（）")
        with self.assertRaises(TypeError):
            title.format_workspace_title("核心标题", "工作区", previous_suffix=42)


if __name__ == "__main__":
    unittest.main()
