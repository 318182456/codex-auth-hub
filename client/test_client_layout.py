import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import client_gui


class ClientLayoutTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.resources.enter_context(patch.object(
            client_gui, "get_config_path", return_value=str(Path(directory) / "missing.json")
        ))
        for method in ("refresh_profiles", "start_auto_refresh", "prompt_login_if_needed", "save_config"):
            self.resources.enter_context(patch.object(client_gui.CodexClientApp, method))
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = client_gui.CodexClientApp(self.root)
        self.root.geometry("1200x800+5000+5000")
        self.root.deiconify()
        self.root.update()

    def profiles(self, count):
        return [{"id": index + 1, "name": f"Codex{index + 1}", "quota": {}, "active_users": []}
                for index in range(count)]

    def test_three_accounts_are_fully_visible_without_unused_table_rows(self):
        self.app.update_tree(self.profiles(3))
        for size in ("980x720", "1200x800", "1500x900"):
            with self.subTest(size=size):
                self.root.geometry(size + "+5000+5000")
                self.root.update()
                tree = self.app.tree
                self.assertEqual(len(tree.get_children()), 3)
                for item in tree.get_children():
                    bbox = tree.bbox(item)
                    self.assertTrue(bbox, "An account has scrolled out of view")
                    self.assertLessEqual(bbox[1] + bbox[3], tree.winfo_height())
                bottom = tree.bbox(tree.get_children()[-1])
                self.assertLessEqual(tree.winfo_height() - bottom[1] - bottom[3], 4)
                self.assertEqual(tree.yview(), (0.0, 1.0))
                for button in (self.app.apply_btn, self.app.btn_login):
                    self.assertTrue(button.winfo_ismapped())
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                         self.root.winfo_rooty() + self.root.winfo_height())

    def test_short_lists_shrink_to_content(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                self.app.update_tree(self.profiles(count))
                self.root.update()
                tree = self.app.tree
                bottom = tree.bbox(tree.get_children()[-1])
                self.assertTrue(bottom)
                self.assertLessEqual(tree.winfo_height() - bottom[1] - bottom[3], 4)

    def test_refresh_preserves_scroll_and_returns_short_list_to_top(self):
        tree = self.app.tree
        self.app.update_tree(self.profiles(10))
        self.root.update()
        tree.yview_moveto(0.5)
        self.root.update()
        position = tree.yview()[0]
        self.app.update_tree(self.profiles(10))
        self.root.update()
        self.assertAlmostEqual(tree.yview()[0], position)
        self.app.update_tree(self.profiles(3))
        self.root.update()
        self.assertEqual(tree.yview(), (0.0, 1.0))

    def test_weekly_only_accounts_hide_five_hour_column_and_keep_selection(self):
        profiles = self.profiles(3)
        for profile in profiles:
            profile["quota"] = {
                "five_hour": {"available": False, "remaining_percent": None},
                "weekly": {"available": True, "remaining_percent": 97, "reset_after_text": "6天"},
            }
        self.app.update_tree(profiles)
        self.root.update()
        tree = self.app.tree
        self.assertNotIn("quota_5h", tree["displaycolumns"])
        self.assertIn("quota_week", tree["displaycolumns"])
        tree.selection_set(tree.get_children()[1])
        self.app.on_select_item(None)
        self.assertEqual(str(self.app.apply_btn["state"]), "normal")
        self.assertEqual(tree.item(tree.selection()[0])["values"][1], "Codex2")
        self.assertEqual(tree.item(tree.selection()[0])["values"][4], "97%  /  6天")
        profiles[0]["quota"]["five_hour"] = {"available": True, "remaining_percent": 80}
        self.app.update_tree(profiles)
        self.root.update()
        self.assertIn("quota_5h", tree["displaycolumns"])

    def test_missing_quota_is_not_displayed_as_zero_or_full(self):
        self.assertEqual(client_gui.format_quota_window({}), "暂无数据")
        self.assertEqual(client_gui.format_quota_window({"available": False}), "未提供")
        self.assertEqual(client_gui.format_quota_window({"remaining_percent": 0}), "0%  /  重置时间未知")


if __name__ == "__main__":
    unittest.main()
