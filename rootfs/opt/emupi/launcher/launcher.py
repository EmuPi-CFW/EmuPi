#!/usr/bin/env python3
"""EmuPi OS launcher.

Pick a system, pick a game. The launcher writes what to do into --cmd-file and
exits; emupi-session then runs RetroArch (or powers off) and starts the launcher
again. Keeping the frontend and the emulator in separate processes means the
display is fully released before RetroArch starts.

Renders to a fixed 640x360 canvas which SDL scales to the display, so the Pi
Zero only ever has to push a small software surface.

Test on a desktop:
    EMUPI_ROMS=./roms python3 launcher.py --windowed --cmd-file /tmp/emupi-cmd
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time
import traceback

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

W, H = 640, 360
ROM_ROOT = os.environ.get("EMUPI_ROMS", "/mnt/roms")
CFG_DIR = os.path.expanduser("~/.config/emupi")
STATE_FILE = os.path.join(CFG_DIR, "state.json")
USER_CFG = os.path.join(ROM_ROOT, "emupi.json")

BG_TOP = (19, 18, 36)
BG_BOTTOM = (8, 8, 15)
CARD = (28, 28, 49)
TEXT = (238, 238, 248)
MUTED = (146, 148, 178)
DIM = (96, 98, 128)

SYSTEMS = [
    dict(id="nes", name="Nintendo Entertainment System", short="NES",
         exts=(".nes", ".fds", ".unf", ".unif", ".zip"),
         cores=("fceumm", "nestopia"), color=(240, 78, 92)),
    dict(id="gba", name="Game Boy Advance", short="GBA",
         exts=(".gba", ".zip"),
         cores=("gpsp", "mgba"), color=(138, 118, 255)),
]
POWER = dict(id="power", name="Power", short="Power", color=(66, 214, 178))
TILES = SYSTEMS + [POWER]

CORE_DIRS = [
    os.path.join(ROM_ROOT, "cores"),  # drop-in cores (e.g. a custom gpSP build)
    "/usr/lib/*/libretro",
    "/usr/lib/libretro",
    "/usr/local/lib/libretro",
]


# ----------------------------------------------------------------- helpers --
def dlog(*a):
    """Debug line to stderr (captured into emupi-logs/launcher.log by emupi-session)."""
    print("[launcher %.2f]" % time.monotonic(), *a, file=sys.stderr, flush=True)


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def find_core(names):
    for name in names:
        for d in CORE_DIRS:
            hits = glob.glob(os.path.join(d, name + "_libretro.so"))
            if hits:
                return hits[0]
    return None


def pretty(filename):
    base = os.path.splitext(filename)[0]
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", base)  # drop (USA) [!] tags
    base = base.replace("_", " ").strip()
    m = re.match(r"^(.*), (The|A|An)$", base)
    if m:
        base = "%s %s" % (m.group(2), m.group(1))
    return base or filename


def scan(system):
    base = os.path.join(ROM_ROOT, system["id"])
    items = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):  # macOS ._ files on exFAT
                continue
            if f.lower().endswith(system["exts"]):
                items.append((pretty(f), os.path.join(root, f)))
    items.sort(key=lambda t: t[0].lower())
    return items


# ------------------------------------------------------------- pixel art ----
def icon_nes(c, cx, cy, s):
    def r(v):
        return max(1, int(v * s))
    body = pygame.Rect(0, 0, r(150), r(64))
    body.center = (cx, cy)
    pygame.draw.rect(c, (214, 214, 220), body, border_radius=r(6))
    inner = body.inflate(-r(14), -r(14))
    pygame.draw.rect(c, (38, 38, 46), inner, border_radius=r(4))
    dx, dy = inner.left + r(24), inner.centery
    pygame.draw.rect(c, (205, 205, 212), (dx - r(5), dy - r(15), r(10), r(30)))
    pygame.draw.rect(c, (205, 205, 212), (dx - r(15), dy - r(5), r(30), r(10)))
    for k in (-1, 1):
        pill = pygame.Rect(0, 0, r(20), r(7))
        pill.center = (cx + k * r(16), dy + r(10))
        pygame.draw.rect(c, (140, 140, 152), pill, border_radius=r(3))
    for k in (22, 46):
        pygame.draw.circle(c, (226, 60, 74), (inner.right - r(k), dy + r(4)), r(9))


def icon_gba(c, cx, cy, s):
    def r(v):
        return max(1, int(v * s))
    body = pygame.Rect(0, 0, r(160), r(86))
    body.center = (cx, cy)
    pygame.draw.rect(c, (128, 110, 255), body, border_radius=r(26))
    bezel = pygame.Rect(0, 0, r(72), r(54))
    bezel.center = (cx, cy)
    pygame.draw.rect(c, (24, 24, 44), bezel, border_radius=r(5))
    pygame.draw.rect(c, (92, 104, 158), bezel.inflate(-r(14), -r(14)), border_radius=r(3))
    dx = body.left + r(30)
    pygame.draw.rect(c, (36, 30, 92), (dx - r(4), cy - r(14), r(8), r(28)))
    pygame.draw.rect(c, (36, 30, 92), (dx - r(14), cy - r(4), r(28), r(8)))
    pygame.draw.circle(c, (236, 232, 255), (body.right - r(24), cy - r(9)), r(8))
    pygame.draw.circle(c, (236, 232, 255), (body.right - r(44), cy + r(6)), r(8))


def icon_power(c, cx, cy, s):
    def r(v):
        return max(1, int(v * s))
    col = (230, 250, 244)
    rect = pygame.Rect(0, 0, r(70), r(70))
    rect.center = (cx, cy + r(4))
    w = max(2, r(7))
    pygame.draw.arc(c, col, rect, math.radians(130), math.radians(360), w)
    pygame.draw.arc(c, col, rect, 0, math.radians(50), w)
    pygame.draw.line(c, col, (cx, cy - r(34)), (cx, cy - r(4)), w)


ICONS = {"nes": icon_nes, "gba": icon_gba, "power": icon_power}


# ------------------------------------------------------------------ input ---
KEYMAP = {
    pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left",
    pygame.K_RIGHT: "right", pygame.K_RETURN: "ok", pygame.K_KP_ENTER: "ok",
    pygame.K_SPACE: "ok", pygame.K_x: "ok", pygame.K_ESCAPE: "back",
    pygame.K_BACKSPACE: "back", pygame.K_z: "back",
    pygame.K_PAGEUP: "pgup", pygame.K_PAGEDOWN: "pgdn",
}
DIRS = ("up", "down", "left", "right")


class Input:
    DELAY, REPEAT = 0.35, 0.09

    def __init__(self, cfg):
        self.btn = {
            "ok": set(cfg.get("ok_buttons", [0, 2, 7])),
            "back": set(cfg.get("back_buttons", [1, 3])),
            "pgup": set(cfg.get("pgup_buttons", [4])),
            "pgdn": set(cfg.get("pgdn_buttons", [5])),
        }
        self.src = {}
        self.held = None
        self.next = 0.0
        self.axis = [0.0, 0.0]

    def _set(self, source, d, now, out):
        old = self.src.get(source)
        self.src[source] = d
        if d and d != old:
            out.append(d)
            self.held, self.next = d, now + self.DELAY
        elif not d and self.held == old:
            self.held = next((v for v in self.src.values() if v), None)

    def translate(self, ev, now):
        out = []
        if ev.type == pygame.KEYDOWN:
            a = KEYMAP.get(ev.key)
            if a in DIRS:
                self._set("kb", a, now, out)
            elif a:
                out.append(a)
        elif ev.type == pygame.KEYUP:
            if KEYMAP.get(ev.key) in DIRS:
                self._set("kb", None, now, out)
        elif ev.type == pygame.JOYHATMOTION:
            x, y = ev.value
            d = "left" if x < 0 else "right" if x > 0 else "up" if y > 0 else "down" if y < 0 else None
            self._set("hat", d, now, out)
        elif ev.type == pygame.JOYAXISMOTION and ev.axis in (0, 1):
            self.axis[ev.axis] = ev.value
            ax, ay = self.axis
            d = None
            if max(abs(ax), abs(ay)) > 0.6:
                if abs(ax) >= abs(ay):
                    d = "right" if ax > 0 else "left"
                else:
                    d = "down" if ay > 0 else "up"
            self._set("axis", d, now, out)
        elif ev.type == pygame.JOYBUTTONDOWN:
            for action, buttons in self.btn.items():
                if ev.button in buttons:
                    out.append(action)
                    break
        return out

    def tick(self, now):
        if self.held and now >= self.next:
            self.next = now + self.REPEAT
            return [self.held]
        return []


# -------------------------------------------------------------------- app ---
class App:
    VIS, ROW_Y, ROW_H = 7, 76, 34

    def __init__(self, args):
        self.args = args
        self.cfg = load_json(USER_CFG, {})
        self.state = load_json(STATE_FILE, {})
        self.input = Input(self.cfg)
        self.joysticks = {}
        self.init_display()
        self.canvas = pygame.Surface((W, H)).convert()
        self.bg = self.make_background()
        self.glows = [self.make_glow(t["color"]) for t in TILES]
        self.fonts = {}
        self.tcache = {}
        self.clock = pygame.time.Clock()
        self.toast = None
        self.dirty = True

        self.counts = {}
        self.refresh_counts()
        last = self.state.get("home", 0)
        self.idx = last if 0 <= last < len(TILES) else 0
        self.pos = float(self.idx)
        self.screen_name = "home"
        self.items, self.sel, self.scroll = [], 0, 0.0
        self.tile = TILES[self.idx]
        if self.state.get("resume") in [s["id"] for s in SYSTEMS]:
            self.idx = [s["id"] for s in SYSTEMS].index(self.state["resume"])
            self.pos = float(self.idx)
            self.open_tile(self.idx)

    # --- setup
    def init_display(self):
        dlog("init: SDL_VIDEODRIVER=%r XDG_RUNTIME_DIR=%r uid=%d" % (
            os.environ.get("SDL_VIDEODRIVER"), os.environ.get("XDG_RUNTIME_DIR"), os.getuid()))
        dlog("pygame %s, SDL %s" % (pygame.version.ver, ".".join(map(str, pygame.get_sdl_version()))))
        pygame.display.init()
        dlog("video driver in use:", pygame.display.get_driver())
        pygame.font.init()
        pygame.joystick.init()
        dlog("joysticks:", pygame.joystick.get_count())
        for i in range(pygame.joystick.get_count()):
            self.add_joystick(i)
        if self.args.windowed:
            self.screen = pygame.display.set_mode((W * 2, H * 2))
        else:
            self.screen = None
            attempts = (
                ("FULLSCREEN|SCALED %dx%d" % (W, H), lambda: pygame.display.set_mode((W, H), pygame.FULLSCREEN | pygame.SCALED)),
                ("FULLSCREEN native", lambda: pygame.display.set_mode((0, 0), pygame.FULLSCREEN)),
                ("FULLSCREEN %dx%d" % (W, H), lambda: pygame.display.set_mode((W, H), pygame.FULLSCREEN)),
            )
            last = None
            for name, fn in attempts:
                try:
                    self.screen = fn()
                    dlog("set_mode OK via", name, "->", self.screen.get_size())
                    break
                except pygame.error as e:
                    last = e
                    dlog("set_mode FAILED via", name, ":", e)
            if self.screen is None:
                raise last
        pygame.display.set_caption("EmuPi OS")
        pygame.mouse.set_visible(False)

    def add_joystick(self, index):
        try:
            j = pygame.joystick.Joystick(index)
            self.joysticks[j.get_instance_id()] = j
        except pygame.error:
            pass

    def make_background(self):
        s = pygame.Surface((W, H))
        for y in range(H):
            pygame.draw.line(s, mix(BG_TOP, BG_BOTTOM, y / (H - 1)), (0, y), (W, y))
        return s.convert()

    def make_glow(self, color):
        small = pygame.Surface((64, 36), pygame.SRCALPHA)
        for r in range(42, 0, -1):
            a = int(78 * (1 - r / 42.0) ** 2)
            pygame.draw.circle(small, (color[0], color[1], color[2], a), (32, 15), r)
        return pygame.transform.smoothscale(small, (W, H)).convert_alpha()

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            path = pygame.font.match_font("roboto,dejavusans,liberationsans,arial", bold=bold)
            self.fonts[key] = pygame.font.Font(path, size)
        return self.fonts[key]

    def text(self, s, size, color, pos, anchor="topleft", bold=False):
        key = (s, size, color, bold)
        surf = self.tcache.get(key)
        if surf is None:
            if len(self.tcache) > 500:
                self.tcache.clear()
            surf = self.font(size, bold).render(s, True, color)
            self.tcache[key] = surf
        rect = surf.get_rect()
        setattr(rect, anchor, pos)
        self.canvas.blit(surf, rect)
        return rect

    def fit(self, s, size, maxw, bold=False):
        f = self.font(size, bold)
        if f.size(s)[0] <= maxw:
            return s
        while s and f.size(s + "...")[0] > maxw:
            s = s[:-1]
        return s.rstrip() + "..."

    # --- data
    def refresh_counts(self):
        for s in SYSTEMS:
            self.counts[s["id"]] = len(scan(s))

    def save_state(self, **extra):
        self.state["home"] = self.idx
        self.state.update(extra)
        save_json(STATE_FILE, self.state)

    # --- navigation
    def open_tile(self, idx):
        self.tile = TILES[idx]
        self.idx = idx
        if self.tile["id"] == "power":
            self.items = [("Shut down", "poweroff"), ("Restart", "reboot"), ("Cancel", "cancel")]
            self.sel = 0
        else:
            self.items = scan(self.tile)
            self.sel = 0
            want = self.state.get("last_" + self.tile["id"])
            for i, (_, path) in enumerate(self.items):
                if path == want:
                    self.sel = i
                    break
        self.scroll = float(self.target_scroll())
        self.screen_name = "list"
        self.dirty = True

    def target_scroll(self):
        n = len(self.items)
        return max(0, min(self.sel - self.VIS // 2, n - self.VIS))

    def handle(self, a):
        if self.screen_name == "home":
            if a == "left":
                self.idx = max(0, self.idx - 1)
            elif a == "right":
                self.idx = min(len(TILES) - 1, self.idx + 1)
            elif a == "ok":
                self.open_tile(self.idx)
            return
        n = len(self.items)
        if a == "back":
            self.state["resume"] = None
            self.save_state()
            self.refresh_counts()
            self.screen_name = "home"
        elif a in ("up", "down") and n:
            self.sel = (self.sel + (1 if a == "down" else -1)) % n
        elif a in ("pgup", "pgdn") and n:
            self.sel = max(0, min(n - 1, self.sel + (self.VIS if a == "pgdn" else -self.VIS)))
        elif a == "ok" and n:
            self.activate()

    def activate(self):
        label, payload = self.items[self.sel]
        if self.tile["id"] == "power":
            if payload == "cancel":
                self.screen_name = "home"
            else:
                self.finish([payload], "Shutting down..." if payload == "poweroff" else "Restarting...")
            return
        core = find_core(self.tile["cores"])
        if not core:
            self.toast = ("No %s core installed. See /mnt/roms/cores." % self.tile["short"], time.monotonic() + 4)
            return
        self.save_state(resume=self.tile["id"], **{"last_" + self.tile["id"]: payload})
        self.finish(["launch", core, payload], label)

    def finish(self, lines, caption):
        self.draw_message(caption)
        self.present()
        pygame.time.wait(250)
        d = os.path.dirname(self.args.cmd_file)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.args.cmd_file, "w") as f:
            f.write("\n".join(lines) + "\n")
        pygame.quit()
        sys.exit(0)

    # --- update/draw
    def update(self, dt):
        k = min(1.0, dt * 11)
        animating = False
        d = self.idx - self.pos
        if abs(d) > 0.002:
            self.pos += d * k
            animating = True
        else:
            self.pos = float(self.idx)
        if self.screen_name == "list":
            t = self.target_scroll()
            d = t - self.scroll
            if abs(d) > 0.01:
                self.scroll += d * k
                animating = True
            else:
                self.scroll = float(t)
        return animating

    def draw(self):
        if self.screen_name == "home":
            self.draw_home()
        else:
            self.draw_list()
        if self.toast:
            msg = self.fit(self.toast[0], 15, W - 80)
            r = self.text(msg, 15, TEXT, (W // 2, H - 52), "midbottom")
            pygame.draw.rect(self.canvas, (50, 30, 44), r.inflate(28, 14), border_radius=10)
            self.text(msg, 15, TEXT, (W // 2, H - 52), "midbottom")

    def draw_home(self):
        c = self.canvas
        c.blit(self.bg, (0, 0))
        for i, g in enumerate(self.glows):
            a = max(0.0, 1 - abs(self.pos - i))
            if a > 0.01:
                g.set_alpha(int(255 * a))
                c.blit(g, (0, 0))
        accent = self.accent_at(self.pos)

        self.text("EmuPi", 26, TEXT, (28, 18), bold=True)
        r = self.text("OS", 26, accent, (28 + self.font(26, True).size("EmuPi ")[0], 18), bold=True)

        order = sorted(range(len(TILES)), key=lambda i: -abs(i - self.pos))
        for i in order:
            self.draw_card(i, i - self.pos)

        near = TILES[int(round(self.pos))]
        if near["id"] == "power":
            sub = "Shut down or restart"
        else:
            n = self.counts.get(near["id"], 0)
            sub = "%d game%s" % (n, "" if n == 1 else "s") if n else "No games yet"
        self.text(near["name"], 21, TEXT, (W // 2, 300), "midtop", bold=True)
        self.text(sub, 14, MUTED, (W // 2, 326), "midtop")

        for i in range(len(TILES)):
            on = i == self.idx
            pygame.draw.circle(c, accent if on else DIM, (W // 2 + (i - 1) * 16, 350), 4 if on else 3)
        # edge chevrons
        for side, ok in ((-1, self.idx > 0), (1, self.idx < len(TILES) - 1)):
            if ok:
                x = 20 if side < 0 else W - 20
                pts = [(x + side * -5, 160), (x + side * 5, 175), (x + side * -5, 190)]
                pygame.draw.lines(c, MUTED, False, pts, 3)

    def accent_at(self, pos):
        i = max(0, min(len(TILES) - 2, int(math.floor(pos))))
        return mix(TILES[i]["color"], TILES[i + 1]["color"], max(0.0, min(1.0, pos - i)))

    def draw_card(self, i, off):
        c = self.canvas
        t = TILES[i]
        d = min(1.0, abs(off))
        s = 1.0 - 0.2 * d
        cw, ch = int(214 * s), int(224 * s)
        rect = pygame.Rect(0, 0, cw, ch)
        rect.center = (W // 2 + int(off * 252), 168)
        pygame.draw.rect(c, (6, 6, 12), rect.move(0, 9), border_radius=22)
        body = mix(CARD, t["color"], 0.20 * (1 - d))
        pygame.draw.rect(c, body, rect, border_radius=22)
        if d < 0.5:
            pygame.draw.rect(c, t["color"], rect, width=3, border_radius=22)
        ICONS[t["id"]](c, rect.centerx, rect.centery - int(22 * s), 1.0 * s)
        size = 34 if d < 0.5 else 24
        col = TEXT if d < 0.5 else MUTED
        self.text(t["short"], size, col, (rect.centerx, rect.bottom - int(34 * s)), "center", bold=True)

    def draw_list(self):
        c = self.canvas
        t = self.tile
        c.blit(self.bg, (0, 0))
        g = self.glows[TILES.index(t)]
        g.set_alpha(255)
        c.blit(g, (0, 0))
        accent = t["color"]

        pygame.draw.rect(c, mix(BG_TOP, accent, 0.16), (0, 0, W, 58))
        pygame.draw.line(c, accent, (0, 58), (W, 58), 2)
        ICONS[t["id"]](c, 52, 29, 0.32 if t["id"] != "power" else 0.4)
        self.text(self.fit(t["name"], 22, 420, True), 22, TEXT, (96, 29), "midleft", bold=True)
        n = len(self.items)
        if n and t["id"] != "power":
            self.text("%d / %d" % (self.sel + 1, n), 14, MUTED, (W - 24, 29), "midright")

        clip = pygame.Rect(0, self.ROW_Y - 4, W, self.VIS * self.ROW_H + 8)
        c.set_clip(clip)
        if not n:
            self.draw_empty(t)
        lo = max(0, int(self.scroll) - 1)
        hi = min(n, int(self.scroll) + self.VIS + 2)
        for idx in range(lo, hi):
            y = int(self.ROW_Y + (idx - self.scroll) * self.ROW_H)
            label = self.fit(self.items[idx][0], 19, W - 120)
            if idx == self.sel:
                row = pygame.Rect(24, y, W - 64, self.ROW_H - 4)
                pygame.draw.rect(c, mix(BG_TOP, accent, 0.38), row, border_radius=9)
                pygame.draw.rect(c, accent, (row.x, row.y + 5, 4, row.h - 10), border_radius=2)
                self.text(label, 19, TEXT, (row.x + 18, row.centery), "midleft", bold=True)
            else:
                self.text(label, 19, MUTED, (42, y + (self.ROW_H - 4) // 2), "midleft")
        c.set_clip(None)

        if n > self.VIS:
            th = self.VIS * self.ROW_H
            pygame.draw.rect(c, (34, 34, 56), (W - 20, self.ROW_Y, 4, th), border_radius=2)
            bh = max(18, int(th * self.VIS / n))
            by = self.ROW_Y + int((th - bh) * (self.scroll / max(1, n - self.VIS)))
            pygame.draw.rect(c, accent, (W - 20, by, 4, bh), border_radius=2)

        x = 28
        x += self.hint(x, H - 26, "A", "Select", accent) + 22
        self.hint(x, H - 26, "B", "Back", accent)

    def draw_empty(self, t):
        self.text("No %s games found" % t["short"], 22, TEXT, (W // 2, 150), "midtop", bold=True)
        self.text("Copy ROMs to the EMUROMS drive, in the folder", 15, MUTED, (W // 2, 188), "midtop")
        self.text("%s/%s" % (ROM_ROOT, t["id"]), 15, TEXT, (W // 2, 210), "midtop")

    def hint(self, x, y, key, label, accent):
        pygame.draw.circle(self.canvas, accent, (x + 10, y), 10)
        self.text(key, 13, (14, 14, 26), (x + 10, y), "center", bold=True)
        r = self.text(label, 14, MUTED, (x + 28, y), "midleft")
        return r.right - x

    def draw_message(self, caption):
        c = self.canvas
        c.blit(self.bg, (0, 0))
        accent = self.tile["color"] if hasattr(self, "tile") else TEXT
        g = self.glows[TILES.index(self.tile)]
        g.set_alpha(255)
        c.blit(g, (0, 0))
        self.text(self.fit(caption, 26, W - 80, True), 26, TEXT, (W // 2, 168), "center", bold=True)
        pygame.draw.rect(c, accent, (W // 2 - 28, 200, 56, 4), border_radius=2)

    def present(self):
        sw, sh = self.screen.get_size()
        if (sw, sh) == (W, H):
            self.screen.blit(self.canvas, (0, 0))
        else:
            pygame.transform.scale(self.canvas, (sw, sh), self.screen)
        pygame.display.flip()

    # --- main loop
    def run(self):
        last = time.monotonic()
        while True:
            now = time.monotonic()
            dt = min(0.1, now - last)
            last = now
            actions = []
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.JOYDEVICEADDED:
                    self.add_joystick(ev.device_index)
                elif ev.type == pygame.JOYDEVICEREMOVED:
                    self.joysticks.pop(getattr(ev, "instance_id", -1), None)
                actions += self.input.translate(ev, now)
            actions += self.input.tick(now)
            for a in actions:
                self.handle(a)
                self.dirty = True
            if self.toast and now > self.toast[1]:
                self.toast = None
                self.dirty = True
            animating = self.update(dt)
            if self.dirty or animating:
                self.draw()
                self.present()
                self.dirty = False
                self.clock.tick(30)
            else:
                pygame.time.wait(25)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd-file", default="/run/emupi/cmd")
    ap.add_argument("--windowed", action="store_true", help="desktop test mode")
    args = ap.parse_args()
    dlog("start; rom root %s exists=%s" % (ROM_ROOT, os.path.isdir(ROM_ROOT)))
    try:
        try:
            app = App(args)
        except pygame.error as e:
            # Fall back to SDL's default driver selection (e.g. when testing on a desktop).
            dlog("display init failed (%s); retrying with default driver" % e)
            os.environ.pop("SDL_VIDEODRIVER", None)
            pygame.display.quit()
            app = App(args)
        dlog("running; games: %s" % app.counts)
        app.run()
        dlog("main loop ended normally (window closed) without a command")
    except SystemExit:
        raise
    except BaseException:
        dlog("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
    pygame.quit()
    # No command was written: tell the session this was NOT a normal exit.
    sys.exit(2)


if __name__ == "__main__":
    main()
