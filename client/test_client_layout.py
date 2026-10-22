import tempfile
import tkinter as tk
import unittest
from datetime import datetime
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
        self.assertNotIn("reset_5h_date", tree["displaycolumns"])
        self.assertIn("quota_week", tree["displaycolumns"])
        tree.selection_set(tree.get_children()[1])
        self.app.on_select_item(None)
        self.assertEqual(str(self.app.apply_btn["state"]), "normal")
        self.assertEqual(tree.item(tree.selection()[0])["values"][1], "Codex2")
        self.assertEqual(tree.set(tree.selection()[0], "quota_week"), "97%  /  6天")
        profiles[0]["quota"]["five_hour"] = {"available": True, "remaining_percent": 80}
        self.app.update_tree(profiles)
        self.root.update()
        self.assertIn("quota_5h", tree["displaycolumns"])
        self.assertIn("reset_5h_date", tree["displaycolumns"])

    def test_reset_dates_follow_each_quota_and_fit_wide_window(self):
        profiles = self.profiles(1)
        profiles[0]["quota"] = {
            "five_hour": {"available": True, "reset_at": "2026-10-22T15:30:00+08:00"},
            "weekly": {"available": True, "reset_at": "2026-10-28T09:00:00+08:00"},
        }
        self.app.update_tree(profiles)
        self.root.geometry("1600x800+5000+5000")
        self.root.update()
        tree = self.app.tree
        item = tree.get_children()[0]
        for quota, date in (("quota_5h", "reset_5h_date"), ("quota_week", "reset_week_date")):
            columns = list(tree["displaycolumns"])
            self.assertEqual(columns.index(date), columns.index(quota) + 1)
            window = profiles[0]["quota"]["five_hour" if quota == "quota_5h" else "weekly"]
            expected = datetime.fromisoformat(window["reset_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.assertEqual(tree.set(item, date), expected)
        self.assertEqual(tree.xview(), (0.0, 1.0))

    def test_reset_date_handles_missing_invalid_and_epoch_values(self):
        for window in ({}, {"reset_at": "invalid"}, {"reset_at_epoch": "invalid"}):
            self.assertEqual(client_gui.format_reset_date(window), "未知")
        self.assertEqual(client_gui.format_reset_date({"available": False}), "未提供")
        expected = datetime.fromtimestamp(1792638000).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(client_gui.format_reset_date({"reset_at_epoch": 1792638000}), expected)
        self.assertEqual(client_gui.format_reset_date({"reset_at": "2026-10-22T03:00:00Z"}),
                         datetime.fromisoformat("2026-10-22T03:00:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M"))

    def test_visible_columns_fill_table_after_hiding_and_restoring_five_hour(self):
        profiles = self.profiles(3)
        for size in ("1200x800", "1600x800"):
            self.root.geometry(size + "+5000+5000")
            for available in (False, True, False):
                with self.subTest(size=size, five_hour=available):
                    for profile in profiles:
                        profile["quota"] = {"five_hour": {"available": available}}
                    self.app.update_tree(profiles)
                    self.root.update()
                    tree = self.app.tree
                    item = tree.get_children()[0]
                    columns = tuple(tree["displaycolumns"])
                    total = sum(int(tree.column(column, "width")) for column in columns)
                    minimum = sum(int(tree.column(column, "minwidth")) for column in columns)
                    self.assertEqual(total, max(minimum, tree.winfo_width() - 4))
                    for index, column in enumerate(columns, 1):
                        self.assertEqual(str(tree.heading(column, "anchor")), str(tree.column(column, "anchor")))
                        x, y, width, height = tree.bbox(item, column)
                        center = x + width // 2
                        if center < tree.winfo_width() - 4:
                            self.assertEqual(tree.identify_column(center), f"#{index}")

    def test_rendered_heading_and_row_grid_pixels_align(self):
        import ctypes
        import win32gui
        import win32ui

        self.root.geometry("1200x800+0+0")
        profiles = self.profiles(3)
        tree = self.app.tree
        for five_hour, scroll in ((False, 0), (True, 0), (True, 0.5)):
            with self.subTest(five_hour=five_hour, scroll=scroll):
                for profile in profiles:
                    profile["quota"] = {"five_hour": {"available": five_hour}}
                self.app.update_tree(profiles)
                self.root.update()
                tree.xview_moveto(scroll)
                tree.selection_set(tree.get_children()[1])
                self.root.update()
                hwnd = tree.winfo_id()
                width, height = tree.winfo_width(), tree.winfo_height()
                handle = win32gui.GetWindowDC(hwnd)
                source = win32ui.CreateDCFromHandle(handle)
                target = source.CreateCompatibleDC()
                bitmap = win32ui.CreateBitmap()
                bitmap.CreateCompatibleBitmap(source, width, height)
                target.SelectObject(bitmap)
                try:
                    self.assertEqual(ctypes.windll.user32.PrintWindow(hwnd, target.GetSafeHdc(), 2), 1)
                    pixels = bitmap.GetBitmapBits(True)
                    def grid_at(y):
                        return [x for x in range(width)
                                if pixels[(y * width + x) * 4:(y * width + x) * 4 + 3] == bytes((238, 226, 220))]
                    heading_grid = grid_at(20)
                    self.assertGreater(len(heading_grid), 5)
                    for item in tree.get_children():
                        bbox = tree.bbox(item)
                        self.assertEqual(grid_at(bbox[1] + bbox[3] // 2), heading_grid)
                finally:
                    target.DeleteDC()
                    source.DeleteDC()
                    win32gui.DeleteObject(bitmap.GetHandle())
                    win32gui.ReleaseDC(hwnd, handle)

    def test_missing_quota_is_not_displayed_as_zero_or_full(self):
        self.assertEqual(client_gui.format_quota_window({}), "暂无数据")
        self.assertEqual(client_gui.format_quota_window({"available": False}), "未提供")
        self.assertEqual(client_gui.format_quota_window({"remaining_percent": 0}), "0%  /  重置时间未知")


if __name__ == "__main__":
    unittest.main()
