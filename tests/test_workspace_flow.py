"""用实际入口验证工作区转交、首次命名冻结和既有保护；不调用模型。"""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as title
import test_title as fixtures

ID, TURN, NEW_TURN = fixtures.ID, fixtures.TURN, fixtures.NEW_TURN
CORE = "🎬 产品视频｜讲解大纲"


def host_payload(name="中文项目甲乙", project_id="ui-project"):
    return {
        "threads": {"structuredContent": {"schemaVersion": 2,
            "threads": [{"id": ID, "kind": "codex", "hostId": "local", "projectId": project_id,
                         "cwd": "/workspace/not-the-label", "summary": "不应保存的摘要"}],
            "pinnedThreads": [], "unavailableHosts": [], "unavailableSources": []}},
        "projects": {"structuredContent": {"schemaVersion": 2,
            "projects": [{"projectId": "ui-project", "projectKind": "local", "hostId": "local",
                          "label": name, "path": "/workspace/not-the-label"}]}},
    }


class WorkspaceFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = fixtures.FakeBackend()
        self.backend.thread.update(cwd="/workspace/not-the-label", projectId=None)
        self.config = title.DEFAULTS.copy()
        self.reader = lambda thread: title.read_workspace_context(self.root, ID, thread)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, name="中文项目甲乙", project_id="ui-project", turn=TURN):
        return title.save_workspace_context(self.root, ID, turn, host_payload(name, project_id))

    def process(self, generator=fixtures.proposal, *, automatic=False, apply=True, reader=None):
        turn = self.backend.thread["turns"][-1]["id"] if automatic else None
        return title.process_thread(self.backend, generator, ID, self.root, self.config,
                                    apply=apply, event_turn=turn,
                                    workspace_reader=self.reader if reader is None else reader)

    def append(self, text="改成另一个明确目标"):
        self.backend.thread["turns"].append({"id": NEW_TURN, "status": "completed", "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": text}]}]})

    def test_host_association_overrides_cli_null_and_directory_hint(self):
        self.save()
        seen = []
        def generate(context):
            seen.append(copy.deepcopy(context))
            return fixtures.proposal(context)
        result = self.process(generate)
        self.assertEqual(result["title"], CORE + "（中文项目甲...）")
        self.assertEqual(self.backend.thread["name"], result["title"])
        self.assertEqual(seen[0]["current_title"], "讨论事情")
        self.assertNotIn("workspace_name", seen[0])
        saved = title.workspace_context_path(self.root, ID).read_text(encoding="utf-8")
        self.assertNotIn("/workspace/", saved)
        self.assertNotIn("不应保存的摘要", saved)

    def test_null_project_id_uses_exact_host_workspace_path(self):
        self.save(name="elep", project_id=None)
        self.assertEqual(self.process()["title"], CORE + "（elep）")
        self.assertEqual(title.read_json(title.workspace_context_path(self.root, ID))["project_id"], "ui-project")

    def test_no_project_id_and_no_exact_path_uses_local(self):
        payload = host_payload(project_id=None)
        payload["projects"]["structuredContent"]["projects"][0]["path"] = "/workspace/other"
        title.save_workspace_context(self.root, ID, TURN, payload)
        self.assertEqual(self.process()["title"], CORE + "（本地项目）")

    def test_ambiguous_exact_path_keeps_original(self):
        payload = host_payload(project_id=None)
        duplicate = dict(payload["projects"]["structuredContent"]["projects"][0], projectId="other-project")
        payload["projects"]["structuredContent"]["projects"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "多个宿主项目"):
            title.save_workspace_context(self.root, ID, TURN, payload)
        self.assertEqual(self.process(Mock(side_effect=AssertionError("不能调用模型")))["status"], "workspace_context_missing")

    def test_explicit_suffix_correction_keeps_first_core_title(self):
        payload = host_payload(project_id=None)
        payload["projects"]["structuredContent"]["projects"][0]["path"] = "/workspace/other"
        title.save_workspace_context(self.root, ID, TURN, payload)
        self.assertEqual(self.process()["title"], CORE + "（本地项目）")
        self.save(name="elep", project_id=None)
        result = self.process(Mock(side_effect=AssertionError("纠正后缀不应重新调用模型")))
        self.assertEqual(result["status"], "renamed")
        self.assertEqual(result["title"], CORE + "（elep）")
        self.assertTrue(title.read_json(title.state_path(self.root, ID))["auto_title_finalized"])

    def test_missing_or_inconsistent_host_data_invalidates_prior_context(self):
        for change in ("missing_id", "missing_project", "wrong_host", "failed_read", "bad_json"):
            with self.subTest(change=change):
                self.save()
                payload = host_payload()
                if change == "missing_id":
                    del payload["threads"]["structuredContent"]["threads"][0]["projectId"]
                elif change == "missing_project":
                    payload["projects"]["structuredContent"]["projects"] = []
                elif change == "wrong_host":
                    payload["threads"]["structuredContent"]["threads"][0]["hostId"] = "remote"
                elif change == "failed_read":
                    payload["threads"] = {"isError": True, "content": []}
                else:
                    payload = "not json"
                with self.assertRaises((ValueError, KeyError)):
                    title.save_workspace_context(self.root, ID, TURN, payload)
                model = Mock(side_effect=AssertionError("元数据未知不能调用模型"))
                self.assertEqual(self.process(model)["status"], "workspace_context_missing")
                model.assert_not_called()
                self.assertEqual(self.backend.writes, [])

    def test_unavailable_local_host_is_not_treated_as_unassociated(self):
        payload = host_payload(project_id=None)
        payload["threads"]["structuredContent"]["unavailableHosts"] = ["local"]
        with self.assertRaises(ValueError):
            title.save_workspace_context(self.root, ID, TURN, payload)

    def test_expired_or_different_turn_context_never_starts_model(self):
        for change in ("expired", "future", "turn"):
            with self.subTest(change=change):
                self.backend = fixtures.FakeBackend()
                self.save()
                path = title.workspace_context_path(self.root, ID)
                record = title.read_json(path)
                if change == "turn":
                    record["turn_id"] = NEW_TURN
                else:
                    record["verified_at"] = time.time() + (60 if change == "future" else -600)
                title.atomic_json(path, record)
                model = Mock(side_effect=AssertionError("陈旧上下文不能调用模型"))
                self.assertTrue(self.process(model)["status"].startswith("workspace_context_"))
                model.assert_not_called()
                self.assertEqual(self.backend.writes, [])

    def test_preview_does_not_write_title_or_finalize(self):
        self.save()
        before = copy.deepcopy(self.backend.thread)
        self.assertEqual(self.process(apply=False, automatic=True)["status"], "preview")
        self.assertEqual(self.backend.thread, before)
        self.assertFalse(title.state_path(self.root, ID).exists())

    def test_first_success_freezes_title_icon_and_workspace_forever(self):
        self.save()
        model = Mock(side_effect=fixtures.proposal)
        self.assertEqual(self.process(model, automatic=True)["status"], "renamed")
        fixed = self.backend.thread["name"]
        self.append()
        self.save("换了工作区", turn=NEW_TURN)
        unavailable = Mock(side_effect=AssertionError("定名后不再读取元数据"))
        with patch.object(title, "POLICY_VERSION", title.POLICY_VERSION + 10):
            result = self.process(model, automatic=True, reader=unavailable)
        self.assertEqual(result["status"], "frozen")
        self.assertEqual(self.backend.thread["name"], fixed)
        self.assertEqual(len(self.backend.writes), 1)
        model.assert_called_once()
        unavailable.assert_not_called()
        self.assertTrue(title.read_json(title.state_path(self.root, ID))["auto_title_finalized"])

    def test_existing_successful_or_assessed_titles_are_frozen_on_upgrade(self):
        for saved in ({"last_generated_title": CORE, "policy_version": 1},
                      {"last_fingerprint": "legacy", "last_seen_title": CORE, "policy_version": 1},
                      {"last_fingerprint": "legacy", "last_seen_title": "旧版保留的准确名称", "policy_version": 1}):
            with self.subTest(saved=saved):
                title.atomic_json(title.state_path(self.root, ID), saved)
                backend = Mock(side_effect=AssertionError("旧标题升级也不能重新读写"))
                result = title.process_thread(backend, lambda _: self.fail(), ID, self.root,
                                              self.config, apply=True, event_turn=TURN,
                                              workspace_reader=lambda _: self.fail())
                self.assertEqual(result["status"], "frozen")

    def test_external_manual_title_after_finalization_is_preserved(self):
        self.save()
        self.process(automatic=True)
        self.backend.thread["name"] = "我手动选择的名字"
        self.append()
        self.assertEqual(self.process(lambda _: self.fail(), automatic=True)["status"], "frozen")
        self.assertEqual(self.backend.thread["name"], "我手动选择的名字")

    def test_public_lock_unlock_preserves_finalization_and_confirms_pending_write(self):
        current = "旧版保留的准确名称"
        cases = (({"last_fingerprint": "legacy", "last_seen_title": current}, True),
                 ({"pending_title": current}, True),
                 ({"pending_title": "尚未实际写入的名字"}, False), ({}, False))
        for saved, finalized in cases:
            with self.subTest(saved=saved):
                title.atomic_json(title.state_path(self.root, ID), saved)
                backend = Mock()
                backend.__enter__ = Mock(return_value=backend)
                backend.__exit__ = Mock(return_value=False)
                backend.read.return_value = {"name": current}
                with patch.dict(os.environ, {"OIL_CODEX_TITLE_DATA": str(self.root)}), \
                     patch.object(title, "find_codex", return_value="/unused"), \
                     patch.object(title, "CodexBackend", return_value=backend):
                    for command in ("lock", "unlock"):
                        with patch.object(sys, "argv", ["title", command, ID]), redirect_stdout(io.StringIO()):
                            self.assertEqual(title.main(), 0)
                        self.assertEqual(title.auto_title_finalized(title.read_json(
                            title.state_path(self.root, ID))), finalized)

    def test_explicit_rename_still_works_after_finalization(self):
        self.save()
        self.process(automatic=True)
        self.append("用户明确要求按新的目标重命名")
        self.save("新工作区", turn=NEW_TURN)
        candidate = {"action": "rename", "title": "🎙️ 音频转写｜文字整理", "reason": "用户请求"}
        result = self.process(lambda _: (candidate, {}))
        self.assertEqual(result["title"], "🎙️ 音频转写｜文字整理（新工作区）")
        self.assertEqual(len(self.backend.writes), 2)

    def test_greeting_does_not_name_or_finalize_before_a_real_goal(self):
        self.backend.thread["turns"][0]["items"] = [
            {"type": "userMessage", "content": [{"type": "text", "text": "你好"}]}]
        self.assertEqual(self.process(lambda _: self.fail(), automatic=True,
                                      reader=lambda _: self.fail())["status"], "awaiting_title")
        self.assertFalse(title.state_path(self.root, ID).exists())
        self.append("请整理产品视频的大纲")
        self.save(turn=NEW_TURN)
        self.assertEqual(self.process(automatic=True)["status"], "renamed")

    def test_uncertain_placeholder_is_not_finalized(self):
        self.save()
        keep = lambda _: ({"action": "keep", "title": "", "reason": "信息不足"}, {})
        self.assertEqual(self.process(keep, automatic=True)["status"], "awaiting_title")
        self.assertEqual(self.backend.writes, [])
        self.assertFalse(title.state_path(self.root, ID).exists())

    def test_explicit_workspace_refresh_only_updates_suffix_without_model(self):
        self.save("原工作区")
        self.process()
        self.save("新工作区")
        result = self.process(lambda _: self.fail("仅换工作区无需模型"))
        self.assertEqual(result["title"], CORE + "（新工作区）")

    def test_metadata_change_during_generation_discards_result(self):
        self.save("原工作区")
        def moved(context):
            self.save("新工作区")
            return fixtures.proposal(context)
        self.assertEqual(self.process(moved, automatic=True)["status"], "stale_result")
        self.assertEqual(self.backend.writes, [])
        self.assertFalse(title.state_path(self.root, ID).exists())

    def test_model_sees_core_and_long_complete_title_is_valid(self):
        core = "🧩 " + "a" * 20 + "｜" + "b" * 25
        self.assertEqual(len(core), 48)
        self.save("abcdefghijk")
        workspace = self.reader(self.backend.thread)
        candidate = {"action": "rename", "title": core, "reason": "明确"}
        full = title.validate_candidate(candidate, "旧标题", workspace=workspace)["title"]
        self.assertEqual(full, core + "（abcdefghij...）")
        kept = title.validate_candidate({"action": "keep", "title": "忽略", "reason": "准确"},
                                        full, workspace=workspace)
        self.assertEqual(kept["title"], full)
        self.assertEqual(kept["action"], "keep")

    def test_public_metadata_command_and_request_hook(self):
        env = {**os.environ, "OIL_CODEX_TITLE_DATA": str(self.root)}
        env.pop("OIL_CODEX_TITLE_WORKER", None)
        title.atomic_json(self.root / "config.json", {"codex_bin": "/not-a-real-codex"})
        cmd = [sys.executable, str(ROOT / "scripts/oil_codex_title.py")]
        prepared = subprocess.run(cmd + ["workspace-context", ID, "--turn", TURN],
            input=json.dumps(host_payload()), encoding="utf-8", capture_output=True, env=env, check=True)
        self.assertEqual(json.loads(prepared.stdout)["status"], "workspace_saved")
        event = {"hook_event_name": "UserPromptSubmit", "session_id": ID, "turn_id": TURN,
                 "prompt": "不应复制到钩子输出的私密正文"}
        requested = subprocess.run(cmd + ["workspace-request"], input=json.dumps(event), encoding="utf-8",
                                   capture_output=True, env=env, check=True)
        context = json.loads(requested.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("list_threads", context)
        self.assertIn("workspace-context", context)
        self.assertNotIn(event["prompt"], context)
        title.atomic_json(title.state_path(self.root, ID), {"last_generated_title": CORE})
        for command, hook_event in (("workspace-request", "UserPromptSubmit"), ("hook", "Stop")):
            event["hook_event_name"] = hook_event
            frozen = subprocess.run(cmd + [command], input=json.dumps(event), encoding="utf-8",
                                    capture_output=True, env=env, check=True)
            self.assertEqual(frozen.stdout, "{}\n")
        self.assertFalse((self.root / "usage").exists())


if __name__ == "__main__":
    unittest.main()
