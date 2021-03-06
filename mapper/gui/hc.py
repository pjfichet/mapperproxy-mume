# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Some code borrowed from pymunk's debug drawing functions.


from collections import namedtuple
import logging
import math
try:
	from Queue import Empty as QueueEmpty
except ImportError:
	from queue import Empty as QueueEmpty

import pyglet
from pyglet.window import key
try:
	from speechlight import Speech
except ImportError:
	Speech = None

from .vec2d import Vec2d
from ..config import Config, config_lock
from ..world import DIRECTIONS


FPS = 30
DIRECTIONS_2D = set(DIRECTIONS[:-2])

DIRECTIONS_VEC2D = {
	"north": Vec2d(0, 1),
	"east": Vec2d(1, 0),
	"south": Vec2d(0, -1),
	"west": Vec2d(-1, 0)
}

KEYS = {
	(key.ESCAPE, 0): "reset_zoom",
	(key.LEFT, 0): "adjust_size",
	(key.RIGHT, 0): "adjust_size",
	(key.UP, 0): "adjust_gap",
	(key.DOWN, 0): "adjust_gap",
	(key.F11, 0): "toggle_fullscreen",
	(key.F12, 0): "toggle_blink",
	(key.SPACE, 0): "toggle_continuous_view"
}

TERRAIN_COLORS = {
	"brush": (127, 255, 0, 255),
	"cavern": (153, 50, 204, 255),
	"city": (190, 190, 190, 255),
	"field": (124, 252, 0, 255),
	"deathtrap": (255, 128, 0, 255),
	"forest": (8, 128, 0, 255),
	"highlight": (0, 0, 255, 255),
	"hills": (139, 69, 19, 255),
	"indoors": (186, 85, 211, 255),
	"mountains": (165, 42, 42, 255),
	"rapids": (32, 64, 192, 255),
	"road": (255, 255, 255, 255),
	"shallow": (218, 120, 245, 255),
	"tunnel": (153, 50, 204, 255),
	"underwater": (48, 8, 120, 255),
	"undefined": (24, 16, 32, 255),
	"water": (32, 64, 192, 255)
}

pyglet.options["debug_gl"] = False
logger = logging.getLogger(__name__)


class Color(namedtuple("Color", ["r", "g", "b", "a"])):
	"""Color tuple used by the debug drawing API.
	"""
	__slots__ = ()

	def as_int(self):
		return tuple(int(i) for i in self)

	def as_float(self):
		return tuple(i / 255.0 for i in self)


class Blinker(object):
	def __init__(self, blink_rate, draw_func, args_func):
		logger.debug("Creating blinker with blink rate {}, calling function {}, with {} for arguments.".format(blink_rate, draw_func, args_func))
		self.blink_rate = blink_rate
		self.draw_func = draw_func
		self.args_func = args_func
		self.since = 0
		self.vl = None

	def blink(self, dt):
		self.since += dt
		if self.since >= 1.0 / self.blink_rate:
			if self.vl is None:
				logger.debug("{} blink on. Drawing.".format(self))
				args, kwargs = self.args_func()
				self.vl = self.draw_func(*args, **kwargs)
			else:
				logger.debug("{} blink off. Cleaning upp".format(self))
				self.vl.delete()
				self.vl = None
			self.since = 0

	def delete(self):
		if self.vl is not None:
			self.vl.delete()
			self.vl = None

	def __del__(self):
		self.delete()


class Window(pyglet.window.Window):
	def __init__(self, world):
		self.world = world
		self._gui_queue = world._gui_queue
		self._gui_queue_lock = world._gui_queue_lock
		if Speech is not None:
			self._speech = Speech()
			self.say = self._speech.say
		else:
			self.say = lambda *args, **kwargs: None
			msg = "Speech disabled. Unable to import speechlight. Please download from:\nhttps://github.com/nstockton/speechlight"
			self.message(msg)
			logger.warning(msg)
		self._cfg = {}
		with config_lock:
			cfg = Config()
			if "gui" in cfg:
				self._cfg.update(cfg["gui"])
			else:
				cfg["gui"] = {}
				cfg.save()
			del cfg
		if "fullscreen" not in self._cfg:
			self._cfg["fullscreen"] = False
		terrain_colors = {}
		terrain_colors.update(TERRAIN_COLORS)
		if "terrain_colors" in self._cfg:
			terrain_colors.update(self._cfg["terrain_colors"])
		self._cfg["terrain_colors"] = terrain_colors
		self.continuous_view = True
		self.batch = pyglet.graphics.Batch()
		self.groups = tuple(pyglet.graphics.OrderedGroup(i) for i in range(6))
		self.visible_rooms = {}
		self.visible_exits = {}
		self.blinkers = {}
		self.center_mark = []
		self.highlight = None
		self.current_room = None
		super(Window, self).__init__(caption="MPM", resizable=True, vsync=False, fullscreen=self._cfg["fullscreen"])
		logger.info("Created window {}".format(self))
		pyglet.clock.schedule_interval_soft(self.queue_observer, 1.0 / FPS)
		if self.blink:
			# If blinking was enabled in the cconfig file, resetting self.blink to True will trigger the initial scheduling of the blinker in the clock.
			self.blink = True

	@property
	def size(self):
		"""The size of a drawn room in pixels."""
		try:
			if not 20 <= int(self._cfg["room_size"]) <= 300:
				raise ValueError
		except KeyError:
			self._cfg["room_size"] = 100
		except ValueError:
			logger.warn("Invalid value for room_size in config.json: {}".format(self._cfg["room_size"]))
			self._cfg["room_size"] = 100
		return int(self._cfg["room_size"])

	@size.setter
	def size(self, value):
		value = int(value)
		if value < 20:
			value = 20
		elif value > 300:
			value = 300
		self._cfg["room_size"] = value

	@property
	def size_as_float(self):
		"""The scale of a drawn room."""
		return self.size / 100.0

	@property
	def gap(self):
		try:
			if not 10 <= self._cfg["gap"] <= 100:
				raise ValueError
		except KeyError:
			self._cfg["gap"] = 100
		except ValueError:
			logger.warning("Invalid value for gap in config.json: {}".format(self._cfg["gap"]))
			self._cfg["gap"] = 100
		return int(self._cfg["gap"])

	@gap.setter
	def gap(self, value):
		value = int(value)
		if value < 10:
			value = 10
		elif value > 100:
			value = 100
		self._cfg["gap"] = value

	@property
	def gap_as_float(self):
		return self.gap / 100.0

	@property
	def blink(self):
		return bool(self._cfg.get("blink", True))

	@blink.setter
	def blink(self, value):
		value = bool(value)
		self._cfg["blink"] = value
		if value:
			pyglet.clock.schedule_interval_soft(self.blinker, 1.0 / 20)
			self.enable_current_room_markers()
		else:
			pyglet.clock.unschedule(self.blinker)
			for marker in self.blinkers["current_room_markers"]:
				marker.delete()
			del self.blinkers["current_room_markers"]

	@property
	def blink_rate(self):
		try:
			if not 0 <= int(self._cfg["blink_rate"]) <= 15:
				raise ValueError
		except KeyError:
			self._cfg["blink_rate"] = 2
		except ValueError:
			logger.warning("Invalid value for blink_rate in config.json: {}".format(self._cfg["blink_rate"]))
			self._cfg["blink_rate"] = 2
		return int(self._cfg["blink_rate"])

	@blink_rate.setter
	def blink_rate(self, value):
		value = int(value)
		if value < 0:
			value = 0
		elif value > 15:
			value = 15
		self._cfg["blink_rate"] = value

	@property
	def current_room_mark_radius(self):
		try:
			if not 1 <= int(self._cfg["current_room_mark_radius"]) <= 100:
				raise ValueError
		except KeyError:
			self._cfg["current_room_mark_radius"] = 10
		except ValueError:
			logger.warning("Invalid value for current_room_mark_radius: {}".format(self._cfg["current_room_mark_radius"]))
			self._cfg["current_room_mark_radius"] = 10
		return int(self._cfg["current_room_mark_radius"])

	@property
	def current_room_mark_color(self):
		try:
			return Color(*self._cfg["current_room_mark_color"])
		except KeyError:
			self._cfg["current_room_mark_color"] = (255, 255, 255, 255)
			return Color(*self._cfg["current_room_mark_color"])

	@property
	def terrain_colors(self):
		try:
			return self._cfg["terrain_colors"]
		except KeyError:
			self._cfg["terrain_colors"] = TERRAIN_COLORS
			return self._cfg["terrain_colors"]

	@property
	def cx(self):
		return self.width / 2.0

	@property
	def cy(self):
		return self.height / 2.0

	@property
	def cp(self):
		return Vec2d(self.cx, self.cy)

	@property
	def room_draw_radius(self):
		return (int(math.ceil(self.width / self.size / (1 if self.continuous_view else self.gap_as_float + 1.0) / 2)), int(math.ceil(self.height / self.size / (1 if self.continuous_view else self.gap_as_float + 1.0) / 2)), 1)

	def message(self, text):
		self.say(text)
		self.world.output(text)

	def queue_observer(self, dt):
		with self._gui_queue_lock:
			while not self._gui_queue.empty():
				try:
					event = self._gui_queue.get_nowait()
					if event is None:
						event = ("on_close",)
					self.dispatch_event(event[0], *event[1:])
				except QueueEmpty:
					break

	def blinker(self, dt):
		for _, marker in self.blinkers.items():
			try:
				marker.blink(dt)
			except AttributeError:
				for submarker in marker:
					submarker.blink(dt)

	def on_close(self):
		logger.debug("Closing window {}".format(self))
		with config_lock:
			cfg = Config()
			cfg["gui"].update(self._cfg)
			cfg.save()
			del cfg
		super(Window, self).on_close()

	def on_draw(self):
		pyglet.gl.glClearColor(0, 0, 0, 0)
		self.clear()
		self.batch.draw()

	def on_map_sync(self, currentRoom):
		logger.debug("Map synced to {}".format(currentRoom))
		self.current_room = currentRoom
		self.redraw()

	def on_gui_refresh(self):
		"""This event is fired when the mapper needs to signal the GUI to clear the visible rooms cache and redraw the map view."""
		logger.debug("Clearing visible exits.")
		for dead in self.visible_exits:
			try:
				try:
					for d in self.visible_exits[dead]:
						d.delete()
				except TypeError:
					self.visible_exits[dead].delete()
			except AssertionError:
				pass
		self.visible_exits.clear()
		logger.debug("Clearing visible rooms.")
		for dead in self.visible_rooms:
			self.visible_rooms[dead][0].delete()
		self.visible_rooms.clear()
		if self.center_mark:
			for i in self.center_mark:
				i.delete()
			del self.center_mark[:]
		self.redraw()
		self.center_mark.append(self.draw_circle(self.cp, self.size / 2.0 / 8 * 3, Color(0, 0, 0, 255), self.groups[4]))
		self.center_mark.append(self.draw_circle(self.cp, self.size / 2.0 / 8, Color(255, 255, 255, 255), self.groups[5]))
		logger.debug("GUI refreshed.")

	def on_resize(self, width, height):
		super(Window, self).on_resize(width, height)
		logger.debug("resizing window to ({}, {})".format(width, height))
		if self.current_room is not None:
			self.on_gui_refresh()

	def on_key_press(self, sym, mod):
		logger.debug("Key press: sym: {}, mod: {}".format(sym, mod))
		key = (sym, mod)
		if key in KEYS:
			funcname = "do_" + KEYS[key]
			try:
				func = getattr(self, funcname)
				try:
					func(sym, mod)
				except Exception as e:
					logger.exception(e.message)
			except AttributeError:
				logger.error("Invalid key assignment for key {}. No such function {}.".format(key, funcname))

	def on_mouse_motion(self, x, y, dx, dy):
		for vnum, item in self.visible_rooms.items():
			vl, room, cp = item
			if math.floor((cp.x - self.cx + self.size / 2) / self.size) == math.floor((x - self.cx + self.size / 2) / self.size) and math.floor((cp.y - self.cy + self.size / 2) / self.size) == math.floor((y - self.cy + self.size / 2) / self.size):
				if vnum is None or vnum not in self.world.rooms:
					return
				elif self.highlight == vnum:
					# Room already highlighted.
					return
				self.highlight = vnum
				self.say("{}, {}".format(room.name, vnum), True)
				break
		else:
			self.highlight = None
		self.on_gui_refresh()

	def on_mouse_press(self, x, y, buttons, modifiers):
		logger.debug("Mouse press on {} {}, buttons: {}, modifiers: {}".format(x, y, buttons, modifiers))
		if buttons == pyglet.window.mouse.MIDDLE:
			self.do_reset_zoom(key.ESCAPE, 0)
			return
		# check if the player clicked on a room
		for vnum, item in self.visible_rooms.items():
			vl, room, cp = item
			if math.floor((cp.x - self.cx + self.size / 2) / self.size) == math.floor((x - self.cx + self.size / 2) / self.size) and math.floor((cp.y - self.cy + self.size / 2) / self.size) == math.floor((y - self.cy + self.size / 2) / self.size):
				# Action depends on which button the player clicked
				if vnum is None or vnum not in self.world.rooms:
					return
				elif buttons == pyglet.window.mouse.LEFT:
					if modifiers & key.MOD_SHIFT:
						# print the vnum
						self.world.output("{}, {}".format(vnum, room.name))
					else:
						result = self.world.path(vnum)
						if result is not None:
							self.world.output(result)
				elif buttons == pyglet.window.mouse.RIGHT:
					self.world.currentRoom = room
					self.world.output("Current room now set to '{}' with vnum {}".format(room.name, vnum))
				break

	def on_mouse_scroll(self, x, y, scroll_x, scroll_y):
		if scroll_y > 0:
			self.do_adjust_size(key.RIGHT, 0)
		elif scroll_y < 0:
			self.do_adjust_size(key.LEFT, 0)

	def on_mouse_leave(self, wx, wy):
		self.highlight = None
		self.on_gui_refresh()

	def do_toggle_blink(self, sym, mod):
		self.blink = not self.blink
		self.say("Blinking {}".format("enabled" if self.blink else "disabled"), True)

	def do_toggle_continuous_view(self, sym, mod):
		self.continuous_view = not self.continuous_view
		self.say("{} view".format("continuous" if self.continuous_view else "tiled"), True)
		self.on_gui_refresh()

	def do_toggle_fullscreen(self, sym, mod):
		fs = not self.fullscreen
		self.set_fullscreen(fs)
		self._cfg["fullscreen"] = fs
		self.say("fullscreen {}.".format("enabled" if fs else "disabled"), True)

	def do_adjust_gap(self, sym, mod):
		self.continuous_view = False
		if sym == key.DOWN:
			self.gap -= 10
		elif sym == key.UP:
			self.gap += 10
		self.say("{} Gap.".format(self.gap_as_float), True)
		self.on_gui_refresh()

	def do_adjust_size(self, sym, mod):
		if sym == key.LEFT:
			self.size -= 10
		elif sym == key.RIGHT:
			self.size += 10
		self.say("{}%".format(self.size), True)
		self.on_gui_refresh()

	def do_reset_zoom(self, sym, mod):
		self.size = 100
		self.on_gui_refresh()
		self.say("Reset zoom", True)

	def circle_vertices(self, cp, radius):
		cp = Vec2d(cp)
		# http://slabode.exofire.net/circle_draw.shtml
		num_segments = int(4 * math.sqrt(radius))
		theta = 2 * math.pi / num_segments
		c = math.cos(theta)
		s = math.sin(theta)
		x = radius  # We start at angle 0.
		y = 0
		ps = []
		for i in range(num_segments):
			ps += [Vec2d(cp.x + x, cp.y + y)]
			t = x
			x = c * x - s * y
			y = s * t + c * y
		ps2 = [ps[0]]
		for i in range(1, int((len(ps) + 1) // 2)):
			ps2.append(ps[i])
			ps2.append(ps[-i])
		ps = ps2
		vs = []
		for p in [ps[0]] + ps + [ps[-1]]:
			vs += [p.x, p.y]
		return vs

	def draw_circle(self, cp, radius, color, group=None):
		vs = self.circle_vertices(cp, radius)
		l = len(vs) // 2
		return self.batch.add(l, pyglet.gl.GL_TRIANGLE_STRIP, group, ("v2f", vs), ("c4B", color.as_int() * l))

	def draw_segment(self, a, b, color, group=None):
		pv1 = Vec2d(a)
		pv2 = Vec2d(b)
		line = (int(pv1.x), int(pv1.y), int(pv2.x), int(pv2.y))
		return self.batch.add(2, pyglet.gl.GL_LINES, group, ("v2i", line), ("c4B", color.as_int() * 2))

	def fat_segment_vertices(self, a, b, radius):
		pv1 = Vec2d(a)
		pv2 = Vec2d(b)
		d = pv2 - pv1
		a = -math.atan2(d.x, d.y)
		radius = max(radius, 1)
		dx = radius * math.cos(a)
		dy = radius * math.sin(a)
		p1 = pv1 + Vec2d(dx, dy)
		p2 = pv1 - Vec2d(dx, dy)
		p3 = pv2 + Vec2d(dx, dy)
		p4 = pv2 - Vec2d(dx, dy)
		vs = [i for xy in [p1, p2, p3] + [p2, p3, p4] for i in xy]
		return vs

	def draw_fat_segment(self, a, b, radius, color, group=None):
		vs = self.fat_segment_vertices(a, b, radius)
		l = len(vs) // 2
		return self.batch.add(l, pyglet.gl.GL_TRIANGLES, group, ("v2f", vs), ("c4B", color.as_int() * l))

	def corners_2_vertices(self, ps):
		ps = [ps[1], ps[2], ps[0]] + ps[3:]
		vs = []
		for p in [ps[0]] + ps + [ps[-1]]:
			vs += [p.x, p.y]
		return vs

	def draw_polygon(self, verts, color, group=None):
		mode = pyglet.gl.GL_TRIANGLE_STRIP
		vs = self.corners_2_vertices(verts)
		l = len(vs) // 2
		return self.batch.add(l, mode, group, ("v2f", vs), ("c4B", color.as_int() * l))

	def equilateral_triangle(self, cp, radius, angle_degrees):
		v = Vec2d(radius, 0)
		v.rotate_degrees(angle_degrees)
		w = v.rotated_degrees(120)
		y = w.rotated_degrees(120)
		return [v + cp, w + cp, y + cp]

	def square_from_cp(self, cp, d):
		return [cp - d, cp - (d, d * -1), cp + d, cp + (d, d * -1)]

	def arrow_points(self, a, d, r):
		l = d - a
		h = (r * 1.5) * math.sqrt(3)
		l.length -= h
		b = a + l
		l.length += h / 3.0
		c = a + l
		return (b, c, l.angle_degrees)

	def arrow_vertices(self, a, d, r):
		b, c, angle = self.arrow_points(a, d, r)
		vs1 = self.fat_segment_vertices(a, b, r)
		vs2 = self.corners_2_vertices(self.equilateral_triangle(c, r * 3, angle))
		return (vs1, vs2)

	def draw_arrow(self, a, d, radius, color, group=None):
		b, c, angle = self.arrow_points(a, d, radius)
		vl1 = self.draw_fat_segment(a, b, radius, color, group=group)
		vl2 = self.draw_polygon(self.equilateral_triangle(c, radius * 3, angle), color, group=group)
		return (vl1, vl2)

	def draw_room(self, room, cp, group=None):
		color = Color(*self.terrain_colors.get("highlight" if self.highlight is not None and self.highlight == room.vnum else room.terrain, "undefined"))
		vs = self.square_from_cp(cp, self.size / 2.0)
		if group is None:
			group = self.groups[0]
		if room.vnum not in self.visible_rooms:
			vl = self.draw_polygon(vs, color, group=group)
			self.visible_rooms[room.vnum] = [vl, room, cp]
		else:
			vl = self.visible_rooms[room.vnum][0]
			vl.vertices = self.corners_2_vertices(vs)
			self.batch.migrate(vl, pyglet.gl.GL_TRIANGLE_STRIP, group, self.batch)
			self.visible_rooms[room.vnum][2] = cp

	def draw_rooms(self, current_room=None):
		if current_room is None:
			current_room = self.current_room
		logger.debug("Drawing rooms near {}".format(current_room))
		self.draw_room(current_room, self.cp, group=self.groups[1])
		newrooms = {current_room.vnum}
		for vnum, room, x, y, z in self.world.getNeighborsFromRoom(start=current_room, radius=self.room_draw_radius):
			if z == 0:
				newrooms.add(vnum)
				d = Vec2d(x, y) * (self.size * (1 if self.continuous_view else self.gap_as_float + 1.0))
				self.draw_room(room, self.cp + d)
		if not self.visible_rooms:
			return
		for dead in set(self.visible_rooms) - newrooms:
			self.visible_rooms[dead][0].delete()
			del self.visible_rooms[dead]

	def draw_exits(self):
		logger.debug("Drawing exits")
		try:
			exit_color1 = self._cfg["exit_color1"]
		except KeyError:
			exit_color1 = (255, 228, 225, 255)
			self._cfg["exit_color1"] = exit_color1
		try:
			exit_color2 = self._cfg["exit_color2"]
		except KeyError:
			exit_color2 = (0, 0, 0, 255)
			self._cfg["exit_color2"] = exit_color2
		exit_color1 = Color(*exit_color1)
		exit_color2 = Color(*exit_color2)
		try:
			radius = self._cfg["exit_radius"]
			if not isinstance(radius, int):
				raise ValueError
		except (KeyError, ValueError) as error:
			if isinstance(error, ValueError):
				logger.warning("Invalid value for exit_radius in config.json: {}".format(radius))
			radius = 10
			self._cfg["exit_radius"] = radius
		newexits = set()
		for vnum, item in self.visible_rooms.items():
			vl, room, cp = item
			if self.continuous_view:
				exits = DIRECTIONS_2D.symmetric_difference(room.exits)  # Swap NESW exits with directions you can't go. Leave up/down in place if present.
				for direction in room.exits:
					if not self.world.isBidirectional(room.exits[direction]):
						exits.add(direction)  # Add any existing NESW exits that are unidirectional back to the exits set for processing later.
			else:
				exits = set(room.exits)  # Normal exits list
			for direction in exits:
				name = vnum + direction
				exit = room.exits.get(direction, None)
				dv = DIRECTIONS_VEC2D.get(direction, None)
				if direction in ("up", "down"):
					if direction == "up":
						new_cp = cp + (0, self.size / 4.0)
						angle = 90
					elif direction == "down":
						new_cp = cp - (0, self.size / 4.0)
						angle = -90
					if self.world.isBidirectional(exit):
						vs1 = self.equilateral_triangle(new_cp, (self.size / 4.0) + 14, angle)
						vs2 = self.equilateral_triangle(new_cp, self.size / 4.0, angle)
						if name in self.visible_exits and isinstance(self.visible_exits[name], tuple):
							vl1, vl2 = self.visible_exits[name]
							vl1.vertices = self.corners_2_vertices(vs1)
							vl2.vertices = self.corners_2_vertices(vs2)
						else:
							if name in self.visible_exits:
								self.visible_exits[name].delete()
							vl1 = self.draw_polygon(vs1, exit_color2, group=self.groups[2])
							vl2 = self.draw_polygon(vs2, exit_color1, group=self.groups[2])
							self.visible_exits[name] = (vl1, vl2)
					elif exit.to in ("undefined", "death"):
						if name in self.visible_exits and not isinstance(self.visible_exits[name], tuple):
							vl = self.visible_exits[name]
							vl.x, vl.y = new_cp
						elif exit.to == "undefined":
							self.visible_exits[name] = pyglet.text.Label("?", font_name="Times New Roman", font_size=(self.size / 100.0) * 72, x=new_cp.x, y=new_cp.y, anchor_x="center", anchor_y="center", color=exit_color2, batch=self.batch, group=self.groups[2])
						else:  # Death
							self.visible_exits[name] = pyglet.text.Label("X", font_name="Times New Roman", font_size=(self.size / 100.0) * 72, x=new_cp.x, y=new_cp.y, anchor_x="center", anchor_y="center", color=Color(255, 0, 0, 255), batch=self.batch, group=self.groups[2])
					else:  # one-way, random, etc
						l = new_cp - cp
						l.length /= 2
						a = new_cp - l
						d = new_cp + l
						r = (self.size / radius) / 2.0
						if name in self.visible_exits and isinstance(self.visible_exits[name], tuple):
							vl1, vl2 = self.visible_exits[name]
							vs1, vs2 = self.arrow_vertices(a, d, r)
							vl1.vertices = vs1
							vl2.vertices = vs2
						else:
							if name in self.visible_exits:
								self.visible_exits[name].delete()
							vl1, vl2 = self.draw_arrow(a, d, r, exit_color2, group=self.groups[2])
							self.visible_exits[name] = (vl1, vl2)
				else:
					if self.continuous_view:
						name += "-"
						if exit is None:
							color = exit_color2
						elif exit.to == "undefined":
							color = Color(0, 0, 255, 255)
						elif exit.to == "death":
							color = Color(255, 0, 0, 255)
						else:
							color = Color(0, 255, 0, 255)
						a, b, c, d = self.square_from_cp(cp, self.size / 2.0)
						if direction == "west":
							s = (a, b)
						elif direction == "north":
							s = (b, c)
						elif direction == "east":
							s = (c, d)
						elif direction == "south":
							s = (d, a)
						if name in self.visible_exits and not isinstance(self.visible_exits[name], tuple):
							vl = self.visible_exits[name]
							vl.vertices = self.fat_segment_vertices(s[0], s[1], self.size / radius / 2.0)
							vl.colors = color * (len(vl.colors) // 4)
						else:
							self.visible_exits[name] = self.draw_fat_segment(s[0], s[1], self.size / radius, color, group=self.groups[2])
					else:
						if self.world.isBidirectional(exit):
							l = (self.size * self.gap_as_float) / 2
							a = cp + (dv * (self.size / 2.0))
							b = a + (dv * l)
							if name in self.visible_exits and not isinstance(self.visible_exits[name], tuple):
								vl = self.visible_exits[name]
								vs = self.fat_segment_vertices(a, b, self.size / radius)
								vl.vertices = vs
							else:
								self.visible_exits[name] = self.draw_fat_segment(a, b, self.size / radius, exit_color1, group=self.groups[2])
						elif exit.to in ("undefined", "death"):
							l = (self.size * 0.75)
							new_cp = cp + dv * l
							if name in self.visible_exits and not isinstance(self.visible_exits[name], tuple):
								vl = self.visible_exits[name]
								vl.x, vl.y = new_cp
							elif exit.to == "undefined":
								self.visible_exits[name] = pyglet.text.Label("?", font_name="Times New Roman", font_size=(self.size / 100.0) * 72, x=new_cp.x, y=new_cp.y, anchor_x="center", anchor_y="center", color=exit_color1, batch=self.batch, group=self.groups[2])
							else:  # Death
								self.visible_exits[name] = pyglet.text.Label("X", font_name="Times New Roman", font_size=(self.size / 100.0) * 72, x=new_cp.x, y=new_cp.y, anchor_x="center", anchor_y="center", color=Color(255, 0, 0, 255), batch=self.batch, group=self.groups[2])
						else:  # One-way, random, etc.
							color = exit_color1
							l = (self.size * self.gap_as_float) / 2
							a = cp + (dv * (self.size / 2.0))
							d = a + (dv * l)
							r = ((self.size / radius) / 2.0) * self.gap_as_float
							if name in self.visible_exits and isinstance(self.visible_exits[name], tuple):
								vl1, vl2 = self.visible_exits[name]
								vs1, vs2 = self.arrow_vertices(a, d, r)
								vl1.vertices = vs1
								vl1.colors = color * (len(vl1.colors) // 4)
								vl2.vertices = vs2
								vl2.colors = color * (len(vl2.colors) // 4)
							else:
								if name in self.visible_exits:
									self.visible_exits[name].delete()
								self.visible_exits[name] = self.draw_arrow(a, d, r, color, group=self.groups[2])
				newexits.add(name)
		for dead in set(self.visible_exits) - newexits:
			try:
				try:
					for d in self.visible_exits[dead]:
						d.delete()
				except TypeError:
					self.visible_exits[dead].delete()
			except AssertionError:
				pass
			del self.visible_exits[dead]

	def enable_current_room_markers(self):
		if "current_room_markers" in self.blinkers:
			return
		current_room_markers = []
		current_room_markers.append(Blinker(self.blink_rate, self.draw_circle, lambda: ((self.cp - (self.size / 2.0), (self.size / 100.0) * self.current_room_mark_radius, self.current_room_mark_color), {"group": self.groups[5]})))
		current_room_markers.append(Blinker(self.blink_rate, self.draw_circle, lambda: ((self.cp - (self.size / 2.0, -self.size / 2.0), (self.size / 100.0) * self.current_room_mark_radius, self.current_room_mark_color), {"group": self.groups[5]})))
		current_room_markers.append(Blinker(self.blink_rate, self.draw_circle, lambda: ((self.cp + (self.size / 2.0), (self.size / 100.0) * self.current_room_mark_radius, self.current_room_mark_color), {"group": self.groups[5]})))
		current_room_markers.append(Blinker(self.blink_rate, self.draw_circle, lambda: ((self.cp + (self.size / 2.0, -self.size / 2.0), (self.size / 100.0) * self.current_room_mark_radius, self.current_room_mark_color), {"group": self.groups[5]})))
		self.blinkers["current_room_markers"] = tuple(current_room_markers)

	def redraw(self):
		logger.debug("Redrawing...")
		self.draw_rooms()
		self.draw_exits()


Window.register_event_type("on_map_sync")
Window.register_event_type("on_gui_refresh")
