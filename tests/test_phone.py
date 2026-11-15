"""手机取图桥接层测试：用假 adb 覆盖整条链路，不需要真的插手机。"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))

from phone import (INJECT_MARKERS, Device, PhoneBridge, PhoneError,  # noqa: E402
                   RemoteImage, UiNode, build_monkey_script, detect_page,
                   device_display_name, diff_new, find_node, guide_for,
                   history_rows, is_weight_only_detail, make_stamp, migrate_steps,
                   monkey_key_lines, monkey_swipe_lines, monkey_tap_lines,
                   parse_bounds, parse_devices, parse_ls_output, parse_size,
                   parse_ui_xml, row_bodyfat_state, set_steps_profile, steps_profile)
from auto_collect import AutoCollector  # noqa: E402


def tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) \
        + chunk(b"IEND", b"")


PNG = tiny_png()


class FakeBridge(PhoneBridge):
    """替换唯一的 run() 出口，模拟一台红米手机。"""

    def __init__(self, with_device: bool = True,
                 inject_blocked: bool = False) -> None:
        super().__init__(adb=Path("fake-adb.exe"))
        self.serial = "FAKE123"
        self.with_device = with_device
        self.inject_blocked = inject_blocked
        self.commands: list[list[str]] = []
        self.ui_xml = b"<hierarchy/>"
        self.files: dict[str, int] = {
            "/sdcard/DCIM/Screenshots/Screenshot_20261115_160000.jpg": 234567,
        }
        self._tmp = Path(tempfile.mkdtemp(prefix="phone-test-"))

    def unique_dest(self, remote: str) -> Path:
        name = Path(remote).name
        dest = self._tmp / name
        index = 1
        while dest.exists():
            dest = self._tmp / f"{index}_{name}"
            index += 1
        return dest

    def _listing(self) -> bytes:
        lines = ["@@/sdcard/DCIM/Screenshots"]
        for path, size in self.files.items():
            lines.append(f"-rw-rw---- 1 u0_a123 media_rw {size} "
                         f"2026-11-15 16:20 {Path(path).name}")
        return ("\n".join(lines) + "\n").encode()

    def _recursive(self) -> bytes:
        return ("\n".join(f"{size} {path}" for path, size in self.files.items())
                + "\n").encode()

    def run(self, args: list[str], *, timeout: float = 15) -> bytes:
        self.commands.append(args)
        if args[:1] == ["devices"]:
            body = ("List of devices attached\n"
                    "FAKE123 device product:zijin model:25053RT47C\n\n"
                    if self.with_device else "List of devices attached\n\n")
            return body.encode()
        if args[:1] == ["exec-out"]:
            return self.ui_xml if args[1:2] == ["cat"] else PNG
        if args[:1] == ["pull"]:
            Path(args[2]).write_bytes(PNG)
            return b"OK"
        if args[:1] == ["push"]:
            return b"OK"
        if args[:1] == ["shell"]:
            command = args[1]
            if command.startswith("input ") and self.inject_blocked:
                raise PhoneError("Exception occurred while executing 'tap': "
                                 "java.lang.SecurityException: Injecting input "
                                 "events requires the caller to have the "
                                 "INJECT_EVENTS permission.")
            if command.startswith("monkey -f"):
                return b"Events injected: 3\n"
            if "dumpsys window" in command:
                return (b"  mCurrentFocus=Window{abc u0 "
                        b"com.huawei.health/com.huawei.health.MainActivity}\n")
            if command.startswith("uiautomator dump"):
                return b"UI hierchary dumped to: /sdcard/x.xml"
            if "getprop" in command:
                return b"25053RT47C\n"
            if "wm size" in command:
                return b"Physical size: 1220x2712\n"
            if "ls -lt" in command:
                return self._listing()
            if "find /sdcard" in command:
                return self._recursive()
            return b""
        raise PhoneError(f"未模拟的 adb 命令：{args}")


class PhoneBridgeTest(unittest.TestCase):
    def test_parsers(self) -> None:
        devices = parse_devices("List of devices attached\n"
                                "abc device product:zijin model:25053RT47C\n\n")
        self.assertEqual(devices[0].state, "device")
        self.assertEqual(devices[0].model, "25053RT47C")
        self.assertEqual(parse_size("Physical size: 1220x2712"), (1220, 2712))
        images = parse_ls_output(
            "@@/sdcard/DCIM/Screenshots\n"
            "total 3\n"
            "-rw-rw---- 1 u0_a123 media_rw 234567 2026-11-15 16:20 shot.jpg\n"
            "drwxrwx--x 2 u0_a123 media_rw 4096 2026-11-15 16:20 tmp\n")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].remote, "/sdcard/DCIM/Screenshots/shot.jpg")
        self.assertEqual(images[0].size, 234567)

    def test_attach_describe_and_screencap(self) -> None:
        bridge = FakeBridge()
        self.assertEqual(bridge.attach().model, "25053RT47C")
        self.assertIn("1220×2712", bridge.describe())
        dest = bridge.unique_dest("screen.png")
        self.assertEqual(bridge.screencap(dest).read_bytes(), PNG)
        self.assertTrue(dest.exists())

    def test_scan_baseline_then_new_image(self) -> None:
        bridge = FakeBridge()
        known, fresh = diff_new(None, bridge.scan_images())
        self.assertEqual(fresh, [], "首次扫描只建立基线，不能把老照片全导进来")
        self.assertEqual(len(known), 1)
        bridge.files["/sdcard/DCIM/Screenshots/Screenshot_20261115_162500.jpg"] = 345678
        known, fresh = diff_new(known, bridge.scan_images())
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].name, "Screenshot_20261115_162500.jpg")
        pulled = bridge.pull(fresh[0].remote, bridge.unique_dest(fresh[0].remote))
        self.assertEqual(pulled.read_bytes(), PNG)

    def test_run_steps_replays_taps_and_captures(self) -> None:
        bridge = FakeBridge()
        steps = [
            {"do": "wake", "note": "唤醒"},
            {"do": "app", "package": "com.huawei.health", "wait": 1},
            {"do": "tap", "x": 540, "y": 1800, "wait": 1, "note": "点分享"},
            {"do": "sleep", "ms": 1},
            {"do": "capture", "note": "抓屏"},
        ]
        logged: list[str] = []
        paths = bridge.run_steps(steps, logged.append)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].read_bytes().startswith(b"\x89PNG"))
        self.assertTrue(any("input tap 540 1800" in " ".join(c) for c in bridge.commands))
        self.assertTrue(any("monkey -p com.huawei.health" in " ".join(c)
                            for c in bridge.commands))
        self.assertEqual(len(logged), len(steps))

    def test_no_device_raises_actionable_error(self) -> None:
        bridge = FakeBridge(with_device=False)
        with self.assertRaises(PhoneError) as ctx:
            bridge.attach()
        self.assertIn("USB 调试", str(ctx.exception))
        self.assertIn("开发者选项", guide_for(str(ctx.exception)))

    def test_p30_pro_and_redmi_keep_independent_step_profiles(self) -> None:
        redmi = Device("REDMI", "device", model="25053RT47C")
        huawei = Device("HUAWEI", "device", model="VOG_AL00")
        redmi_steps = [{"do": "tap", "x": 651, "y": 1053}]
        data = migrate_steps({"enabled": True, "steps": redmi_steps})
        self.assertEqual(steps_profile(data, redmi)["steps"], redmi_steps)
        self.assertFalse(steps_profile(data, huawei).get("enabled", False),
                         "P30 Pro 不能误用红米坐标")
        huawei_steps = [{"do": "tap", "x": 540, "y": 900}]
        set_steps_profile(data, huawei, huawei_steps)
        self.assertEqual(steps_profile(data, huawei)["steps"], huawei_steps)
        self.assertEqual(steps_profile(data, redmi)["steps"], redmi_steps,
                         "录制华为后红米配置仍原样保留")
        self.assertIn("华为 P30 Pro", device_display_name(huawei))

    def test_p30_pro_chinese_history_time(self) -> None:
        self.assertEqual(make_stamp("2026/3/28", "晚上8:02"),
                         "2026-03-28 20:02")
        self.assertEqual(make_stamp("2026/3/28", "上午12:05"),
                         "2026-03-28 00:05")
        self.assertEqual(make_stamp("2025/5/2", "半夜11:08"),
                         "2025-05-02 23:08")


# ---------- 控件树 / 注入 / 全自动状态机的确定性测试 ----------

def _node(text: str = "", desc: str = "", rid: str = "",
          bounds: str = "[0,0][10,10]", clickable: str = "false") -> str:
    return (f'<node text="{text}" content-desc="{desc}" resource-id="{rid}" '
            f'class="android.widget.View" bounds="{bounds}" '
            f'clickable="{clickable}"/>')


def _xml(*nodes: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>")


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def real_nodes(name: str) -> list[UiNode]:
    """真机控件树精简快照（2026-11-15，红米 Turbo 5 Max / 华为运动健康）。"""
    return parse_ui_xml((FIXTURES / name).read_text(encoding="utf-8"))


def history_xml(weights=("86.7", "87.4", "88.1"), bodyfat_flags=None) -> str:
    """默认每行都有体脂；bodyfat_flags=False 的行为「只有体重」的普通称重。"""
    flags = (list(bodyfat_flags) if bodyfat_flags is not None
             else [True] * len(weights))
    parts, top = [], 334
    for index, weight in enumerate(weights):
        parts.append(_node(f"2026/10/{25-index}，日均 {weight} 公斤",
                           rid="com.huawei.health:id/health_data_weight_father_ll",
                           bounds=f"[0,{top}][1280,{top+26}]", clickable="true"))
        y0, y1 = top + 26, top + 234
        parts.append(_node(rid="com.huawei.health:id/child_item_linear_layout",
                           bounds=f"[208,{y0}][1202,{y1}]"))
        parts.append(_node(weight,
                           rid="com.huawei.health:id/child_item_weight",
                           bounds=f"[208,{y0+60}][312,{y0+130}]"))
        parts.append(_node("公斤", rid="com.huawei.health:id/child_item_weight_unit",
                           bounds=f"[327,{y0+60}][446,{y0+130}]"))
        parts.append(_node(f"{12+index}:37",
                           rid="com.huawei.health:id/child_item_time",
                           bounds=f"[1042,{y0+70}][1150,{y0+140}]"))
        if index < len(flags) and flags[index]:
            parts.append(_node(f"{28+index}.1%",
                               rid="com.huawei.health:id/child_item_fat",
                               bounds=f"[280,{y0+100}][396,{y0+160}]"))
            parts.append(_node("33.6",
                               rid="com.huawei.health:id/child_item_skeletal_muscle",
                               bounds=f"[481,{y0+100}][573,{y0+160}]"))
        top = y1 + 26
    return _xml(*parts)


def detail_xml(stamp: str = "2026/10/25 12:37") -> str:
    return _xml(
        _node("测量记录"),
        _node("86.7", rid="com.huawei.health:id/level_value_text"),
        _node(stamp),
        _node(desc="返回", rid="com.huawei.health:id/hwappbarpattern_cancel_icon",
              bounds="[39,165][195,321]", clickable="true"),
        _node(desc="分享", rid="com.huawei.health:id/hwappbarpattern_menu_icon",
              bounds="[929,165][1085,321]", clickable="true"),
        _node(desc="更多选项", rid="com.huawei.health:id/hwappbarpattern_ok_icon",
              bounds="[1085,165][1241,321]", clickable="true"),
    )


def weight_only_detail_xml(stamp: str = "2026/9/25 14:04") -> str:
    """真机：普通称重详情页，横幅「未测量到脂肪率？」+ 只有 BMI，没有「分享」。"""
    return _xml(
        _node("测量记录"),
        _node(desc="返回", rid="com.huawei.health:id/hwappbarpattern_cancel_icon",
              bounds="[39,165][195,321]", clickable="true"),
        _node(desc="更多选项", rid="com.huawei.health:id/hwappbarpattern_ok_icon",
              bounds="[1085,165][1241,321]", clickable="true"),
        _node(rid="com.huawei.health:id/health_healthdata_inputweight_top_tip",
              bounds="[0,334][1280,490]", clickable="true"),
        _node("未测量到脂肪率？", bounds="[182,377][598,447]"),
        _node("解决方法", bounds="[992,381][1176,443]"),
        _node(stamp, rid="com.huawei.health:id/hw_show_health_data_weight_bodyindex_mid_time",
              bounds="[442,568][837,638]"),
        _node("86.2", rid="com.huawei.health:id/level_value_text",
              bounds="[532,840][739,991]"),
        _node("BMI", rid="com.huawei.health:id/body_index_recycle_title",
              bounds="[104,1447][174,1499]"),
        _node("26.9", rid="com.huawei.health:id/body_index_recycle_value",
              bounds="[104,1499][226,1586]"),
    )


def share_xml() -> str:
    return _xml(
        _node("预览"),
        _node(desc="返回", rid="com.huawei.health:id/hwappbarpattern_cancel_icon",
              bounds="[39,165][195,321]", clickable="true"),
        _node(rid="com.huawei.health:id/share_save_to_local_layout",
              bounds="[39,2440][247,2694]", clickable="true"),
        _node("保存", bounds="[104,2642][182,2694]"),
    )


def dashboard_xml() -> str:
    """主首页也有「分享 / 更多选项」，不能误判成详情页。"""
    return _xml(_node("今日"), _node(desc="更多选项"),
                _node(desc="搜索"), _node("795/10,000 步"))


class FakeAutoBridge(PhoneBridge):
    """用三种真实页面结构模拟一台手机，点按会驱动页面切换。"""

    def __init__(self, weights=("86.7", "87.4", "88.1"), *,
                 duplicate: bool = False,
                 package: str = "com.huawei.health",
                 bodyfat_flags=None,
                 start_weight_only: bool = False) -> None:
        super().__init__(adb=Path("fake-adb.exe"))
        self.serial = "FAKEAUTO"
        self.inject_backend = "monkey"
        self.weights = list(weights)
        self.bodyfat_flags = (list(bodyfat_flags) if bodyfat_flags is not None
                             else [True] * len(weights))
        self.duplicate = duplicate
        self.package = package
        self.state = "detail" if start_weight_only else "history"
        self.weight_only_current = start_weight_only
        self.save_count = 0
        self.current_stamp = "2026/10/25 12:37"
        self.taps: list[tuple[str, int, int]] = []
        self.scroll_distances: list[int] = []
        self._tmp = Path(tempfile.mkdtemp(prefix="auto-test-"))

    def attach(self):
        from phone import Device
        return Device("FAKEAUTO", "device", model="FAKEAUTO")

    def current_package(self) -> str:
        return self.package

    def launch(self, package: str) -> None:
        pass

    def scroll_down(self, pixels=None) -> None:
        # 假手机只有一屏；记录下每次位移，供「小步翻页」断言使用
        self.scroll_distances.append(int(pixels or 0))

    def scroll_by(self, pixels: int, *, up: bool = False, ms: int = 700) -> None:
        self.scroll_distances.append(-int(pixels) if up else int(pixels))

    def screen_size(self):
        return (1280, 2772)

    def injection_status(self) -> str:
        return "monkey 脚本（免安全设置）"

    def scan_images(self):
        return [RemoteImage("/sdcard/Pictures", f"sporthealth-{i}.jpg", 1000 + i)
                for i in range(self.save_count)]

    def unique_dest(self, remote: str) -> Path:
        dest = self._tmp / Path(remote).name
        index = 1
        while dest.exists():
            dest = self._tmp / f"{index}_{Path(remote).name}"
            index += 1
        return dest

    def pull(self, remote: str, dest: Path) -> Path:
        index = int(remote.split("-")[-1].split(".")[0])
        payload = b"REPORT" + (b"DUP" if self.duplicate else str(index).encode())
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest

    def ui_nodes(self) -> list[UiNode]:
        if self.state == "detail":
            xml = (weight_only_detail_xml(self.current_stamp)
                   if self.weight_only_current else detail_xml(self.current_stamp))
        else:
            xml = {"history": history_xml(self.weights, self.bodyfat_flags),
                   "share": share_xml()}[self.state]
        return parse_ui_xml(xml)

    def tap(self, x: int, y: int) -> None:
        self.taps.append((self.state, int(x), int(y)))
        nodes = self.ui_nodes()

        def near(node: UiNode | None) -> bool:
            return bool(node and abs(x - node.center[0]) <= 90
                        and abs(y - node.center[1]) <= 90)

        if self.state == "history":
            rows = history_rows(nodes)
            index = next((i for i, row in enumerate(rows)
                          if row["bounds"][0] <= x <= row["bounds"][2]
                          and row["bounds"][1] <= y <= row["bounds"][3]), 0)
            self.current_stamp = f"2026/10/{25-index} {12+index}:37"
            self.weight_only_current = (index < len(self.bodyfat_flags)
                                        and not self.bodyfat_flags[index])
            self.state = "detail"
        elif self.state == "detail":
            if near(find_node(nodes, desc="分享")):
                self.state = "share"
            elif near(find_node(nodes, desc="返回")):
                self.state = "history"
        elif self.state == "share":
            if near(find_node(nodes, rid="share_save_to_local_layout")):
                self.save_count += 1  # 故意停在预览页，考验回退逻辑
            elif near(find_node(nodes, desc="返回")):
                self.state = "detail"


class UiParsingTest(unittest.TestCase):
    def test_bounds_and_page_detection(self) -> None:
        self.assertEqual(parse_bounds("[39,165][195,321]"), (39, 165, 195, 321))
        self.assertEqual(detect_page(parse_ui_xml(share_xml())), "share")
        self.assertEqual(detect_page(parse_ui_xml(detail_xml())), "detail")
        self.assertEqual(detect_page(parse_ui_xml(history_xml())), "history")
        self.assertEqual(detect_page(parse_ui_xml(dashboard_xml())), "unknown",
                         "主首页也带「更多选项」，不能被误判成测量记录详情页")

    def test_history_rows_have_stable_keys(self) -> None:
        rows = history_rows(parse_ui_xml(history_xml()))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["key"] for row in rows}), 3)
        self.assertTrue(all("2026/10" in row["label"] for row in rows))
        self.assertEqual(rows[0]["center"], (705, 464))
        self.assertEqual([row["bodyfat"] for row in rows], [True, True, True],
                         "默认夹具每行都有脂肪率，不能被误判成普通称重")

    def test_monkey_script_generation(self) -> None:
        script = build_monkey_script(monkey_tap_lines(100, 200))
        self.assertIn("type= raw events", script)
        self.assertIn("DispatchPointer(0,0,0,100,200", script)
        self.assertIn("DispatchPointer(0,0,1,100,200", script)
        self.assertIn("UserWait(150)", script)
        swipe = monkey_swipe_lines(640, 2000, 640, 700)
        self.assertTrue(any("DispatchPointer(0,0,2," in line for line in swipe))
        self.assertEqual(len(monkey_key_lines(4)), 3)
        self.assertIn("DispatchKey(0,0,0,4", monkey_key_lines(4)[0])

    def test_inject_guide_covers_hyperos(self) -> None:
        guide = guide_for("SecurityException: INJECT_EVENTS permission")
        self.assertIn("monkey", guide)
        self.assertIn("安全设置", guide)


class InjectionFallbackTest(unittest.TestCase):
    def test_input_blocked_falls_back_to_monkey(self) -> None:
        bridge = FakeBridge(inject_blocked=True)
        bridge.tap(540, 1800)
        self.assertEqual(bridge.inject_backend, "monkey")
        self.assertTrue(any("monkey" in " ".join(cmd) for cmd in bridge.commands))
        self.assertTrue(any(cmd[:1] == ["push"] for cmd in bridge.commands))
        bridge.swipe(600, 2000, 600, 700)
        bridge.key(4)
        self.assertEqual(bridge.inject_backend, "monkey")
        self.assertTrue(all(marker in "INJECT_EVENTS permission"
                            for marker in INJECT_MARKERS[:1]))

    def test_empty_monkey_script_for_self_check(self) -> None:
        bridge = FakeBridge(inject_blocked=True)
        bridge._run_monkey(["UserWait(1)"])
        self.assertTrue(any("monkey -f" in " ".join(cmd) for cmd in bridge.commands))

    def test_ui_dump_and_foreground_guard(self) -> None:
        bridge = FakeBridge()
        bridge.ui_xml = share_xml().encode()
        self.assertEqual(detect_page(bridge.ui_nodes()), "share")
        self.assertEqual(bridge.current_package(), "com.huawei.health")
        bridge.ensure_foreground()  # 不抛异常
        with self.assertRaises(PhoneError):
            bridge.ensure_foreground(("com.other.app",))


class AutoCollectorTest(unittest.TestCase):
    def _run(self, bridge: FakeAutoBridge, limit: int = 3):
        events: list[str] = []
        collector = AutoCollector(bridge, limit=limit, max_scrolls=2,
                                  on_event=lambda kind, text, path: events.append(text),
                                  sleep=lambda _: None)
        return collector.run(), events, collector

    def test_full_loop_saves_each_record_once(self) -> None:
        bridge = FakeAutoBridge()
        paths, events, collector = self._run(bridge)
        self.assertEqual(collector.saved, 3)
        self.assertEqual(len(paths), 3, "三条不同记录各拉一张，不重复")
        self.assertEqual(len({p.read_bytes() for p in paths}), 3)
        self.assertEqual(bridge.save_count, 3)
        self.assertTrue(any("返回" in text for text in events),
                        "保存后确实通过页面「返回」按钮回到历史列表")

    def test_duplicate_image_is_dropped(self) -> None:
        bridge = FakeAutoBridge(duplicate=True)
        paths, events, collector = self._run(bridge)
        self.assertEqual(collector.saved, 3)
        self.assertEqual(len(paths), 1, "内容哈希去重：重复保存的报告只留一张")
        self.assertTrue(any("重复" in text for text in events),
                        "重复图片被明确记录为跳过")

    def test_never_taps_a_foreign_app(self) -> None:
        bridge = FakeAutoBridge(package="com.cmb.pb")
        with self.assertRaises(PhoneError) as ctx:
            self._run(bridge)
        self.assertIn("不在华为运动健康", str(ctx.exception))
        self.assertEqual(bridge.taps, [], "前台不是华为运动健康时一次都不点击")

    def test_starts_from_detail_and_normalizes_to_history(self) -> None:
        bridge = FakeAutoBridge()
        bridge.state = "detail"
        paths, events, _ = self._run(bridge, limit=2)
        self.assertEqual(len(paths), 2)
        self.assertTrue(any("先回到体重历史列表" in text for text in events))


class WeightOnlyTest(unittest.TestCase):
    """只有体重、没有体脂的普通称重：真机快照 + 变体文案都要认得出来。"""

    def test_real_history_snapshot_marks_every_row(self) -> None:
        rows = history_rows(real_nodes("phone_history_real.xml"))
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["bodyfat"] for row in rows],
                         [False, True, True, False, False, False],
                         "9/25、9/19、9/18 两条是普通称重；9/24、9/22 有体脂")
        self.assertIn("28.1%", rows[1]["label"])
        self.assertNotIn("%", rows[0]["label"])

    def test_real_detail_snapshot(self) -> None:
        weight_only = real_nodes("phone_detail_weightonly_real.xml")
        bodyfat = real_nodes("phone_detail_bodyfat_real.xml")
        self.assertTrue(is_weight_only_detail(weight_only))
        self.assertFalse(is_weight_only_detail(bodyfat),
                         "有「分享」+ 脂肪率/骨骼肌量的详情页绝不能被跳过")
        self.assertEqual(detect_page(weight_only), "detail")
        self.assertEqual(detect_page(bodyfat), "detail")

    def test_incomplete_row_is_not_guessed(self) -> None:
        nodes = [UiNode("86.2", "", "com.huawei.health:id/child_item_weight",
                        (208, 576, 314, 646), False)]
        self.assertIsNone(row_bodyfat_state(nodes, (208, 511, 1202, 719)),
                          "半屏行 / 控件没加载时返回 None，交给详情页兜底")
        nodes.append(UiNode("14:04", "", "com.huawei.health:id/child_item_time",
                            (1036, 584, 1150, 646), False))
        self.assertFalse(row_bodyfat_state(nodes, (208, 511, 1202, 719)))

    def test_detail_without_tip_text_still_detected(self) -> None:
        base = [
            _node("测量记录"),
            _node("86.2", rid="com.huawei.health:id/level_value_text"),
            _node("BMI", rid="com.huawei.health:id/body_index_recycle_title"),
            _node(desc="返回", rid="com.huawei.health:id/hwappbarpattern_cancel_icon",
                  bounds="[39,165][195,321]", clickable="true"),
            _node(desc="更多选项", rid="com.huawei.health:id/hwappbarpattern_ok_icon",
                  bounds="[1085,165][1241,321]", clickable="true"),
        ]
        self.assertTrue(is_weight_only_detail(parse_ui_xml(_xml(*base))),
                        "没有横幅文案时，靠「无分享 + 只有 BMI + 有体重」也能判定")
        with_share = base + [_node(desc="分享",
                                   rid="com.huawei.health:id/hwappbarpattern_menu_icon",
                                   bounds="[929,165][1085,321]", clickable="true")]
        self.assertFalse(is_weight_only_detail(parse_ui_xml(_xml(*with_share))),
                         "有「分享」入口就不能判成普通称重")


class ShareFailsWeightOnlyBridge(FakeAutoBridge):
    """复现真机故障：点了「分享」没反应，再读控件树才发现是无体脂记录。"""

    def __init__(self) -> None:
        super().__init__(weights=("86.6",), bodyfat_flags=(True,))
        self.state = "detail"
        self.weight_only_current = False
        self._flipped = False

    def ui_nodes(self) -> list[UiNode]:
        if self.state == "detail" and not self._flipped:
            return parse_ui_xml(_xml(
                _node("测量记录"),
                _node("2026/9/25 14:04"),
                _node("86.2", rid="com.huawei.health:id/level_value_text"),
                _node("脂肪率", rid="com.huawei.health:id/body_index_recycle_title"),
                _node(desc="分享", rid="com.huawei.health:id/hwappbarpattern_menu_icon",
                      bounds="[929,165][1085,321]", clickable="true"),
                _node(desc="返回", rid="com.huawei.health:id/hwappbarpattern_cancel_icon",
                      bounds="[39,165][195,321]", clickable="true"),
            ))
        return super().ui_nodes()

    def tap(self, x: int, y: int) -> None:
        if self.state == "detail" and not self._flipped:
            self._flipped = True
            self.weight_only_current = True
            return
        super().tap(x, y)


class WeightOnlyAutoCollectTest(unittest.TestCase):
    def _run(self, bridge: FakeAutoBridge, limit: int = 5):
        events: list[str] = []
        collector = AutoCollector(bridge, limit=limit, max_scrolls=2,
                                  on_event=lambda kind, text, path: events.append(text),
                                  sleep=lambda _: None)
        return collector.run(), events, collector

    def test_list_rows_are_skipped_without_opening(self) -> None:
        bridge = FakeAutoBridge(
            weights=("86.2", "86.6", "86.8", "87.1"),
            bodyfat_flags=(False, True, True, False))
        paths, events, collector = self._run(bridge)
        self.assertEqual(collector.saved, 2)
        self.assertEqual(collector.skipped, 2)
        self.assertEqual(len(paths), 2)
        self.assertEqual(len([t for t in bridge.taps if t[0] == "history"]), 2,
                         "普通称重在列表页就跳过，一次都不打开详情页")
        self.assertTrue(any("只有体重" in text for text in events))
        self.assertTrue(any("跳过 2 条" in text for text in events),
                        "完成提示里单独报跳过数")

    def test_weight_only_detail_returns_to_history(self) -> None:
        bridge = FakeAutoBridge(start_weight_only=True)
        events: list[str] = []
        collector = AutoCollector(
            bridge, limit=1,
            on_event=lambda kind, text, path: events.append(text),
            sleep=lambda _: None)
        collector._handle_detail(bridge.ui_nodes())
        self.assertEqual(collector.skipped, 1)
        self.assertEqual(collector.saved, 0)
        self.assertEqual(bridge.state, "history", "点页面「返回」正常回到列表")
        self.assertEqual(bridge.taps, [("detail", 117, 243)],
                         "只点了「返回」，没有去点不存在的「分享」")
        self.assertTrue(any("只有体重" in text for text in events))

    def test_starting_from_weight_only_detail_continues(self) -> None:
        bridge = FakeAutoBridge(weights=("86.2", "86.6"),
                                bodyfat_flags=(False, True),
                                start_weight_only=True)
        paths, events, collector = self._run(bridge, limit=3)
        self.assertEqual(len(paths), 1, "后面那条有体脂的记录仍然正常导出")
        self.assertEqual(collector.saved, 1)
        self.assertGreaterEqual(collector.skipped, 1)
        self.assertTrue(any("先回到体重历史列表" in text for text in events))

    def test_share_failure_skips_instead_of_raising(self) -> None:
        bridge = ShareFailsWeightOnlyBridge()
        events: list[str] = []
        collector = AutoCollector(
            bridge, limit=1,
            on_event=lambda kind, text, path: events.append(text),
            sleep=lambda _: None)
        collector._handle_detail(bridge.ui_nodes())   # 不再抛 PhoneError
        self.assertEqual(collector.skipped, 1)
        self.assertEqual(collector.saved, 0)
        self.assertEqual(bridge.state, "history")
        self.assertTrue(any("跳过" in text for text in events))


if __name__ == "__main__":
    unittest.main()
