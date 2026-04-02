import logging
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango
from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    """送料系统控制面板

    控制接力送料系统：料斗 → 干燥箱 → 中间料管 → 打印头

    硬件配置（根据CAN协议）：
    - 4个电磁阀：料箱(0)、干燥箱(1)、中间料管(2)、回吹(3)
    - 3个耗材检测：干燥箱(X1)、打印头1(X2)、打印头2(X3)
    - 2个超声波传感器：料斗(0)、干燥箱(1)
    - 气压传感器

    CAN通信：
    - 命令发送: 0x10A
    - 状态接收: 0x10B
    """

    def __init__(self, screen, title):
        super().__init__(screen, title)

        # 电磁阀定义 (根据协议)
        self.valves = {
            0: {"name": _("Hopper Valve"), "desc": _("Hopper → Dryer")},
            1: {"name": _("Dryer Valve"), "desc": _("Dryer → Buffer")},
            2: {"name": _("Buffer Valve"), "desc": _("Buffer → Printhead")},
            3: {"name": _("Blowback Valve"), "desc": _("Retract blowback")},
        }

        # 耗材传感器定义 (根据协议)
        self.filament_sensors = {
            0: {"name": _("Dryer Sensor"), "pin": "Dryer Sensor"},      # 干燥箱耗材检测
            1: {"name": _("Printhead 1"), "pin": "Printhead 1"},       # 打印头1耗材检测
            2: {"name": _("Printhead 2"), "pin": "Printhead 2"},       # 打印头2耗材检测
        }

        # 超声波传感器定义 (根据协议)
        self.ultrasonic_sensors = {
            0: {"name": _("Hopper Level"), "empty": 610, "full": 280},
            1: {"name": _("Dryer Level"), "empty": 510, "full": 20},
        }

        # 状态数据
        self.feeder_status = {
            "connected": False,
            "valves": [False, False, False, False],       # 4个电磁阀状态
            "valve_modes": [0, 0, 0, 0],                  # 0=自动, 1=手动
            "filament": [False, False, False],            # 3个耗材传感器
            "ultrasonic_valid": [False, False],           # 超声波有效标志
            "ultrasonic_distance": [0, 0],                # 距离值 mm
            "ultrasonic_level": [0, 0],                   # 余量百分比
            "air_pressure": 0,                            # 气压 kPa
            "air_pressure_valid": False,
            "run_mode": 0,                                # 0=自动模式, 1=手动模式
            "error": None
        }

        # 系统参数 (根据协议)
        self.MIN_PRESSURE = 400  # 最小气压 kPa (0.4 MPa)

        # 创建主布局
        self._build_ui()

        logging.info("Feeder: Panel initialized")

        # 首次获取状态
        GLib.timeout_add_seconds(1, self._initial_status_request)

    def _build_ui(self):
        """构建用户界面"""
        # 使用滚动视图
        scroll = self._gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        main_box.set_hexpand(True)
        main_box.set_vexpand(True)

        # 顶部状态栏：连接状态 | 运行模式 | 气压
        self._create_status_bar(main_box)

        # 流程图示
        self._create_flow_diagram(main_box)

        # 控制按钮
        self._create_control_buttons(main_box)

        # 电磁阀手动控制
        self._create_valve_controls(main_box)

        scroll.add(main_box)
        self.content.add(scroll)

    def _create_status_bar(self, parent):
        """创建顶部状态栏：连接状态 | 运行模式 | 气压"""
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        status_bar.set_halign(Gtk.Align.CENTER)

        # 连接状态
        conn_label = Gtk.Label(label=_("Feeder:"))
        status_bar.pack_start(conn_label, False, False, 0)

        self.labels['conn_text'] = Gtk.Label(label=_("Disconnected"))
        status_bar.pack_start(self.labels['conn_text'], False, False, 5)

        self.labels['conn_indicator'] = Gtk.Label(label="●")
        self.labels['conn_indicator'].get_style_context().add_class("sensor_off")
        status_bar.pack_start(self.labels['conn_indicator'], False, False, 0)

        # 分隔符
        sep1 = Gtk.Label(label="  |  ")
        status_bar.pack_start(sep1, False, False, 0)

        # 运行模式
        self._create_sensor_status_compact(status_bar)

        # 分隔符
        sep2 = Gtk.Label(label="  |  ")
        status_bar.pack_start(sep2, False, False, 0)

        # 气压显示
        self._create_pressure_display_compact(status_bar)

        parent.pack_start(status_bar, False, False, 5)

    def _create_flow_diagram(self, parent):
        """创建送料流程图示"""
        flow_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        flow_box.set_halign(Gtk.Align.FILL)
        flow_box.set_hexpand(True)

        stages = [
            {"name": _("Hopper"), "has_level": True, "level_id": 0},
            {"name": _("Dryer"), "has_level": True, "level_id": 1, "has_sensor": True, "sensor_id": 0},
            {"name": _("Buffer"), "has_sensor": True, "sensor_id": 1},  # HEAD1
            {"name": _("Printhead"), "has_sensor": True, "sensor_id": 2},  # HEAD2
        ]

        for i, stage in enumerate(stages):
            # 阶段框
            frame = self._create_stage_frame(stage, i)
            flow_box.pack_start(frame, True, True, 0)

            # 箭头和阀门状态
            if i < len(stages) - 1:
                arrow_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                arrow_box.set_valign(Gtk.Align.CENTER)

                # 阀门指示器
                self.labels[f'valve_ind_{i}'] = Gtk.Label(label="○")
                self.labels[f'valve_ind_{i}'].get_style_context().add_class("sensor_off")
                arrow_box.pack_start(self.labels[f'valve_ind_{i}'], False, False, 0)

                # 箭头
                arrow = Gtk.Label(label="→")
                arrow.get_style_context().add_class("feeder_arrow")
                arrow_box.pack_start(arrow, False, False, 0)

                flow_box.pack_start(arrow_box, False, False, 5)

        parent.pack_start(flow_box, False, False, 5)

    def _create_stage_frame(self, stage, idx):
        """创建单个阶段框 - 紧凑版"""
        frame = Gtk.Frame()
        frame.set_label(stage["name"])
        frame.set_label_align(0.5, 0.5)  # 标题居中
        frame.get_style_context().add_class("feeder_stage_frame")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        box.set_margin_start(5)
        box.set_margin_end(5)
        box.set_vexpand(True)

        # 余量显示 (超声波传感器) - 顶部
        if stage.get("has_level"):
            level_id = stage["level_id"]
            self.labels[f'level_bar_{level_id}'] = Gtk.ProgressBar()
            self.labels[f'level_bar_{level_id}'].set_fraction(0)
            self.labels[f'level_bar_{level_id}'].set_size_request(60, 12)
            self.labels[f'level_bar_{level_id}'].set_show_text(True)
            self.labels[f'level_bar_{level_id}'].set_text("--")
            box.pack_start(self.labels[f'level_bar_{level_id}'], False, False, 0)

        # 耗材传感器状态 - 底部
        if stage.get("has_sensor"):
            sensor_id = stage["sensor_id"]
            self.labels[f'filament_ind_{sensor_id}'] = Gtk.Label(label="●")
            self.labels[f'filament_ind_{sensor_id}'].get_style_context().add_class("sensor_off")
            box.pack_end(self.labels[f'filament_ind_{sensor_id}'], False, False, 0)

        frame.add(box)
        return frame

    def _create_sensor_status(self, parent):
        """创建传感器状态显示（原版，未使用）"""
        pass

    def _create_sensor_status_compact(self, parent):
        """创建运行模式显示"""
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        mode_label = Gtk.Label(label=_("Run Mode:"))
        mode_box.pack_start(mode_label, False, False, 0)

        self.labels['run_mode_value'] = Gtk.Label(label=_("Auto"))
        self.labels['run_mode_value'].get_style_context().add_class("feeder_mode_value")
        mode_box.pack_start(self.labels['run_mode_value'], False, False, 0)

        self.labels['run_mode_indicator'] = Gtk.Label(label="●")
        self.labels['run_mode_indicator'].get_style_context().add_class("sensor_on")
        mode_box.pack_start(self.labels['run_mode_indicator'], False, False, 0)

        parent.pack_start(mode_box, False, False, 0)

    def _create_pressure_display(self, parent):
        """创建气压显示（原版，未使用）"""
        pass

    def _create_pressure_display_compact(self, parent):
        """创建紧凑型气压显示"""
        pressure_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)

        pressure_label = Gtk.Label(label=_("Pressure:"))
        pressure_box.pack_start(pressure_label, False, False, 0)

        self.labels['pressure_value'] = Gtk.Label(label="--")
        self.labels['pressure_value'].get_style_context().add_class("feeder_pressure_value")
        pressure_box.pack_start(self.labels['pressure_value'], False, False, 0)

        pressure_unit = Gtk.Label(label="MPa")
        pressure_box.pack_start(pressure_unit, False, False, 0)

        self.labels['pressure_status'] = Gtk.Label(label="●")
        self.labels['pressure_status'].get_style_context().add_class("sensor_off")
        pressure_box.pack_start(self.labels['pressure_status'], False, False, 0)

        parent.pack_start(pressure_box, False, False, 0)

    def _create_control_buttons(self, parent):
        """创建主控制按钮"""
        button_grid = self._gtk.HomogeneousGrid()

        # 自动送料
        self.labels['btn_auto_feed'] = self._gtk.Button("arrow-down", _("Auto Feed"), "color3")
        self.labels['btn_auto_feed'].connect("clicked", self._on_auto_feed)
        button_grid.attach(self.labels['btn_auto_feed'], 0, 0, 1, 1)

        # 回退
        self.labels['btn_retract'] = self._gtk.Button("arrow-up", _("Retract"), "color2")
        self.labels['btn_retract'].connect("clicked", self._on_retract)
        button_grid.attach(self.labels['btn_retract'], 1, 0, 1, 1)

        # 刷新状态
        self.labels['btn_refresh'] = self._gtk.Button("refresh", _("Refresh"), "color4")
        self.labels['btn_refresh'].connect("clicked", self._on_refresh)
        button_grid.attach(self.labels['btn_refresh'], 2, 0, 1, 1)

        # 急停
        self.labels['btn_stop'] = self._gtk.Button("cancel", _("Stop All"), "color1")
        self.labels['btn_stop'].connect("clicked", self._on_stop)
        button_grid.attach(self.labels['btn_stop'], 3, 0, 1, 1)

        parent.pack_start(button_grid, False, False, 5)

    def _create_valve_controls(self, parent):
        """创建电磁阀手动控制 - 紧凑版"""
        valve_grid = self._gtk.HomogeneousGrid()

        valve_labels = [_("To Dryer"), _("To Buffer"), _("To Printhead"), _("Return Buffer")]  # 简短标签

        for valve_id, valve in self.valves.items():
            # 状态指示 + 按钮组合
            self.labels[f'valve_status_{valve_id}'] = Gtk.Label(label="○")
            self.labels[f'valve_status_{valve_id}'].get_style_context().add_class("sensor_off")

            btn = self._gtk.Button(None, valve_labels[valve_id], "color3")
            btn.connect("clicked", self._on_valve_toggle, valve_id)
            self.labels[f'valve_btn_{valve_id}'] = btn

            valve_grid.attach(btn, valve_id, 0, 1, 1)

        parent.pack_start(valve_grid, False, False, 2)

    # ============ 控制回调函数 ============

    def _on_auto_feed(self, widget):
        """自动送料"""
        logging.info("Auto feed triggered")
        # 按顺序打开电磁阀: 料箱→干燥箱→中间料管
        self._send_valve_command(0, 0x01, mode=0x00)  # 打开料箱阀，自动模式
        self._screen.show_popup_message(_("Auto feed started"), level=1)

    def _on_retract(self, widget):
        """回退（吹回）"""
        logging.info("Retract triggered")
        self._send_valve_command(3, 0x01)  # 打开回吹阀
        self._screen.show_popup_message(_("Retract started"), level=1)

    def _on_refresh(self, widget):
        """刷新状态"""
        logging.info("Refresh status")
        self._initial_status_request()

    def _on_stop(self, widget):
        """停止所有操作"""
        logging.info("Stop all triggered")
        # 关闭所有电磁阀
        for valve_id in range(4):
            self._send_valve_command(valve_id, 0x00)
        self._screen.show_popup_message(_("All valves closed"), level=1)

    def _on_valve_toggle(self, widget, valve_id):
        """手动切换电磁阀状态"""
        logging.info(f"Manual toggle valve {valve_id}")
        self._send_valve_command(valve_id, 0x06)  # 0x06 = 手动切换

    # ============ 通信函数 ============

    def _send_valve_command(self, valve_id, action, mode=0x01):
        """发送电磁阀控制命令

        命令格式: [0x20, 阀门ID, 动作, 模式]
        动作: 0x00=关闭, 0x01=打开, 0x02=切换, 0x04=手动开, 0x05=手动关
        模式: 0x00=自动, 0x01=手动
        """
        cmd_data = [0x20, valve_id, action, mode]
        self._send_can_command(cmd_data)

    def _send_can_command(self, data):
        """发送CAN命令 - 使用REST API"""
        try:
            result = self._screen.apiclient.post_request(
                "machine/feeder/command",
                json={"data": data}
            )
            if result and "status" in result.get("result", {}):
                self._update_status_from_result(result["result"]["status"])
        except Exception as e:
            logging.error(f"Failed to send command: {e}")

    def _initial_status_request(self):
        """首次获取状态 - 使用REST API"""
        try:
            logging.info("Feeder: requesting initial status...")
            result = self._screen.apiclient.send_request("machine/feeder/status")
            if result and "result" in result:
                status = result["result"].get("status", {})
                logging.info(f"Feeder: initial status - connected={status.get('connected')}, pressure={status.get('pressure')}")
                self._update_status_from_result(status)
                GLib.idle_add(self._update_ui)
        except Exception as e:
            logging.error(f"Failed to get initial status: {e}")
        return False  # 不重复执行

    def _update_status_from_result(self, status):
        """从API结果更新状态"""
        if not status:
            return

        self.feeder_status["connected"] = status.get("connected", False)

        if "valves" in status:
            self.feeder_status["valves"] = status["valves"]
        if "filament" in status:
            self.feeder_status["filament"] = status["filament"]
        if "pressure" in status:
            self.feeder_status["air_pressure"] = status["pressure"]
        if "pressure_valid" in status:
            self.feeder_status["air_pressure_valid"] = status["pressure_valid"]
        if "ultrasonic" in status:
            self.feeder_status["ultrasonic_level"] = status["ultrasonic"]
        if "ultrasonic_valid" in status:
            self.feeder_status["ultrasonic_valid"] = status["ultrasonic_valid"]
        if "run_mode" in status:
            self.feeder_status["run_mode"] = status["run_mode"]

        logging.debug(f"Feeder status updated: {self.feeder_status}")

    def _update_ui(self):
        """更新UI显示"""
        logging.info(f"Feeder: _update_ui called, connected={self.feeder_status.get('connected')}, pressure={self.feeder_status.get('air_pressure')}")
        status = self.feeder_status

        # 更新连接状态
        conn_ctx = self.labels['conn_indicator'].get_style_context()
        conn_ctx.remove_class("sensor_on")
        conn_ctx.remove_class("sensor_off")
        if status["connected"]:
            conn_ctx.add_class("sensor_on")
            self.labels['conn_text'].set_text(_("Connected"))
        else:
            conn_ctx.add_class("sensor_off")
            self.labels['conn_text'].set_text(_("Disconnected"))

        # 更新电磁阀状态
        for i, valve_open in enumerate(status["valves"]):
            # 阀门指示器 (流程图中)
            if i < 3 and f'valve_ind_{i}' in self.labels:
                ind_ctx = self.labels[f'valve_ind_{i}'].get_style_context()
                ind_ctx.remove_class("sensor_on")
                ind_ctx.remove_class("sensor_off")
                if valve_open:
                    ind_ctx.add_class("sensor_on")
                    self.labels[f'valve_ind_{i}'].set_text("●")
                else:
                    ind_ctx.add_class("sensor_off")
                    self.labels[f'valve_ind_{i}'].set_text("○")

            # 阀门状态 (手动控制区)
            status_key = f'valve_status_{i}'
            if status_key in self.labels:
                ctx = self.labels[status_key].get_style_context()
                ctx.remove_class("sensor_on")
                ctx.remove_class("sensor_off")
                if valve_open:
                    ctx.add_class("sensor_on")
                    self.labels[status_key].set_text("●")
                else:
                    ctx.add_class("sensor_off")
                    self.labels[status_key].set_text("○")

        # 更新运行模式显示
        run_mode = status.get("run_mode", 0)
        if 'run_mode_value' in self.labels:
            if run_mode == 0:
                self.labels['run_mode_value'].set_text(_("Auto"))
            else:
                self.labels['run_mode_value'].set_text(_("Manual"))

        if 'run_mode_indicator' in self.labels:
            mode_ctx = self.labels['run_mode_indicator'].get_style_context()
            mode_ctx.remove_class("sensor_on")
            mode_ctx.remove_class("sensor_warning")
            if run_mode == 0:
                mode_ctx.add_class("sensor_on")  # 自动模式 - 绿色
            else:
                mode_ctx.add_class("sensor_warning")  # 手动模式 - 黄色

        # 更新耗材传感器状态 (流程图中的指示器)
        for i, detected in enumerate(status["filament"]):
            # 流程图中的指示器
            if i == 0:
                ind_key = 'filament_ind_0'  # Dryer
            elif i == 1:
                ind_key = 'filament_ind_1'  # Buffer (HEAD1)
            elif i == 2:
                ind_key = 'filament_ind_2'  # Printhead (HEAD2)
            else:
                ind_key = None

            if ind_key and ind_key in self.labels:
                ind_ctx = self.labels[ind_key].get_style_context()
                ind_ctx.remove_class("sensor_on")
                ind_ctx.remove_class("sensor_off")
                ind_ctx.remove_class("sensor_warning")
                if detected:
                    ind_ctx.add_class("sensor_on")  # 有料 - 绿色
                else:
                    ind_ctx.add_class("sensor_warning")  # 无料 - 黄色

        # 更新超声波传感器 (余量)
        for i, level in enumerate(status["ultrasonic_level"]):
            bar_key = f'level_bar_{i}'
            if bar_key in self.labels:
                valid = status["ultrasonic_valid"][i] if i < len(status["ultrasonic_valid"]) else True
                if valid and level != 0xFF:
                    self.labels[bar_key].set_fraction(level / 100.0)
                    self.labels[bar_key].set_text(f"{level}%")
                else:
                    self.labels[bar_key].set_fraction(0)
                    self.labels[bar_key].set_text("--")

        # 更新气压显示
        pressure = status["air_pressure"]
        pressure_mpa = pressure / 1000.0  # kPa -> MPa
        self.labels['pressure_value'].set_text(f"{pressure_mpa:.2f}")

        pressure_ctx = self.labels['pressure_status'].get_style_context()
        pressure_ctx.remove_class("sensor_on")
        pressure_ctx.remove_class("sensor_off")
        pressure_ctx.remove_class("sensor_warning")
        if pressure >= self.MIN_PRESSURE:
            pressure_ctx.add_class("sensor_on")  # 绿色
        else:
            pressure_ctx.add_class("sensor_warning")  # 黄色

        # 更新按钮可用性
        connected = status["connected"]
        pressure_ok = pressure >= self.MIN_PRESSURE

        self.labels['btn_auto_feed'].set_sensitive(connected and pressure_ok)
        self.labels['btn_retract'].set_sensitive(connected and pressure_ok)
        for i in range(4):
            btn_key = f'valve_btn_{i}'
            if btn_key in self.labels:
                self.labels[btn_key].set_sensitive(connected and pressure_ok)

    def activate(self):
        """面板激活"""
        self._initial_status_request()

    def deactivate(self):
        """面板停用"""
        # 取消订阅 (可选，面板关闭时自动清理)
        pass

    def process_update(self, action, data):
        """处理Moonraker推送的状态更新"""
        # 处理 feeder_manager 状态更新通知 (notify_feeder_status_changed)
        if action == "notify_feeder_status_changed":
            logging.info(f"Feeder: received status update via websocket: {data}")
            if isinstance(data, dict):
                self._update_status_from_result(data)
                GLib.idle_add(self._update_ui)
