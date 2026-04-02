import logging
import gi
import os
import subprocess
import urllib.request
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango, GdkPixbuf, GLib
import cairo
from ks_includes.KlippyGcodes import KlippyGcodes
from ks_includes.screen_panel import ScreenPanel


class Panel(ScreenPanel):
    distances = ['0.02', '.1', '1', '10']
    distance = distances[-2]

    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.settings = {}
        self.pos = {}
        self.is_home = False
        self.current_extruder = self._printer.get_stat("toolhead", "extruder")
        self.menu = ['main_menu']
        self.camera_timeout = None
        self.current_cam = None
        self.camera_playing = False

        self.buttons = {
            'x+': self._gtk.Button(None, "X+", "color1"),
            'x-': self._gtk.Button(None, "X-", "color1"),
            'y+': self._gtk.Button(None, "Y+", "color2"),
            'y-': self._gtk.Button(None, "Y-", "color2"),
            'z+': self._gtk.Button(None, "Z+", "color3"),
            'z-': self._gtk.Button(None, "Z-", "color3"),
            'home': self._gtk.Button(None, _("Home"), "color4"),
            'motors_off': self._gtk.Button(None, _("Disable Motors"), "color4"),
        }

        self.buttons['x+'].connect("clicked", self.move, "X", "+")
        self.buttons['x-'].connect("clicked", self.move, "X", "-")
        self.buttons['y+'].connect("clicked", self.move, "Y", "+")
        self.buttons['y-'].connect("clicked", self.move, "Y", "-")
        self.buttons['z+'].connect("clicked", self.move, "Z", "+")
        self.buttons['z-'].connect("clicked", self.move, "Z", "-")

        grid = self._gtk.HomogeneousGrid()
        # limit = 2
        i = 0
        self.extruders = [extruder for extruder in self._printer.get_tools()]
        # for extruder in self._printer.get_tools():
        #     if self._printer.extrudercount > 1:
        #         self.labels[extruder] = self._gtk.Button(None, f"T{self._printer.get_tool_number(extruder)}")
        #         self.labels[extruder].connect("clicked", self.change_extruder, extruder)
        #     else:
        #         self.labels[extruder] = self._gtk.Button(None, "extruder")
        #     if extruder == self.current_extruder:
        #         self.labels[extruder].get_style_context().add_class("button_active")
        #     if i < limit:
        #         grid.attach(self.labels[extruder], i, 0, 1, 1)
        #         i += 1
        grid.attach(self.buttons['x+'], 0, 1, 1, 1)
        grid.attach(self.buttons['x-'], 1, 1, 1, 1)
        grid.attach(self.buttons['y+'], 0, 2, 1, 1)
        grid.attach(self.buttons['y-'], 1, 2, 1, 1)

        distgrid = self._gtk.HomogeneousGrid()
        self.labels['move_dist'] = Gtk.Label(_("Move Distance (mm)"))
        distgrid.attach(self.labels['move_dist'], 0, 0, len(self.distances), 1)            
        for j, i in enumerate(self.distances):
            self.labels[i] = self._gtk.Button(label=i)
            self.labels[i].set_direction(Gtk.TextDirection.LTR)
            self.labels[i].connect("clicked", self.change_distance, i)
            ctx = self.labels[i].get_style_context()
            if (self._screen.lang_ltr and j == 0) or (not self._screen.lang_ltr and j == len(self.distances) - 1):
                ctx.add_class("distbutton_top")
            elif (not self._screen.lang_ltr and j == 0) or (self._screen.lang_ltr and j == len(self.distances) - 1):
                ctx.add_class("distbutton_bottom")
            else:
                ctx.add_class("distbutton")
            if i == self.distance:
                ctx.add_class("distbutton_active")
            distgrid.attach(self.labels[i], j, 1, 1, 1)

        for p in ('pos_x', 'pos_y', 'pos_z'):
            self.labels[p] = Gtk.Label()

        offsetgrid = self._gtk.HomogeneousGrid()
        offsetgrid = Gtk.Grid()
        self.labels['confirm'] = self._gtk.Button(None, _("Confirm Pos"), "color1")
        self.labels['save'] = self._gtk.Button(None, "Save", "color1")

        self.labels['confirm'].connect("clicked", self.confirm_extrude_position)
        self.labels['save'].connect("clicked", self.save_offset)
        offsetgrid.attach(self.labels['confirm'], 0, 0, 1, 1)
        offsetgrid.attach(self.labels['save'], 1, 0, 1, 1)

        # Camera image area - use Frame to prevent expansion
        self.camera_frame = Gtk.Frame()
        self.camera_image = Gtk.Image()
        self.camera_frame.add(self.camera_image)
        self.camera_frame.set_hexpand(False)
        self.camera_frame.set_vexpand(False)

        # Find calicam camera
        for cam in self._printer.cameras:
            if cam["enabled"] and cam["name"] == 'calicam':
                self.current_cam = cam
                logging.debug(f"Found calibration camera: {cam['name']}")
                break

        # Start button for camera - fills the whole left area
        self.labels['start_cam'] = self._gtk.Button(
            image_name="camera", label=_("Start"), style="color1",
            scale=self.bts, position=Gtk.PositionType.LEFT, lines=1
        )
        self.labels['start_cam'].set_hexpand(True)
        self.labels['start_cam'].set_vexpand(True)
        self.labels['start_cam'].connect("clicked", self.play)

        # Use a Stack to switch between button and camera
        self.camera_stack = Gtk.Stack()
        self.camera_stack.add_named(self.labels['start_cam'], "button")
        self.camera_stack.add_named(self.camera_frame, "camera")
        self.camera_stack.set_visible_child_name("button")

        self.labels['main_menu'] = self._gtk.HomogeneousGrid()
        self.labels['main_menu'].attach(self.camera_stack, 0, 0, 3, 6)
        self.labels['main_menu'].attach(grid, 3, 0, 2, 3)
        self.labels['main_menu'].attach(distgrid, 3, 3, 2, 2)
        self.labels['main_menu'].attach(offsetgrid, 3, 5, 2, 1)

        self.content.add(self.labels['main_menu'])
        self.reset_pos()

    def _run_light_on(self):
        """Turn on calibration light if macro exists"""
        if "XY_CALIBRATION_LIGHT_ON" in self._printer.get_gcode_macros():
            self._screen._ws.klippy.gcode_script("XY_CALIBRATION_LIGHT_ON")
            logging.info("Executing XY_CALIBRATION_LIGHT_ON macro")

    def _run_light_off(self):
        """Turn off calibration light if macro exists"""
        if "XY_CALIBRATION_LIGHT_OFF" in self._printer.get_gcode_macros():
            self._screen._ws.klippy.gcode_script("XY_CALIBRATION_LIGHT_OFF")
            logging.info("Executing XY_CALIBRATION_LIGHT_OFF macro")

    def process_update(self, action, data):
        if action != "notify_status_update":
            return
        homed_axes = self._printer.get_stat("toolhead", "homed_axes")
        if homed_axes == "xyz":
            # Use toolhead position (raw coordinates without offsets) instead of gcode_position
            if "toolhead" in data and "position" in data["toolhead"]:
                self.pos['x'] = data['toolhead']['position'][0]
                self.pos['y'] = data['toolhead']['position'][1]
                self.pos['z'] = data['toolhead']['position'][2]  
        else:
            if "x" in homed_axes:
                if "toolhead" in data and "position" in data["toolhead"]:
                    self.pos['x'] = data['toolhead']['position'][0]
            else:
                self.pos['x'] = None
            if "y" in homed_axes:
                if "toolhead" in data and "position" in data["toolhead"]:
                    self.pos['y'] = data['toolhead']['position'][1]
            else:
                self.pos['y'] = None
            if "z" in homed_axes:
                if "toolhead" in data and "position" in data["toolhead"]:
                    self.pos['z'] = data['toolhead']['position'][2]
            else:
                self.pos['z'] = None


    def change_distance(self, widget, distance):
        logging.info(f"### Distance {distance}")
        self.labels[f"{self.distance}"].get_style_context().remove_class("distbutton_active")
        self.labels[f"{distance}"].get_style_context().add_class("distbutton_active")
        self.distance = distance

    def move(self, widget, axis, direction):
        if self._config.get_config()['main'].getboolean(f"invert_{axis.lower()}", False):
            direction = "-" if direction == "+" else "+"

        dist = f"{direction}{self.distance}"
        config_key = "move_speed_z" if axis == "Z" else "move_speed_xy"
        speed = None if self.ks_printer_cfg is None else self.ks_printer_cfg.getint(config_key, None)
        if speed is None:
            speed = self._config.get_config()['main'].getint(config_key, 20)
        speed = 60 * max(1, speed)
        script = f"{KlippyGcodes.MOVE_RELATIVE}\nG0 {axis}{dist} F{speed}"
        self._screen._send_action(widget, "printer.gcode.script", {"script": script})
        if self._printer.get_stat("gcode_move", "absolute_coordinates"):
            self._screen._ws.klippy.gcode_script("G90")

    def add_option(self, boxname, opt_array, opt_name, option):
        name = Gtk.Label()
        name.set_markup(f"<big><b>{option['name']}</b></big>")
        name.set_hexpand(True)
        name.set_vexpand(True)
        name.set_halign(Gtk.Align.START)
        name.set_valign(Gtk.Align.CENTER)
        name.set_line_wrap(True)
        name.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

        dev = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        dev.get_style_context().add_class("frame-item")
        dev.set_hexpand(True)
        dev.set_vexpand(False)
        dev.set_valign(Gtk.Align.CENTER)
        dev.add(name)

        if option['type'] == "binary":
            box = Gtk.Box()
            box.set_vexpand(False)
            switch = Gtk.Switch()
            switch.set_hexpand(False)
            switch.set_vexpand(False)
            switch.set_active(self._config.get_config().getboolean(option['section'], opt_name))
            switch.connect("notify::active", self.switch_config_option, option['section'], opt_name)
            switch.set_property("width-request", round(self._gtk.font_size * 7))
            switch.set_property("height-request", round(self._gtk.font_size * 3.5))
            box.add(switch)
            dev.add(box)
        elif option['type'] == "scale":
            dev.set_orientation(Gtk.Orientation.VERTICAL)
            scale = Gtk.Scale.new_with_range(orientation=Gtk.Orientation.HORIZONTAL,
                                             min=option['range'][0], max=option['range'][1], step=option['step'])
            scale.set_hexpand(True)
            scale.set_value(int(self._config.get_config().get(option['section'], opt_name, fallback=option['value'])))
            scale.set_digits(0)
            scale.connect("button-release-event", self.scale_moved, option['section'], opt_name)
            dev.add(scale)

        opt_array[opt_name] = {
            "name": option['name'],
            "row": dev
        }

        opts = sorted(list(opt_array), key=lambda x: opt_array[x]['name'])
        pos = opts.index(opt_name)

        self.labels[boxname].insert_row(pos)
        self.labels[boxname].attach(opt_array[opt_name]['row'], 0, pos, 1, 1)
        self.labels[boxname].show_all()

    def back(self):
        if self.camera_playing:
            self.stop_camera()
            self._run_light_off()
        if len(self.menu) > 1:
            self.unload_menu()
            return True
        return False   

    def confirm_extrude_position(self, widget):
        if self._printer.extrudercount < 2:
            self._screen.show_popup_message(_("Only one extruder does not require calibration."), level = 2)
            return
        self.current_extruder = self._printer.get_stat("toolhead", "extruder")

        if self._printer.get_tool_number(self.current_extruder) == 0:
            self.pos['lx'] = self.pos['x']
            self.pos['ly'] = self.pos['y']
            self.pos['lz'] = self.pos['z'] 
            self._screen.show_popup_message(f"left extruder pos: ({self.pos['lx']:.2f}, {self.pos['ly']:.2f}, {self.pos['lz']:.2f})", level = 1)
            self.change_extruder(widget, "extruder1")
            self._calculate_position()
        elif self._printer.get_tool_number(self.current_extruder) == 1:
            if self.pos['lx'] is None or self.pos['ly'] is None or self.pos['lz'] is None:
                self._screen.show_popup_message(f"Please confirm left extruder position.", level = 2)
            else:
                self.pos['ox'] = self.pos['lx'] - self.pos['x']
                self.pos['oy'] = self.pos['ly'] - self.pos['y']
                self._screen.show_popup_message(f"Right extruder offset is ({self.pos['ox']:.2f}, {self.pos['oy']:.2f})", level = 1)
                self.labels['save'].set_sensitive(True)                      

    def change_extruder(self, widget, extruder):
        self._screen._send_action(widget, "printer.gcode.script",
                                  {"script": f"T{self._printer.get_tool_number(extruder)}"})
        
    def save_offset(self, widget):      
        if self.pos['ox'] is None or self.pos['oy'] is None:
            self._screen.show_popup_message(_("Need to recalculate the offset value."), level = 2)
            return
        
        try:
            self._screen.klippy_config.set("Variables", "idex_xoffset", f"{self.pos['ox']:.2f}")
            self._screen.klippy_config.set("Variables", "idex_yoffset", f"{self.pos['oy']:.2f}")
            self._screen.klippy_config.set("Variables", "cam_xpos", f"{self.pos['lx']:.2f}")
            self._screen.klippy_config.set("Variables", "cam_ypos", f"{self.pos['ly']:.2f}")
            logging.info(f"xy offset set to x: {self.pos['ox']:.2f} y: {self.pos['oy']:.2f}")
            with open(self._screen.klippy_config_path, 'w') as file:
                self._screen.klippy_config.write(file)
                if self.camera_playing:
                    self.stop_camera()
                    self._run_light_off()
                self.save_config()
                self._screen._menu_go_back()
        except Exception as e:
            logging.error(f"Error writing configuration file in {self._screen.klippy_config_path}:\n{e}")
            self._screen.show_popup_message(_("Error writing configuration"))
            
    def play(self, widget):
        if not self.current_cam:
            self._screen.show_popup_message(_("No calibration camera found."), level=2)
            return

        url = self.get_snapshot_url()
        if not url:
            self._screen.show_popup_message(_("No camera URL available."), level=2)
            return

        if check_web_page_access(url) == False:
            self._screen.show_popup_message(_("Please wait for the camera initialization to complete."), level=1)
            return

        self._run_light_on()

        self.reset_pos()
        if self._printer.get_stat("toolhead", "homed_axes") != "xyz":
            self._screen._ws.klippy.gcode_script("G28")
        current_extruder = self._printer.get_stat("toolhead", "extruder")
        if current_extruder != "extruder":
            self.change_extruder(widget=None, extruder="extruder")
        self._calculate_position()

        # Switch to camera view and start camera
        self.camera_stack.set_visible_child_name("camera")
        self.start_camera()

    def get_snapshot_url(self):
        """Get the snapshot URL for the camera"""
        if not self.current_cam:
            return None
        url = self.current_cam.get('snapshot_url', self.current_cam.get('stream_url', ''))
        if not url:
            return None
        if url.startswith('/'):
            endpoint = self._screen.apiclient.endpoint.split(':')
            url = f"{endpoint[0]}:{endpoint[1]}{url}"
        return url

    def start_camera(self):
        """Start camera snapshot refresh"""
        if self.current_cam:
            self.camera_playing = True
            logging.debug(f"Starting camera: {self.current_cam['name']}")
            self.update_camera_image()

    def stop_camera(self):
        """Stop camera snapshot refresh"""
        if self.camera_timeout:
            GLib.source_remove(self.camera_timeout)
            self.camera_timeout = None
        self.camera_playing = False
        logging.debug("Camera stopped")

    def update_camera_image(self):
        """Fetch and update camera snapshot with XY overlay"""
        if not self.current_cam or not self.camera_playing:
            return False

        url = self.get_snapshot_url()
        if not url:
            return False

        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                data = response.read()

            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()

            if pixbuf:
                # Apply rotation
                rotation = self.current_cam.get('rotation', 0)
                if rotation == 90:
                    pixbuf = pixbuf.rotate_simple(GdkPixbuf.PixbufRotation.CLOCKWISE)
                elif rotation == 180:
                    pixbuf = pixbuf.rotate_simple(GdkPixbuf.PixbufRotation.UPSIDEDOWN)
                elif rotation == 270:
                    pixbuf = pixbuf.rotate_simple(GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE)

                # Apply flip
                if self.current_cam.get('flip_horizontal', False):
                    pixbuf = pixbuf.flip(True)
                if self.current_cam.get('flip_vertical', False):
                    pixbuf = pixbuf.flip(False)

                img_width = pixbuf.get_width()
                img_height = pixbuf.get_height()

                # Scale to fit - Grid is 5 columns, camera is in columns 0-2 (3 columns = 60% width)
                max_width = self._gtk.content_width * 3 // 5
                max_height = self._gtk.content_height

                scale_w = max_width / img_width
                scale_h = max_height / img_height
                # Use max scale to fill the area (may crop some parts)
                scale = max(scale_w, scale_h)

                new_width = int(img_width * scale)
                new_height = int(img_height * scale)

                if new_width > 0 and new_height > 0:
                    pixbuf = pixbuf.scale_simple(new_width, new_height, GdkPixbuf.InterpType.BILINEAR)

                    # Crop to fit the target area if needed
                    if new_width > max_width or new_height > max_height:
                        crop_x = max(0, (new_width - max_width) // 2)
                        crop_y = max(0, (new_height - max_height) // 2)
                        crop_w = min(max_width, new_width)
                        crop_h = min(max_height, new_height)
                        try:
                            pixbuf = pixbuf.new_subpixbuf(crop_x, crop_y, crop_w, crop_h)
                        except Exception as e:
                            logging.warning(f"Crop failed: {e}")

                # Draw XY coordinate overlay AFTER scaling/cropping so it's centered on the displayed frame
                final_width = pixbuf.get_width()
                final_height = pixbuf.get_height()

                from gi.repository import Gdk
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, final_width, final_height)
                cr = cairo.Context(surface)

                # Draw the pixbuf first
                Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
                cr.paint()

                # Draw XY coordinate system - centered on the frame
                center_x = final_width // 2
                center_y = final_height // 2
                axis_length = 200
                line_width = 1
                arrow_size = 10

                # Draw X axis (horizontal line - red)
                cr.set_source_rgba(1, 0, 0, 1)
                cr.set_line_width(line_width)
                cr.move_to(center_x - axis_length, center_y)
                cr.line_to(center_x + axis_length, center_y)
                cr.stroke()

                # Draw X axis arrow
                cr.move_to(center_x + axis_length, center_y)
                cr.line_to(center_x + axis_length - arrow_size, center_y - arrow_size // 2)
                cr.line_to(center_x + axis_length - arrow_size, center_y + arrow_size // 2)
                cr.close_path()
                cr.fill()

                # Draw Y axis (vertical line - green)
                cr.set_source_rgba(0, 1, 0, 1)
                cr.move_to(center_x, center_y - axis_length)
                cr.line_to(center_x, center_y + axis_length)
                cr.stroke()

                # Draw Y axis arrow
                cr.move_to(center_x, center_y - axis_length)
                cr.line_to(center_x - arrow_size // 2, center_y - axis_length + arrow_size)
                cr.line_to(center_x + arrow_size // 2, center_y - axis_length + arrow_size)
                cr.close_path()
                cr.fill()

                # Convert cairo surface back to pixbuf
                pixbuf = Gdk.pixbuf_get_from_surface(surface, 0, 0, final_width, final_height)

                self.camera_image.set_from_pixbuf(pixbuf)

        except Exception as e:
            logging.warning(f"Failed to update camera image: {e}")

        # Schedule next update (100ms = 10 FPS)
        self.camera_timeout = GLib.timeout_add(100, self.update_camera_image)
        return False

    def log(self, loglevel, component, message):
        logging.debug(f'[{loglevel}] {component}: {message}')
        if loglevel == 'error' and 'No Xvideo support found' not in message:
            self._screen.show_popup_message(f'{message}')

    def reset_pos(self):
        self.pos['lx'] = None
        self.pos['ly'] = None
        self.pos['lz'] = None 
        self.pos['rx'] = None
        self.pos['ry'] = None
        self.pos['rz'] = None 
        self.pos['ox'] = None
        self.pos['oy'] = None
        self.labels['save'].set_sensitive(False)

    def _calculate_position(self):
        try:
            x_position = self._screen.klippy_config.getfloat("Variables", "cam_xpos")
            y_position = self._screen.klippy_config.getfloat("Variables", "cam_ypos")
            z_position = self._screen.klippy_config.getfloat("Variables", "cam_zpos")            
        except:
            logging.error("Couldn't get the calibration camera position.")
            return

        logging.info(f"Moving to X:{x_position} Y:{y_position}")
        self._screen._ws.klippy.gcode_script(f'G0 Z{z_position} F3000')
        self._screen._ws.klippy.gcode_script(f'G0 X{x_position} Y{y_position} F3000')
        self.pos['z'] = z_position    
        
    def save_config(self):
        script = {"script": "SAVE_CONFIG"}
        self._screen._confirm_send_action(
            None,
            _("Saved successfully!") + "\n\n" + _("Need reboot, relaunch immediately?"),
            "printer.gcode.script",
            script
        )        

    def activate(self):
        # Reset to button view
        self.camera_stack.set_visible_child_name("button")
        symbolic_link = "/home/mingda/printer_data/config/crowsnest.conf"
        source_file = "/home/mingda/printer_data/config/crowsnest2.conf"
        create_symbolic_link(source_file, symbolic_link)
        os.system('sudo systemctl restart crowsnest.service')
        self._screen.show_popup_message(_("Please wait for the camera's fill light to light up for 5 seconds before clicking 'Start'"), level=2)

    def deactivate(self):
        # Stop camera
        self.stop_camera()
        self._run_light_off()

        symbolic_link = "/home/mingda/printer_data/config/crowsnest.conf"
        source_file = "/home/mingda/printer_data/config/crowsnest1.conf"
        create_symbolic_link(source_file, symbolic_link)
        # os.system('sudo systemctl restart crowsnest.service')
        subprocess.Popen(["sudo", "systemctl", "restart", "crowsnest.service"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def create_symbolic_link(source_path, link_path):
    if os.path.exists(link_path):
        os.remove(link_path)
    try:
        os.symlink(source_path, link_path)
        logging.info(f"Symbolic link created: {link_path} -> {source_path}")
    except OSError as e:
        logging.info(f"Error creating symbolic link: {e}")

def check_web_page_access(url):
    try:
        # Run the curl command to fetch the headers, following redirects
        result = subprocess.run(["curl", "-I", "-L", url], check=True, capture_output=True, text=True, timeout=10)

        # Extract the final HTTP status code (last response when redirects occur)
        lines = [line for line in result.stdout.splitlines() if line.startswith('HTTP/')]
        if lines:
            status_code = lines[-1].split()[1]
        else:
            logging.warning(f"Could not parse HTTP status from curl output")
            return False

        if status_code == "200":
            logging.info(f"The web page at {url} is accessible. Status code: {status_code}")
            return True
        else:
            logging.warning(f"Warning: The web page at {url} returned status code {status_code}")

    except subprocess.CalledProcessError as e:
        logging.error(f"Error: The web page at {url} is not accessible. {e}")
    except subprocess.TimeoutExpired:
        logging.error(f"Error: Timeout occurred while checking the web page at {url}.")        
    return False
