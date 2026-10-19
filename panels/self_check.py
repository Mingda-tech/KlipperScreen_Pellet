import logging
import os
import gi
import pathlib
# import bed_mesh
import netifaces
import threading
import time
import shutil

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

from ks_includes.screen_panel import ScreenPanel

class Panel(ScreenPanel):
    initialized = False
    FEEDER_POLL_INTERVAL = 5
    FEEDER_STATUS_TIMEOUT = 15

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.themedir = os.path.join(pathlib.Path(__file__).parent.resolve().parent, "styles", screen.theme, "images")
        self.image_pass = os.path.join(self.themedir, "check_pass.svg")
        self.image_fail = os.path.join(self.themedir, "check_fail.svg")
        self.image_loading = os.path.join(self.themedir, "loading.gif")
        self.images = []

        self.network_interfaces = netifaces.interfaces()
        self.wireless_interfaces = [iface for iface in self.network_interfaces if iface.startswith('wl')]
        self.wifi = None
        self.use_network_manager = os.system('systemctl is-active --quiet NetworkManager.service') == 0
        if len(self.wireless_interfaces) > 0:
            logging.info(f"Found wireless interfaces: {self.wireless_interfaces}")
            if self.use_network_manager:
                logging.info("Using NetworkManager")
                from ks_includes.wifi_nm import WifiManager
            else:
                logging.info("Using wpa_cli")
                from ks_includes.wifi import WifiManager
            self.wifi = WifiManager(self.wireless_interfaces[0])

        self.test_items = ["Nozzle Heating", "Hot Bed Heating", "Nozzle Cooling Fan", "Hotend Cooling Fan", "Filament Detection", "Auto Leveling", "Camera", "WiFi"]
        self.steps = [x for x in range(len(self.test_items))]

        grid = self._gtk.HomogeneousGrid()
        grid.set_row_homogeneous(False)
        self.labels['warning'] = Gtk.Label()
        message = _("Do not touch the printer during self-check.")
        self.labels['warning'].set_markup(f'<span foreground="red">{message}</span>')
        grid.attach(self.labels['warning'], 0, 0, 4, 1)
        for i, text in enumerate(self.test_items):
            self.labels[text] = 0
            i = i + 1
            grid.attach(Gtk.Label(), 0, i, 1, 1)
            label = Gtk.Label(label=_(text))
            label.set_vexpand(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_line_wrap(True)            
            grid.attach(label, 0, i, 2, 1)
            image = Gtk.Image()
            image.set_from_file(self.image_loading)
            self.images.append(image)
            grid.attach(image, 2, i, 2, 1)
        
        self.labels['confirm'] = self._gtk.Button(None, _("Confirm"), "color1")
        self.labels['confirm'].connect("clicked", self.confirm_action)
        grid.attach(self.labels['confirm'], 0, len(self.test_items)+1, 4, 1)

        for i in range(len(self.test_items)):
            self.change_state(i, 1)

        self.is_check = True
        self.tool_target = 50
        self.bed_target = 40
        self.fan_speed = 35
        self.fans = self._printer.get_fans()
        self.start_time = time.monotonic()
        self.time_out = 60     #seconds
        self.check_timeout = None
        self.feeder_status = {}
        self.feeder_connected_at = None
        self.feeder_updated_at = None
        self.feeder_request = None
        self.feeder_last_request = None
        self.feeder_active = False
        self.feeder_revision = 0
        self.feeder_field_revisions = {"connected": 0, "filament": 0}
        #Nozzle Heating
        for extruder in self._printer.get_tools():
            temp = self._printer.get_dev_stat(extruder, "temperature")
            if temp < self.tool_target:
                self._screen._ws.klippy.set_tool_temp(self._printer.get_tool_number(extruder), self.tool_target)

        #Bed Headting
        for dev in self._printer.get_heaters():
            if dev == "heater_bed":
                temp = self._printer.get_dev_stat("heater_bed", "temperature")
                if temp < self.bed_target:
                    self.bed_target = temp + 5        
                    self._screen._ws.klippy.set_bed_temp(self.bed_target)  

        #Nozzle Cooling Fan
        for fan in self.fans:
            if fan == "fan":
                speed = self._printer.get_fan_speed(fan) * 100
                if speed < self.fan_speed:
                    self._screen._ws.klippy.gcode_script(f"M106 S{self.fan_speed * 2.55:.0f}")                           

        self.content.add(grid)        

    def remove_all_dialog(self):
        self.close_screensaver()
        for dialog in self.screen.dialogs:
            self.remove_dialog(dialog)
    
    def change_state(self, step, state):
        if not 0 <= step < len(self.images):
            return
        imagePath = self.image_loading
        if state == -1:
            imagePath = self.image_fail
        elif state == 0:
            imagePath = self.image_pass
        self.images[step].set_from_file(imagePath)

    def activate(self):
        if not self.feeder_active:
            self.feeder_active = True
            self.feeder_status = {}
            self.feeder_connected_at = None
            self.feeder_updated_at = None
            self.feeder_last_request = None
        self._request_feeder_status()
        self.self_test()
        if self.check_timeout is None:
            self.check_timeout = GLib.timeout_add_seconds(1, self._check_timeout)

    def deactivate(self):
        self.feeder_active = False
        self.feeder_request = None
        self.feeder_status = {}
        self.feeder_connected_at = None
        self.feeder_updated_at = None
        if self.check_timeout is not None:
            GLib.source_remove(self.check_timeout)
            self.check_timeout = None

    def _check_timeout(self):
        self._request_feeder_status()
        self.self_test()
        return True

    def _request_feeder_status(self):
        now = time.monotonic()
        if not self.feeder_active or self.feeder_request is not None:
            return
        if self.feeder_last_request is not None and now - self.feeder_last_request < self.FEEDER_POLL_INTERVAL:
            return
        token = object()
        revision = self.feeder_revision
        client = self._screen.apiclient
        self.feeder_request = token
        self.feeder_last_request = now

        def request_status():
            try:
                response = client.send_request("machine/feeder/status")
            except Exception:
                logging.exception("Self-check: unable to read feeder status")
                response = False
            GLib.idle_add(self._feeder_status_response, token, revision, response)

        try:
            threading.Thread(target=request_status, daemon=True).start()
        except RuntimeError:
            logging.exception("Self-check: unable to start feeder status request")
            self._feeder_status_response(token, revision, False)

    def _feeder_status_response(self, token, revision, response):
        if not self.feeder_active or self.feeder_request is not token:
            return False
        self.feeder_request = None
        result = response.get("result") if isinstance(response, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        if isinstance(status, dict) and "connected" in status and "filament" in status:
            # Preserve newer notification fields while still filling gaps from the GET snapshot.
            update = {
                key: status[key] for key in ("connected", "filament")
                if self.feeder_field_revisions[key] <= revision
            }
            if not self._is_feeder_flag(status["connected"]) or not status["connected"]:
                update.pop("filament", None)
            self._update_feeder_status(update)
        else:
            # A transient HTTP failure must not erase a recent valid notification.
            logging.warning("Self-check: feeder status unavailable or incomplete")
        self.self_test()
        return False

    @staticmethod
    def _is_feeder_flag(value):
        return type(value) in (bool, int) and value in (0, 1)

    def _update_feeder_status(self, status):
        if not isinstance(status, dict) or not any(key in status for key in ("connected", "filament")):
            return
        now = time.monotonic()
        self.feeder_revision += 1
        if "connected" in status:
            connected = status["connected"]
            self.feeder_field_revisions["connected"] = self.feeder_revision
            self.feeder_connected_at = now
            self.feeder_status["connected"] = self._is_feeder_flag(connected) and bool(connected)
            if not self.feeder_status["connected"]:
                self.feeder_status.pop("filament", None)
                self.feeder_updated_at = None
                self.feeder_field_revisions["filament"] = self.feeder_revision
        if "filament" in status:
            self.feeder_field_revisions["filament"] = self.feeder_revision
            filament = status["filament"]
            valid = (
                self.feeder_status.get("connected") is not False
                and isinstance(filament, (list, tuple)) and len(filament) == 3
                and all(self._is_feeder_flag(value) for value in filament)
            )
            if valid:
                # Keep the three CAN protocol positions, including machines without a hopper.
                self.feeder_status["filament"] = tuple(bool(value) for value in filament)
                self.feeder_updated_at = now
            else:
                self.feeder_status.pop("filament", None)
                self.feeder_updated_at = None
        logging.debug("Self-check: feeder status %s", self.feeder_status)

    def _feeder_has_material(self):
        if self.feeder_status.get("connected") is not True:
            return False
        now = time.monotonic()
        if any(timestamp is None or now - timestamp > self.FEEDER_STATUS_TIMEOUT
               for timestamp in (self.feeder_connected_at, self.feeder_updated_at)):
            return False
        filament = self.feeder_status.get("filament", ())
        # HEAD2 is the printhead sensor; upstream material levels do not gate this check.
        return len(filament) == 3 and filament[2] is True

    def self_test(self):
        timed_out = time.monotonic() - self.start_time >= self.time_out
        for step in self.steps[:]:
            is_ok = False
            if step == 0:
                for extruder in self._printer.get_tools():
                    is_ok = True
                    temp = self._printer.get_dev_stat(extruder, "temperature")
                    if temp < self.tool_target:
                        is_ok = False
                        break
            elif step == 1:
                for dev in self._printer.get_heaters():
                    is_ok = True
                    if dev == "heater_bed":
                        temp = self._printer.get_dev_stat("heater_bed", "temperature")
                        if temp < self.bed_target:
                            is_ok = False
                            break
            elif step == 2:
                for fan in self.fans:
                    is_ok = True
                    if fan == "fan":
                        speed = self._printer.get_fan_speed("fan") * 100
                        if speed < self.fan_speed-5:
                            is_ok = False
                            break
            elif step == 3:
                for fan in self.fans:
                    is_ok = True
                    if 'hotend' in fan.lower():
                        speed = self._printer.get_fan_speed(fan) * 100                        
                        if speed < self.fan_speed-5:
                            is_ok = False
                            break
            elif step == 4:
                is_ok = self._feeder_has_material()
                # Material presence can change after a pass; keep checking it until the panel closes.
                state = 0 if is_ok else (-1 if timed_out else 1)
                GLib.idle_add(self.change_state, step, state)
                continue
            elif step == 5:
                is_ok = False
                bm = self._printer.get_stat("bed_mesh")
                if bm is not None and self._printer.get_stat("bed_mesh", "profile_name") != '':
                    is_ok = True
                                            
            elif step == 6:
                for i, cam in enumerate(self._printer.cameras):
                    is_ok = os.path.exists(f"/dev/video{2*i}")
                    if not cam["enabled"]:
                        is_ok = False
                        break
            elif step == 7:
                is_ok = False
                if self.wifi is not None:
                    if self.use_network_manager:
                        # NetworkManager异步方式 - 检查设备连接状态
                        try:
                            if hasattr(self.wifi, 'connected') and self.wifi.connected:
                                is_ok = True
                            elif hasattr(self.wifi, 'wifi_dev') and self.wifi.wifi_dev:
                                # 直接检查NetworkManager设备状态
                                device = self.wifi.wifi_dev.SpecificDevice()
                                if device.ActiveAccessPoint:
                                    is_ok = True
                        except Exception as e:
                            logging.error(f"NetworkManager WiFi check failed: {e}")
                            is_ok = False
                    else:
                        # wpa_cli同步方式
                        connected_ssid = self.wifi.get_connected_ssid()
                        if connected_ssid is not None:
                            is_ok = True

            if is_ok:
                GLib.idle_add(self.change_state, step, 0)
                self.steps.remove(step)

        if timed_out:
            for step in self.steps:
                if step != 4:
                    GLib.idle_add(self.change_state, step, -1)
            self.steps = [step for step in self.steps if step == 4]

    def confirm_action(self, widget):
        self._screen._ws.klippy.gcode_script("TURN_OFF_HEATERS")                           
        self._screen._ws.klippy.gcode_script("M106 S0")                           
        self._screen.show_panel("main_menu", None, remove_all=True, items=self._config.get_menu_items("__main"))
    def process_update(self, action, data):
        if action == "notify_feeder_status_changed":
            self._update_feeder_status(data)
            self.self_test()
        elif action == "notify_status_update":
            self.self_test()
