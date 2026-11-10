#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FISHAO AUTO FISHING + BALANCE  (Linux Mint / X11)  -  GUI Tkinter
"""

import copy
import json
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import cv2
import mss
import numpy as np
import pyautogui
from PIL import Image, ImageDraw, ImageTk

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = True   # mouse ke pojok layar = berhenti darurat

# =====================================
# 1. FILE & DEFAULT CONFIG
# =====================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "fishao_config.json")
CLOSE_FILE = os.path.join(BASE_DIR, "close.png")
HABIS_FILE = os.path.join(BASE_DIR, "habis.png")
ENERGI_FILE = os.path.join(BASE_DIR, "energi_habis.png")
BITE_FILE = os.path.join(BASE_DIR, "bite.png")
BITE_PREVIEW_FILE = os.path.join(BASE_DIR, "bite_preview.png")

APP_TITLE = "Fishao Bot (Linux)"

DEFAULT_CONFIG = {
    # area bar pelangi (tetap / cadangan)
    "region": {"left": 675, "top": 385, "width": 295, "height": 142},
    # AUTO-LOKASI: posisi bar relatif terhadap pojok kiri atas bite.png
    # {"dx": int, "dy": int, "width": int, "height": int}. None = auto mati
    "bite_offset": None,
    # titik tombol ROD (untuk cek warna)
    "rod": {"x": 1332, "y": 547},
    # daftar lokasi klik lempar pancing (dipilih acak). Kosong = pakai titik ROD
    "click_points": [],
    # jumlah titik default saat memilih
    "click_count": 5,
    # delay acak sebelum klik lempar pancing (detik). 0 dan 0 = tanpa delay
    "click_delay_min": 1.0,
    "click_delay_max": 3.0,
    # warna di titik ROD
    "color_idle": None,          # BELUM mancing  [r, g, b]
    "color_fishing": None,       # SEDANG mancing [r, g, b]
    "color_tolerance": 40,
    # gambar
    "close_confidence": 0.85,
    "habis_confidence": 0.85,
    "energi_confidence": 0.85,
    "bite_confidence": 0.80,
    # balance
    "deadzone": 6,
}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)

            for k, v in saved.items():
                if k in cfg:
                    cfg[k] = v

            # Migrasi dari versi lama (1 lokasi klik khusus)
            if (
                "click_points" not in saved
                and saved.get("click_same") is False
                and isinstance(saved.get("click"), dict)
            ):
                cfg["click_points"] = [saved["click"]]

        except Exception as e:
            print(f"[WARN] Gagal membaca config: {e}")

    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[WARN] Gagal menyimpan config: {e}")


def pick_click_point(cfg):
    """Pilih lokasi klik acak dari daftar. Return (titik, jumlah_titik)."""

    pts = cfg.get("click_points") or []

    if pts:
        return random.choice(pts), len(pts)

    return cfg["rod"], 0


# =====================================
# 2. KONSTANTA BOT
# =====================================

SWITCH_DELAY = 0.01       # jeda minimum pergantian arah (detik)
GRACE_TIME = 0.06         # tahan arah terakhir kalau penanda hilang sekejap
MIN_BLUE_PIXELS = 6       # piksel biru minimal supaya dianggap penanda
CHECK_INTERVAL = 0.3      # seberapa sering siklus mancing mengecek layar
DELAY_CHECK_INTERVAL = 0.1  # cek ringan (warna ROD) saat menunggu delay klik
CLICK_COOLDOWN = 1.5      # jeda setelah klik lempar pancing
CLOSE_COOLDOWN = 1.0      # jeda setelah ESC
BAR_LOST_TIME = 1.0       # bar harus hilang segini lama sebelum siklus jalan
START_DELAY = 3           # jeda setelah tombol Mulai (klik jendela game!)
HOTKEY_DEBOUNCE = 0.5     # jeda minimum antar tekanan SPASI (detik)

BITE_SEARCH_INTERVAL = 0.15  # seberapa sering mencari bite.png (mode auto)
LOCK_LOST_TIME = 0.5         # bar hilang segini lama -> lepas kunci area

# Kalibrasi auto: jarak pencarian bar di sekitar judul & margin area bar
FIND_BAR_SIDE = 110       # px kiri/kanan judul yang ikut dicari
FIND_BAR_DEPTH = 240      # px di bawah judul yang dicari
BAR_PAD_X = 8             # margin kiri/kanan area bar (px)
BAR_PAD_Y = 16            # margin atas/bawah area bar (px, untuk segitiga)

# Offset dianggap tidak valid kalau area terlalu kecil
MIN_OFFSET_W = 40
MIN_OFFSET_H = 8

# Batas warna HSV (OpenCV: H 0-179)
RAINBOW_MIN_S = 110
RAINBOW_MIN_V = 120
GREEN_H_MIN, GREEN_H_MAX = 45, 80
BLUE_H_MIN, BLUE_H_MAX = 95, 135

PREVIEW_WIDTH = 360
BITE_PREVIEW_WIDTH = 420


# =====================================
# 3. UTILITAS
# =====================================

def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def color_dist(a, b):
    return float(np.sqrt(sum((int(x) - int(y)) ** 2 for x, y in zip(a, b))))


def rod_is_idle(rgb, cfg):
    """True kalau warna di tombol ROD = BELUM mancing."""

    idle = cfg.get("color_idle")

    if idle is None:
        # Belum diambil: pakai aturan lama (oranye/cokelat)
        r, g, b = rgb
        return r > 140 and b < 50

    d_idle = color_dist(rgb, idle)

    if d_idle > cfg["color_tolerance"]:
        return False

    fishing = cfg.get("color_fishing")

    if fishing is not None and color_dist(rgb, fishing) < d_idle:
        return False

    return True


def read_pixel_once(x, y):
    """Baca warna 1 piksel di layar (koordinat global). Return (r, g, b)."""
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": 1, "height": 1})
        return tuple(int(v) for v in shot.pixel(0, 0))


def match_template_img(screen, tpl):
    """
    Cocokkan template ke gambar layar (BGR).
    Return (kemiripan, (x, y) pojok kiri atas) atau (0.0, None).
    """

    if tpl is None or screen is None:
        return 0.0, None

    th, tw = tpl.shape[:2]

    if th > screen.shape[0] or tw > screen.shape[1]:
        return 0.0, None

    res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)

    if not np.isfinite(max_val):
        return 0.0, None

    return float(max_val), max_loc


def offset_ok(off):
    """True kalau offset auto-lokasi masuk akal (bukan kotak kecil nyasar)."""

    if not isinstance(off, dict):
        return False

    try:
        w = int(off.get("width", 0) or 0)
        h = int(off.get("height", 0) or 0)
    except (TypeError, ValueError):
        return False

    return w >= MIN_OFFSET_W and h >= MIN_OFFSET_H


# =====================================
# 4. DETEKSI BAR PELANGI
# =====================================

def detect(frame):
    """
    Return dict berisi:
      bar      = (x, y, w, h) bar pelangi, atau None
      green_x  = tengah zona hijau
      marker_x = posisi segitiga biru, atau None
    """

    fh, fw = frame.shape[:2]

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    # ---- Mask warna pelangi (merah -> kuning -> hijau)
    rainbow = (
        (s >= RAINBOW_MIN_S)
        & (v >= RAINBOW_MIN_V)
        & ((h <= 85) | (h >= 165))
    ).astype(np.uint8) * 255

    rainbow = cv2.morphologyEx(
        rainbow, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)
    )

    green = (
        (h >= GREEN_H_MIN) & (h <= GREEN_H_MAX)
        & (s >= 90) & (v >= 120)
    )

    contours, _ = cv2.findContours(
        rainbow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    # ---- Pilih bar: lebar, pipih, dan ada hijaunya
    bar = None

    for c in contours:

        x, y, w, hh = cv2.boundingRect(c)

        if w < fw * 0.4 or hh < 8 or w / hh < 3:
            continue

        if green[y:y + hh, x:x + w].mean() < 0.08:
            continue

        if bar is None or w > bar[2]:
            bar = (x, y, w, hh)

    if bar is None:
        return {"bar": None, "green_x": None, "marker_x": None}

    bx, by, bw, bh = bar

    # ---- Tengah zona hijau
    green_cols = green[by:by + bh, bx:bx + bw].mean(axis=0) > 0.4
    idx = np.nonzero(green_cols)[0]

    if len(idx) > 0:
        green_x = bx + float(idx.mean())
    else:
        green_x = bx + bw / 2.0

    # ---- Segitiga biru
    blue = (
        (h >= BLUE_H_MIN) & (h <= BLUE_H_MAX)
        & (s >= 80) & (v >= 60)
    )

    y0 = max(0, by - 6)
    y1 = min(fh, by + bh + 6)

    ys, xs = np.nonzero(blue[y0:y1, bx:bx + bw])

    marker_x = None

    if len(xs) >= MIN_BLUE_PIXELS:
        marker_x = bx + float(np.median(xs))

    return {"bar": bar, "green_x": green_x, "marker_x": marker_x}


def find_bar_near_title(screen_bgr, tx, ty, tw, th):
    """
    Cari bar pelangi di bawah judul pada screenshot (koordinat gambar).
    Return (bx, by, bw, bh) dalam koordinat gambar, atau None.
    """

    H, W = screen_bgr.shape[:2]

    x0 = max(0, tx - FIND_BAR_SIDE)
    x1 = min(W, tx + tw + FIND_BAR_SIDE)
    y0 = min(H, ty + th)                      # mulai tepat di bawah judul
    y1 = min(H, ty + th + FIND_BAR_DEPTH)

    if x1 - x0 < 50 or y1 - y0 < 20:
        return None

    info = detect(screen_bgr[y0:y1, x0:x1])

    if info["bar"] is None:
        return None

    bx, by, bw, bh = info["bar"]

    return x0 + bx, y0 + by, bw, bh


def bar_in_region(screen_pil, region, ox, oy):
    """
    Cek apakah ada bar pelangi di dalam 'region' (koordinat global) pada
    screenshot beku. ox, oy = posisi monitor.
    """

    l = int(region["left"] - ox)
    t = int(region["top"] - oy)
    r = l + int(region["width"])
    b = t + int(region["height"])

    l = max(0, l)
    t = max(0, t)
    r = min(screen_pil.width, r)
    b = min(screen_pil.height, b)

    if r - l < 20 or b - t < 8:
        return False

    crop = screen_pil.crop((l, t, r, b)).convert("RGB")
    arr = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2BGR)

    return detect(arr)["bar"] is not None


def make_bite_preview(screen_pil, t, b, ox, oy):
    """
    Potongan layar beku + kotak MERAH (judul) + kotak HIJAU (area bar).
    t, b = region global judul dan bar.
    """

    tl, tt = int(t["left"] - ox), int(t["top"] - oy)
    bl, bt = int(b["left"] - ox), int(b["top"] - oy)
    tr, tb = tl + int(t["width"]), tt + int(t["height"])
    br, bb = bl + int(b["width"]), bt + int(b["height"])

    pad = 14
    x0 = max(0, min(tl, bl) - pad)
    y0 = max(0, min(tt, bt) - pad)
    x1 = min(screen_pil.width, max(tr, br) + pad)
    y1 = min(screen_pil.height, max(tb, bb) + pad)

    img = screen_pil.crop((x0, y0, x1, y1)).convert("RGB")
    d = ImageDraw.Draw(img)

    d.rectangle((tl - x0, tt - y0, tr - x0, tb - y0),
                outline=(255, 45, 45), width=2)
    d.rectangle((bl - x0, bt - y0, br - x0, bb - y0),
                outline=(0, 255, 102), width=2)

    return img


# =====================================
# 5. BOT (jalan di thread terpisah)
# =====================================

STATUS_COLORS = {
    "LEFT": (255, 165, 0),
    "RIGHT": (0, 255, 0),
    "CENTER": (0, 255, 255),
    "HOLD": (255, 0, 255),
    "WAIT": (255, 0, 0),
    "WAITING": (200, 200, 200),
    "DELAY": (255, 215, 0),
    "CLICK": (255, 255, 0),
    "CLOSE": (255, 200, 0),
    "HABIS": (255, 0, 0),
    "ENERGI": (255, 0, 0),
}


class FishingBot(threading.Thread):

    def __init__(self, cfg, dry_run, log, set_status, set_preview, on_finish):
        super().__init__(daemon=True)

        self.cfg = cfg
        self.dry_run = dry_run
        self.log = log
        self.set_status = set_status
        self.set_preview = set_preview
        self.on_finish = on_finish

        self.stop_event = threading.Event()
        self.stop_reason = None
        self.sct = None

        self.active_key = None
        self.last_switch = 0.0
        self.next_cycle_check = 0.0
        self.cycle_status = "WAITING"

        # Waktu klik yang dijadwalkan (setelah delay acak), None = tidak ada
        self.pending_click_at = None

        # Cache template gambar: path -> (mtime, gambar)
        self._tpl_cache = {}

        # Mode auto-lokasi bar via bite.png
        self.auto = False

    # ---------- keyboard ----------

    def _key_down(self, key):
        if not self.dry_run:
            pyautogui.keyDown(key)

    def _key_up(self, key):
        if self.dry_run:
            return

        old = pyautogui.FAILSAFE
        pyautogui.FAILSAFE = False

        try:
            pyautogui.keyUp(key)
        except Exception:
            pass
        finally:
            pyautogui.FAILSAFE = old

    def set_direction(self, direction):

        if direction == "RIGHT":
            new_key = "right"
        elif direction == "LEFT":
            new_key = "left"
        else:
            new_key = None

        if new_key == self.active_key:
            return

        now = time.perf_counter()

        # Lepaskan tombol lama dulu
        if self.active_key is not None:
            self._key_up(self.active_key)
            self.active_key = None

        if new_key is not None:
            remaining = SWITCH_DELAY - (now - self.last_switch)

            if remaining > 0:
                time.sleep(remaining)

            self._key_down(new_key)
            self.active_key = new_key

        self.last_switch = time.perf_counter()

    def release_all(self):
        if self.active_key is not None:
            self._key_up(self.active_key)

        self.active_key = None

    # ---------- screen ----------

    def grab(self, left, top, width, height):
        img = np.array(self.sct.grab({
            "left": left, "top": top,
            "width": width, "height": height
        }))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def grab_full(self):
        img = np.array(self.sct.grab(self.sct.monitors[1]))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def read_pixel(self, x, y):
        shot = self.sct.grab({"left": x, "top": y, "width": 1, "height": 1})
        return tuple(int(v) for v in shot.pixel(0, 0))

    def get_template(self, path):
        """Muat template dari file (di-cache, dimuat ulang kalau file berubah)."""

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        cached = self._tpl_cache.get(path)

        if cached is not None and cached[0] == mtime:
            return cached[1]

        tpl = cv2.imread(path, cv2.IMREAD_COLOR)
        self._tpl_cache[path] = (mtime, tpl)
        return tpl

    def find_template(self, path, confidence, screen=None):
        """
        Cari gambar (PNG hasil snipping) di layar. True kalau ketemu.
        Kalau 'screen' diberikan, pakai tangkapan layar itu (hemat).
        """

        if not os.path.exists(path):
            return False

        tpl = self.get_template(path)

        if tpl is None:
            return False

        if screen is None:
            screen = self.grab_full()

        val, _ = match_template_img(screen, tpl)

        return val >= confidence

    # ---------- AUTO-LOKASI bar via bite.png ----------

    def locate_bar_region(self):
        """
        Cari bite.png di layar, lalu hitung area bar = posisi bite + offset.
        Return (region_dict, kemiripan) atau None kalau tidak ketemu.
        """

        cfg = self.cfg
        off = cfg["bite_offset"]

        tpl = self.get_template(BITE_FILE)

        if tpl is None:
            return None

        screen = self.grab_full()
        val, loc = match_template_img(screen, tpl)

        if loc is None or val < cfg["bite_confidence"]:
            return None

        mon = self.sct.monitors[1]

        left = mon["left"] + loc[0] + int(off["dx"])
        top = mon["top"] + loc[1] + int(off["dy"])
        right = left + int(off["width"])
        bottom = top + int(off["height"])

        # Jaga supaya tidak keluar dari layar
        left = max(mon["left"], left)
        top = max(mon["top"], top)
        right = min(mon["left"] + mon["width"], right)
        bottom = min(mon["top"] + mon["height"], bottom)

        if right - left < 20 or bottom - top < 8:
            return None

        region = {
            "left": left, "top": top,
            "width": right - left, "height": bottom - top,
        }

        return region, val

    # ---------- siklus mancing ----------

    def random_delay(self):
        """Delay acak (detik) sebelum klik lempar pancing."""

        lo = float(self.cfg.get("click_delay_min", 0) or 0)
        hi = float(self.cfg.get("click_delay_max", 0) or 0)

        if hi < lo:
            lo, hi = hi, lo

        if hi <= 0:
            return 0.0

        return random.uniform(max(0.0, lo), hi)

    def cycle_step(self):
        cfg = self.cfg
        rod = cfg["rod"]

        now = time.time()

        # ---- Fase menunggu delay sebelum klik: cukup cek warna (ringan)
        if self.pending_click_at is not None and now < self.pending_click_at:

            rgb = self.read_pixel(rod["x"], rod["y"])

            if rod_is_idle(rgb, cfg):
                self.next_cycle_check = now + DELAY_CHECK_INTERVAL
                return "DELAY"

            self.pending_click_at = None
            self.log("[INFO] Status berubah saat menunggu delay, klik dibatalkan.")

        # Satu tangkapan layar dipakai untuk semua pengecekan gambar
        screen = self.grab_full()

        # 0a. Umpan habis -> berhenti
        if self.find_template(HABIS_FILE, cfg["habis_confidence"], screen):
            self.pending_click_at = None
            self.log("[DETEKSI] habis.png ditemukan! Umpan habis.")
            return "HABIS"

        # 0b. Energi habis -> berhenti
        if self.find_template(ENERGI_FILE, cfg["energi_confidence"], screen):
            self.pending_click_at = None
            self.log("[DETEKSI] energi_habis.png ditemukan! Energi habis.")
            return "ENERGI"

        # A. Popup ikan berhasil -> ESC
        if self.find_template(CLOSE_FILE, cfg["close_confidence"], screen):
            self.pending_click_at = None
            self.log("[DETEKSI] close.png ditemukan -> ESC")

            if not self.dry_run:
                pyautogui.press("esc")

            self.next_cycle_check = time.time() + CLOSE_COOLDOWN
            return "CLOSE"

        # B. Warna tombol ROD = belum mancing -> (delay acak) -> klik lempar
        rgb = self.read_pixel(rod["x"], rod["y"])

        if rod_is_idle(rgb, cfg):

            if self.pending_click_at is None:
                delay = self.random_delay()

                if delay > 0:
                    self.pending_click_at = time.time() + delay
                    self.log(
                        f"[AKSI] Belum mancing (RGB {rgb}) -> "
                        f"tunggu {delay:.1f} detik sebelum klik"
                    )
                    self.next_cycle_check = time.time() + DELAY_CHECK_INTERVAL
                    return "DELAY"

            # Delay selesai (atau delay = 0) -> klik
            self.pending_click_at = None

            c, total = pick_click_point(cfg)

            asal = f"acak dari {total} titik" if total else "titik ROD"

            self.log(
                f"[AKSI] Klik lempar pancing ({c['x']}, {c['y']}) [{asal}]"
            )

            if not self.dry_run:
                pyautogui.moveTo(c["x"], c["y"])
                time.sleep(0.05)
                pyautogui.click()

            self.next_cycle_check = time.time() + CLICK_COOLDOWN
            return "CLICK"

        # C. Sedang mancing, menunggu ikan
        self.pending_click_at = None
        self.next_cycle_check = time.time() + CHECK_INTERVAL
        return "WAITING"

    # ---------- main ----------

    def run(self):
        reason = "STOP"

        try:
            self.sct = mss.mss()
            self._loop()

            if self.stop_reason:
                reason = self.stop_reason

        except pyautogui.FailSafeException:
            reason = "FAILSAFE (mouse di pojok layar)"

        except Exception as e:
            reason = f"ERROR: {e!r}"

        finally:
            self.release_all()

            if self.sct is not None:
                try:
                    self.sct.close()
                except Exception:
                    pass

            self.on_finish(reason)

    def _loop(self):
        cfg = self.cfg
        fixed = cfg["region"]
        deadzone = cfg["deadzone"]

        off = cfg.get("bite_offset")
        has_offset = offset_ok(off)

        self.auto = has_offset and os.path.exists(BITE_FILE)

        mode = "[MODE UJI - tanpa tekan tombol] " if self.dry_run else ""

        self.log(
            f"{mode}Bot mulai dalam {START_DELAY} detik. "
            "Klik/fokuskan jendela GAME sekarang! (SPASI = berhenti)"
        )

        if self.auto:
            self.log(
                "[INFO] Area bar: AUTO (dicari lewat bite.png, "
                f"confidence {cfg['bite_confidence']:.2f})."
            )
        elif isinstance(off, dict) and not has_offset:
            self.log(
                "[WARN] Offset auto TIDAK VALID (area terlalu kecil). "
                "Kalibrasi ulang di tab Setup. Pakai area tetap."
            )
        elif has_offset:
            self.log(
                "[WARN] Offset auto ada tapi bite.png tidak ditemukan. "
                "Pakai area tetap."
            )
        else:
            self.log("[INFO] Area bar: TETAP (auto belum dikalibrasi).")

        if not os.path.exists(CLOSE_FILE):
            self.log("[WARN] close.png belum ada (ambil di tab Setup).")

        if not os.path.exists(HABIS_FILE):
            self.log("[WARN] habis.png belum ada (ambil di tab Setup).")

        if not os.path.exists(ENERGI_FILE):
            self.log("[WARN] energi_habis.png belum ada (ambil di tab Setup).")

        if cfg.get("color_idle") is None:
            self.log(
                "[INFO] Warna 'belum mancing' belum diambil, "
                "pakai aturan lama (oranye)."
            )

        n_click = len(cfg.get("click_points") or [])

        if n_click:
            self.log(f"[INFO] Lokasi klik: acak dari {n_click} titik.")
        else:
            self.log("[INFO] Lokasi klik: titik ROD (belum ada daftar titik).")

        lo = float(cfg.get("click_delay_min", 0) or 0)
        hi = float(cfg.get("click_delay_max", 0) or 0)

        if max(lo, hi) > 0:
            self.log(
                f"[INFO] Delay sebelum klik: acak "
                f"{min(lo, hi):.1f} - {max(lo, hi):.1f} detik."
            )
        else:
            self.log("[INFO] Delay sebelum klik: tidak ada.")

        start_time = time.time() + START_DELAY
        last_seen = 0.0
        last_bar_time = 0.0
        last_direction = None
        last_status = None
        last_log = 0.0

        # Mode auto: area bar yang sedang dikunci
        locked = None
        lock_seen = 0.0
        next_search = 0.0

        while not self.stop_event.is_set():

            # ---- Tentukan area bar & ambil frame
            frame = None

            if self.auto:

                if locked is None and time.time() >= next_search:
                    found = self.locate_bar_region()
                    next_search = time.time() + BITE_SEARCH_INTERVAL

                    if found is not None:
                        locked, val = found
                        lock_seen = time.time()
                        self.log(
                            f"[AUTO] bite.png ketemu (kemiripan {val:.2f}) -> "
                            f"area bar ({locked['left']}, {locked['top']}) "
                            f"{locked['width']}x{locked['height']}"
                        )

                if locked is not None:
                    frame = self.grab(
                        locked["left"], locked["top"],
                        locked["width"], locked["height"]
                    )
            else:
                frame = self.grab(
                    fixed["left"], fixed["top"],
                    fixed["width"], fixed["height"]
                )

            if frame is not None:
                info = detect(frame)
                preview = frame.copy()
            else:
                info = {"bar": None, "green_x": None, "marker_x": None}
                preview = np.zeros(
                    (max(1, int(fixed["height"])),
                     max(1, int(fixed["width"])), 3),
                    np.uint8
                )
                cv2.putText(
                    preview, "cari bite.png...", (5, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1
                )

            now = time.time()
            armed = now >= start_time

            bar = info["bar"]
            green_x = info["green_x"]
            marker_x = info["marker_x"]

            direction = None
            error = None

            # ---- Kunci area (mode auto): lepas kalau bar hilang lama
            if self.auto and locked is not None:
                if bar is not None:
                    lock_seen = now
                elif now - lock_seen > LOCK_LOST_TIME:
                    locked = None
                    next_search = 0.0
                    self.log("[AUTO] Bar hilang, area dilepas. Cari bite.png lagi.")

            if bar is not None:
                last_bar_time = now
                # Bar muncul = sudah mancing, batalkan klik yang tertunda
                self.pending_click_at = None

            if bar is not None and marker_x is not None:

                last_seen = now

                error = marker_x - green_x

                if error < -deadzone:
                    # Penanda di kiri hijau -> dorong ke kanan
                    direction = "RIGHT"
                elif error > deadzone:
                    # Penanda di kanan hijau -> dorong ke kiri
                    direction = "LEFT"
                else:
                    direction = "CENTER"

                last_direction = direction

            # ---- Keputusan: balance atau siklus mancing
            if not armed:
                self.release_all()
                status = "WAIT"

            elif direction is not None:
                self.set_direction(direction)
                status = direction

            elif (now - last_seen < GRACE_TIME
                  and bar is not None
                  and last_direction is not None):
                status = "HOLD"

            else:
                # Bar tidak ada -> lepas tombol, jalankan siklus mancing
                self.release_all()
                last_direction = None

                if (bar is None
                        and now - last_bar_time > BAR_LOST_TIME
                        and now >= self.next_cycle_check):

                    self.cycle_status = self.cycle_step()

                    if self.cycle_status in ("HABIS", "ENERGI"):
                        self.stop_reason = self.cycle_status
                        self.set_status(self.cycle_status)
                        break

                status = self.cycle_status

            # ---- Gambar preview
            if bar is not None:

                bx, by, bw, bh = bar

                cv2.rectangle(
                    preview, (bx, by), (bx + bw, by + bh), (255, 255, 0), 1
                )

                gx = int(round(green_x))
                cv2.line(
                    preview, (gx, by - 4), (gx, by + bh + 4), (0, 255, 0), 2
                )

                if marker_x is not None:
                    mx = int(round(marker_x))
                    cv2.line(
                        preview, (mx, by - 4), (mx, by + bh + 4),
                        (255, 0, 255), 2
                    )

            if error is not None:
                cv2.putText(
                    preview, f"ERR:{error:+.0f}", (5, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1
                )

            rgb_preview = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)

            cv2.putText(
                rgb_preview, status, (5, rgb_preview.shape[0] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                STATUS_COLORS.get(status, (255, 255, 255)), 2
            )

            self.set_preview(rgb_preview)

            # ---- Log: saat status berubah atau tiap 5 detik
            if status != last_status or now - last_log >= 5:

                if status != last_status:
                    self.set_status(status)

                last_status = status
                last_log = now

                self.log(
                    f"[{status}] bar={'ya' if bar else 'tidak'} "
                    f"marker={'ya' if marker_x is not None else 'tidak'} "
                    f"error={'-' if error is None else f'{error:+.0f}'}"
                )

            # Tanpa frame (mode auto, popup belum muncul): santai saja
            time.sleep(0.003 if frame is not None else 0.02)


# =====================================
# 6. OVERLAY SNIPPING / DROPLET / MULTI TITIK
# =====================================

class Overlay:
    """
    Layar penuh beku untuk memilih sesuatu.
      mode "region": drag area   -> {"region": {...}, "image": PIL.Image}
      mode "point" : klik 1 titik -> {"x": int, "y": int, "rgb": (r, g, b)}
      mode "points": klik banyak titik (count kali)
                                  -> {"points": [{"x": int, "y": int}, ...]}
      mode "bite"  : kalibrasi bite.png
                       1) drag HANYA tulisan judul (kotak MERAH)
                       2) bar pelangi dicari OTOMATIS di bawah judul.
                          Kalau gagal, drag kotak HIJAU di sekeliling bar.
                                  -> {"title": {"region", "image"},
                                      "bar": {"region", "image"},
                                      "auto": bool, "screen": PIL.Image,
                                      "origin": (ox, oy)}
    ESC = batal. Klik kanan = batal (mode points: selesai kalau sudah ada titik).
    Mode points: Enter = selesai, Backspace = undo titik terakhir.
    """

    def __init__(self, root, mode, hint, on_done, count=1):
        self.root = root
        self.mode = mode
        self.hint = hint
        self.on_done = on_done
        self.count = max(1, int(count))
        self.finished = False

        self.rect = None
        self.sx = self.sy = 0
        self.points = []
        self.hint_item = None
        self.hint_bg = None

        # khusus mode "bite"
        self.stage = 1
        self.title_res = None

        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)

        self.ox = mon["left"]
        self.oy = mon["top"]
        self.img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        self.top = tk.Toplevel(root)
        self.top.configure(bg="black")
        self.top.geometry(f"+{self.ox}+{self.oy}")
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-topmost", True)

        self.canvas = tk.Canvas(
            self.top, highlightthickness=0, bg="black", cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)

        self.top.bind("<Escape>", lambda e: self.finish(None))
        self.top.bind("<Return>", lambda e: self.finish_points())
        self.top.bind("<BackSpace>", lambda e: self.undo_point())
        self.top.focus_force()
        self.canvas.focus_set()

        self.top.after(300, self.setup)

    def hint_text(self):
        if self.mode == "bite":
            if self.stage == 1:
                return (
                    "Drag HANYA tulisan judul 'YOU HAVE A BITE!' (kotak MERAH). "
                    "Bar dicari otomatis    (ESC / klik kanan = batal)"
                )

            return (
                "Bar tidak ketemu otomatis. Sekarang drag kotak HIJAU "
                "di sekeliling bar pelangi    (ESC / klik kanan = batal)"
            )

        if self.mode == "points":
            return (
                f"{self.hint}  [{len(self.points)}/{self.count}]   "
                "Enter/klik kanan = selesai, Backspace = undo, ESC = batal"
            )

        return self.hint + "    (ESC / klik kanan = batal)"

    def update_hint(self):
        if self.hint_item is not None:
            self.canvas.itemconfigure(self.hint_item, text=self.hint_text())

    def setup(self):
        if self.finished:
            return

        self.top.update_idletasks()

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()

        if cw < 100 or ch < 100:
            cw, ch = self.img.size

        self.cw, self.ch = cw, ch

        if (cw, ch) == self.img.size:
            disp = self.img
        else:
            disp = self.img.resize((cw, ch), Image.LANCZOS)

        self.photo = ImageTk.PhotoImage(disp)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        # Bar petunjuk
        self.hint_bg = self.canvas.create_rectangle(
            0, 0, cw, 34, fill="black", outline=""
        )
        self.hint_item = self.canvas.create_text(
            cw // 2, 17, fill="white", font=("sans-serif", 11, "bold"),
            text=self.hint_text()
        )

        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Button-3>", self.on_right)

    def to_img(self, cx, cy):
        ix = int(round(cx * self.img.width / self.cw))
        iy = int(round(cy * self.img.height / self.ch))
        ix = max(0, min(self.img.width - 1, ix))
        iy = max(0, min(self.img.height - 1, iy))
        return ix, iy

    # ---- info koordinat + warna di dekat kursor

    def on_motion(self, e):
        if self.mode not in ("point", "points"):
            return

        ix, iy = self.to_img(e.x, e.y)
        rgb = self.img.getpixel((ix, iy))

        self.canvas.delete("info")

        w = 250
        px = e.x + 22
        py = e.y + 22

        if px + w > self.cw:
            px = e.x - w - 22

        if py + 36 > self.ch:
            py = e.y - 58

        self.canvas.create_rectangle(
            px, py, px + w, py + 34,
            fill="black", outline="white", tags="info"
        )
        self.canvas.create_rectangle(
            px + 4, py + 4, px + 30, py + 30,
            fill=rgb_to_hex(rgb), outline="white", tags="info"
        )
        self.canvas.create_text(
            px + 36, py + 17, anchor="w", fill="white",
            font=("monospace", 9),
            text=f"({ix + self.ox},{iy + self.oy}) {tuple(rgb)}",
            tags="info"
        )

    # ---- klik / drag

    def on_press(self, e):

        if self.mode == "point":
            ix, iy = self.to_img(e.x, e.y)
            rgb = self.img.getpixel((ix, iy))

            self.finish({
                "x": ix + self.ox,
                "y": iy + self.oy,
                "rgb": tuple(int(v) for v in rgb),
            })
            return

        if self.mode == "points":
            ix, iy = self.to_img(e.x, e.y)

            idx = len(self.points)
            self.points.append({"x": ix + self.ox, "y": iy + self.oy})

            tag = f"pt{idx}"
            r = 9

            self.canvas.create_oval(
                e.x - r, e.y - r, e.x + r, e.y + r,
                outline="#ff2d2d", width=2, tags=tag
            )
            self.canvas.create_line(
                e.x - r - 4, e.y, e.x + r + 4, e.y,
                fill="#ff2d2d", tags=tag
            )
            self.canvas.create_line(
                e.x, e.y - r - 4, e.x, e.y + r + 4,
                fill="#ff2d2d", tags=tag
            )
            self.canvas.create_text(
                e.x + r + 6, e.y - r - 6, text=str(idx + 1),
                fill="#ffff00", font=("sans-serif", 11, "bold"), tags=tag
            )

            self.update_hint()

            if len(self.points) >= self.count:
                self.finish({"points": list(self.points)})

            return

        # mode region / bite (kotak hijau untuk langkah bar)
        color = "#00ff66" if (self.mode == "bite" and self.stage == 2) else "#ff2d2d"

        self.sx, self.sy = e.x, e.y
        self.canvas.delete("sel")
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline=color, width=2, tags="sel"
        )

    def on_drag(self, e):
        if self.mode not in ("region", "bite") or self.rect is None:
            return

        self.canvas.coords(self.rect, self.sx, self.sy, e.x, e.y)

        self.canvas.delete("size")

        w = abs(e.x - self.sx) * self.img.width // max(1, self.cw)
        h = abs(e.y - self.sy) * self.img.height // max(1, self.ch)

        self.canvas.create_text(
            min(self.sx, e.x) + 4, max(40, min(self.sy, e.y) - 12),
            anchor="w", fill="#ffff00", font=("monospace", 10, "bold"),
            text=f"{w}x{h}", tags="size"
        )

    def auto_find_bar(self, tx, ty, tw, th):
        """Cari bar pelangi di bawah judul. Return dict bar atau None."""

        try:
            screen = cv2.cvtColor(np.array(self.img), cv2.COLOR_RGB2BGR)
            found = find_bar_near_title(screen, tx, ty, tw, th)
        except Exception:
            return None

        if found is None:
            return None

        bx, by, bw, bh = found
        W, H = self.img.size

        l = max(0, bx - BAR_PAD_X)
        t = max(0, by - BAR_PAD_Y)
        r = min(W, bx + bw + BAR_PAD_X)
        b = min(H, by + bh + BAR_PAD_Y)

        return {
            "region": {
                "left": l + self.ox,
                "top": t + self.oy,
                "width": r - l,
                "height": b - t,
            },
            "image": self.img.crop((l, t, r, b)),
        }

    def on_release(self, e):
        if self.mode not in ("region", "bite") or self.rect is None:
            return

        x1, x2 = sorted((self.sx, e.x))
        y1, y2 = sorted((self.sy, e.y))

        if (x2 - x1) < 5 or (y2 - y1) < 5:
            # Terlalu kecil, ulangi
            self.canvas.delete("sel")
            self.canvas.delete("size")
            self.rect = None
            return

        ix1, iy1 = self.to_img(x1, y1)
        ix2, iy2 = self.to_img(x2, y2)

        crop = self.img.crop((ix1, iy1, ix2, iy2))

        res = {
            "region": {
                "left": ix1 + self.ox,
                "top": iy1 + self.oy,
                "width": ix2 - ix1,
                "height": iy2 - iy1,
            },
            "image": crop,
        }

        if self.mode == "bite":

            if self.stage == 1:
                self.title_res = res

                # Coba cari bar otomatis di bawah judul
                bar = self.auto_find_bar(ix1, iy1, ix2 - ix1, iy2 - iy1)

                if bar is not None:
                    self.finish({
                        "title": res,
                        "bar": bar,
                        "auto": True,
                        "screen": self.img,
                        "origin": (self.ox, self.oy),
                    })
                    return

                # Gagal -> minta drag kotak hijau manual
                self.stage = 2

                self.canvas.delete("sel")
                self.canvas.delete("size")
                self.canvas.create_rectangle(
                    x1, y1, x2, y2, outline="#ff2d2d", width=2, tags="done1"
                )

                if self.hint_bg is not None:
                    self.canvas.itemconfigure(self.hint_bg, fill="#0a6b2a")

                self.rect = None
                self.update_hint()
                return

            self.finish({
                "title": self.title_res,
                "bar": res,
                "auto": False,
                "screen": self.img,
                "origin": (self.ox, self.oy),
            })
            return

        self.finish(res)

    # ---- khusus mode points

    def on_right(self, e):
        if self.mode == "points" and self.points:
            self.finish({"points": list(self.points)})
        else:
            self.finish(None)

    def finish_points(self):
        if self.mode == "points" and self.points:
            self.finish({"points": list(self.points)})

    def undo_point(self):
        if self.mode != "points" or not self.points:
            return

        idx = len(self.points) - 1
        self.canvas.delete(f"pt{idx}")
        self.points.pop()
        self.update_hint()

    def finish(self, result):
        if self.finished:
            return

        self.finished = True

        try:
            self.top.destroy()
        except Exception:
            pass

        self.on_done(result)


# =====================================
# 7. GUI UTAMA
# =====================================

class App:

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()

        self.bot = None
        self.q = queue.Queue()
        self.latest_preview = None
        self.preview_photo = None
        self.bite_prev_photo = None
        self.hotkey_listener = None
        self.hotkey_global = False
        self.last_toggle = 0.0

        # True saat sedang memilih area/titik/warna (SPASI diabaikan)
        self.busy = False

        root.title(f"{APP_TITLE} - (OFF)")
        root.geometry("680x720+10+10")
        root.minsize(520, 420)
        root.attributes("-topmost", True)

        self.tol_var = tk.IntVar(value=self.cfg["color_tolerance"])
        self.close_conf_var = tk.DoubleVar(value=self.cfg["close_confidence"])
        self.habis_conf_var = tk.DoubleVar(value=self.cfg["habis_confidence"])
        self.energi_conf_var = tk.DoubleVar(value=self.cfg["energi_confidence"])
        self.bite_conf_var = tk.DoubleVar(value=self.cfg["bite_confidence"])
        self.dead_var = tk.IntVar(value=self.cfg["deadzone"])
        self.click_count_var = tk.IntVar(value=self.cfg["click_count"])
        self.delay_min_var = tk.DoubleVar(value=self.cfg["click_delay_min"])
        self.delay_max_var = tk.DoubleVar(value=self.cfg["click_delay_max"])
        self.dry_var = tk.BooleanVar(value=False)
        self.info_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Siap")

        self.build_ui()
        self.refresh_labels()
        self.update_title(False)
        self.start_hotkey()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    # ---------- judul + indikator START / OFF ----------

    def update_title(self, running):
        if running:
            self.root.title(f"{APP_TITLE} - (START)")
            self.state_lbl.config(text=" START ", bg="#1e9e3a", fg="white")
        else:
            self.root.title(f"{APP_TITLE} - (OFF)")
            self.state_lbl.config(text=" OFF ", bg="#666666", fg="white")

    # ---------- hotkey SPASI (mulai / berhenti) ----------

    def start_hotkey(self):
        """
        SPASI = toggle MULAI / BERHENTI.
        Global kalau pynput ada, kalau tidak hanya saat GUI fokus.
        """

        try:
            from pynput import keyboard as pk
        except Exception:
            self.append_log(
                "[INFO] pynput belum terpasang: SPASI hanya bekerja saat "
                "jendela ini fokus. Pasang: pip install pynput"
            )
            self.root.bind_all("<space>", self.on_space_tk)
            return

        def on_press(key):
            try:
                if key != pk.Key.space:
                    return

                now = time.time()

                if now - self.last_toggle < HOTKEY_DEBOUNCE:
                    return

                self.last_toggle = now

                bot = self.bot

                if bot is not None and bot.is_alive():
                    # Berhenti secepat mungkin (langsung dari thread ini)
                    bot.stop_event.set()
                    self.q.put(("hotkey_stop", None))
                else:
                    self.q.put(("hotkey_start", None))
            except Exception:
                pass

        try:
            self.hotkey_listener = pk.Listener(on_press=on_press)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            self.hotkey_global = True
            self.append_log(
                "[INFO] Hotkey global aktif: SPASI = mulai / berhenti."
            )
        except Exception as e:
            self.hotkey_listener = None
            self.append_log(
                f"[INFO] Hotkey global gagal ({e!r}). SPASI hanya bekerja "
                "saat jendela ini fokus."
            )
            self.root.bind_all("<space>", self.on_space_tk)

    def on_space_tk(self, event=None):
        """Cadangan SPASI saat GUI fokus (kalau pynput tidak ada)."""

        w = getattr(event, "widget", None)

        # Jangan ganggu pengetikan / tombol yang sedang fokus
        if isinstance(
            w, (ttk.Button, ttk.Spinbox, ttk.Entry, tk.Text, tk.Listbox)
        ):
            return

        if self.busy:
            return

        now = time.time()

        if now - self.last_toggle < HOTKEY_DEBOUNCE:
            return

        self.last_toggle = now

        if self.bot_running():
            self.stop()
        else:
            self.start()

    # ---------- scroll mouse wheel ----------

    def bind_wheel(self, canvas):
        def on_wheel(e):
            if getattr(e, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(e, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            elif getattr(e, "delta", 0):
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def enter(_e):
            canvas.bind_all("<Button-4>", on_wheel)
            canvas.bind_all("<Button-5>", on_wheel)
            canvas.bind_all("<MouseWheel>", on_wheel)

        def leave(_e):
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)

    # ---------- UI ----------

    def build_ui(self):

        # ===== Bar kontrol ATAS (selalu kelihatan, tidak ikut scroll)
        top = ttk.Frame(self.root, padding=6)
        top.pack(side="top", fill="x")

        self.start_btn = ttk.Button(
            top, text="MULAI (SPASI)", command=self.start, takefocus=False
        )
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(
            top, text="BERHENTI (SPASI)", command=self.stop,
            state="disabled", takefocus=False
        )
        self.stop_btn.pack(side="left", padx=4)

        self.state_lbl = tk.Label(
            top, text=" OFF ", bg="#666666", fg="white",
            font=("sans-serif", 11, "bold")
        )
        self.state_lbl.pack(side="left", padx=6)

        ttk.Checkbutton(
            top, text="Mode uji (tanpa tekan tombol/klik)",
            variable=self.dry_var, takefocus=False
        ).pack(side="left", padx=6)

        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        ttk.Separator(self.root, orient="horizontal").pack(side="top", fill="x")

        # ===== Notebook
        nb = ttk.Notebook(self.root)
        nb.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 6))
        self.nb = nb

        setup_tab = ttk.Frame(nb)
        run = ttk.Frame(nb, padding=6)

        nb.add(setup_tab, text="1. Setup")
        nb.add(run, text="2. Jalan")

        # ----- Setup bisa di-scroll
        setup_canvas = tk.Canvas(setup_tab, highlightthickness=0)
        setup_scrollbar = ttk.Scrollbar(
            setup_tab, orient="vertical", command=setup_canvas.yview
        )
        setup_canvas.configure(yscrollcommand=setup_scrollbar.set)

        setup_scrollbar.pack(side="right", fill="y")
        setup_canvas.pack(side="left", fill="both", expand=True)

        setup = ttk.Frame(setup_canvas, padding=6)
        setup_window = setup_canvas.create_window(
            (0, 0), window=setup, anchor="nw"
        )

        setup.bind(
            "<Configure>",
            lambda e: setup_canvas.configure(
                scrollregion=setup_canvas.bbox("all")
            )
        )
        setup_canvas.bind(
            "<Configure>",
            lambda e: setup_canvas.itemconfigure(
                setup_window, width=e.width
            )
        )

        self.bind_wheel(setup_canvas)

        # ----- AUTO-LOKASI bar via bite.png
        f = ttk.LabelFrame(
            setup, text="AUTO-LOKASI bar via bite.png (disarankan)",
            padding=6
        )
        f.pack(fill="x", pady=3)

        self.bite_lbl = ttk.Label(f, text="")
        self.bite_lbl.grid(row=0, column=0, columnspan=3, sticky="w")

        self.bite_off_lbl = ttk.Label(f, text="", wraplength=540)
        self.bite_off_lbl.grid(row=1, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Button(
            btns, text="Kalibrasi bite.png (drag judul)",
            command=self.pick_bite
        ).pack(side="left")
        ttk.Button(
            btns, text="Tes cari bite.png (3 dtk)", command=self.test_bite
        ).pack(side="left", padx=4)
        ttk.Button(
            btns, text="Matikan auto", command=self.disable_auto
        ).pack(side="left")

        cf = ttk.Frame(f)
        cf.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(cf, text="Confidence bite.png:").pack(side="left")
        ttk.Spinbox(
            cf, from_=0.5, to=0.99, increment=0.01, width=5,
            textvariable=self.bite_conf_var
        ).pack(side="left", padx=4)

        ttk.Label(
            f,
            text=(
                "Cara: munculkan popup 'YOU HAVE A BITE!', klik Kalibrasi, lalu "
                "drag HANYA tulisan judulnya (kotak merah). Bar pelangi dicari "
                "otomatis. Kalau gagal, baru diminta drag kotak hijau di "
                "sekeliling bar."
            ),
            wraplength=540, foreground="#555555"
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.bite_prev_lbl = ttk.Label(f, text="")
        self.bite_prev_lbl.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

        f.columnconfigure(0, weight=1)

        # ----- Area bar tetap (cadangan)
        f = ttk.LabelFrame(
            setup,
            text="Area bar TETAP (cadangan, dipakai kalau auto mati)",
            padding=6
        )
        f.pack(fill="x", pady=3)

        self.region_lbl = ttk.Label(f, text="")
        self.region_lbl.pack(side="left")

        ttk.Button(
            f, text="Pilih area (drag)", command=self.pick_region
        ).pack(side="right")

        # ----- ROD
        f = ttk.LabelFrame(
            setup, text="Tombol ROD (warna = sudah / belum mancing)",
            padding=6
        )
        f.pack(fill="x", pady=3)

        self.rod_lbl = ttk.Label(f, text="")
        self.rod_lbl.grid(row=0, column=0, sticky="w")

        ttk.Button(
            f, text="Pilih titik + warna BELUM mancing",
            command=self.pick_rod
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.idle_sw = tk.Label(f, width=4, bg="#444444", relief="solid", bd=1)
        self.idle_sw.grid(row=1, column=0, sticky="w", pady=2)
        self.idle_lbl = ttk.Label(f, text="")
        self.idle_lbl.grid(row=1, column=0, sticky="w", padx=(40, 0))

        ttk.Button(
            f, text="Ambil warna SEDANG mancing (5 dtk)",
            command=self.pick_fishing_color
        ).grid(row=2, column=1, sticky="e", padx=(8, 0), pady=2)

        self.fish_sw = tk.Label(f, width=4, bg="#444444", relief="solid", bd=1)
        self.fish_sw.grid(row=2, column=0, sticky="w", pady=2)
        self.fish_lbl = ttk.Label(f, text="")
        self.fish_lbl.grid(row=2, column=0, sticky="w", padx=(40, 0))

        ttk.Button(
            f, text="Tes warna sekarang", command=self.test_color
        ).grid(row=3, column=1, sticky="e", padx=(8, 0), pady=2)

        tf = ttk.Frame(f)
        tf.grid(row=3, column=0, sticky="w")
        ttk.Label(tf, text="Toleransi warna:").pack(side="left")
        ttk.Spinbox(
            tf, from_=5, to=150, width=5, textvariable=self.tol_var
        ).pack(side="left", padx=4)

        f.columnconfigure(0, weight=1)

        # ----- Lokasi klik (banyak titik, dipilih acak) + delay
        f = ttk.LabelFrame(
            setup, text="Lokasi klik lempar pancing (dipilih ACAK)",
            padding=6
        )
        f.pack(fill="x", pady=3)

        self.click_lbl = ttk.Label(f, text="")
        self.click_lbl.pack(anchor="w")

        body = ttk.Frame(f)
        body.pack(fill="x", pady=(4, 0))

        lf = ttk.Frame(body)
        lf.pack(side="left", fill="both", expand=True)

        self.click_list = tk.Listbox(
            lf, height=6, exportselection=False, selectmode="extended"
        )
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.click_list.yview)
        self.click_list.config(yscrollcommand=sb.set)
        self.click_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bf = ttk.Frame(body)
        bf.pack(side="right", fill="y", padx=(8, 0))

        cf = ttk.Frame(bf)
        cf.pack(fill="x", pady=(0, 4))
        ttk.Label(cf, text="Jumlah titik:").pack(side="left")
        ttk.Spinbox(
            cf, from_=1, to=50, width=4, textvariable=self.click_count_var
        ).pack(side="left", padx=4)

        ttk.Button(
            bf, text="Pilih N titik (ganti semua)",
            command=lambda: self.pick_clicks(True)
        ).pack(fill="x", pady=1)
        ttk.Button(
            bf, text="Tambah N titik",
            command=lambda: self.pick_clicks(False)
        ).pack(fill="x", pady=1)
        ttk.Button(
            bf, text="Hapus terpilih", command=self.delete_selected_clicks
        ).pack(fill="x", pady=1)
        ttk.Button(
            bf, text="Hapus semua", command=self.clear_clicks
        ).pack(fill="x", pady=1)

        df = ttk.Frame(f)
        df.pack(fill="x", pady=(6, 0))
        ttk.Label(df, text="Delay sebelum klik (acak):").pack(side="left")
        ttk.Spinbox(
            df, from_=0, to=60, increment=0.1, width=5,
            textvariable=self.delay_min_var
        ).pack(side="left", padx=(6, 2))
        ttk.Label(df, text="sampai").pack(side="left")
        ttk.Spinbox(
            df, from_=0, to=60, increment=0.1, width=5,
            textvariable=self.delay_max_var
        ).pack(side="left", padx=(2, 2))
        ttk.Label(df, text="detik (0 dan 0 = tanpa delay)").pack(side="left")

        # ----- Gambar
        f = ttk.LabelFrame(
            setup, text="Gambar (snipping) + confidence", padding=6
        )
        f.pack(fill="x", pady=3)

        self.close_lbl = ttk.Label(f, text="")
        self.close_lbl.grid(row=0, column=0, sticky="w")
        ttk.Button(
            f, text="Ambil close.png",
            command=lambda: self.snip_image("close")
        ).grid(row=0, column=1, padx=4, pady=2)
        ttk.Spinbox(
            f, from_=0.5, to=0.99, increment=0.01, width=5,
            textvariable=self.close_conf_var
        ).grid(row=0, column=2)

        self.habis_lbl = ttk.Label(f, text="")
        self.habis_lbl.grid(row=1, column=0, sticky="w")
        ttk.Button(
            f, text="Ambil habis.png",
            command=lambda: self.snip_image("habis")
        ).grid(row=1, column=1, padx=4, pady=2)
        ttk.Spinbox(
            f, from_=0.5, to=0.99, increment=0.01, width=5,
            textvariable=self.habis_conf_var
        ).grid(row=1, column=2)

        self.energi_lbl = ttk.Label(f, text="")
        self.energi_lbl.grid(row=2, column=0, sticky="w")
        ttk.Button(
            f, text="Ambil energi_habis.png",
            command=lambda: self.snip_image("energi")
        ).grid(row=2, column=1, padx=4, pady=2)
        ttk.Spinbox(
            f, from_=0.5, to=0.99, increment=0.01, width=5,
            textvariable=self.energi_conf_var
        ).grid(row=2, column=2)

        f.columnconfigure(0, weight=1)

        # ----- Balance
        f = ttk.LabelFrame(setup, text="Balance", padding=6)
        f.pack(fill="x", pady=3)

        ttk.Label(
            f, text="Deadzone (piksel). Naik = kurang goyang, turun = lebih responsif:"
        ).pack(side="left")
        ttk.Spinbox(
            f, from_=0, to=40, width=5, textvariable=self.dead_var
        ).pack(side="left", padx=4)

        # ----- Info
        ttk.Label(
            setup, textvariable=self.info_var,
            wraplength=540, foreground="#0a5"
        ).pack(fill="x", pady=4)

        # ----- Tab Jalan
        self.preview_lbl = ttk.Label(run, text="(preview muncul saat bot jalan)")
        self.preview_lbl.pack(pady=4)

        self.log_text = tk.Text(run, height=10, width=58, state="normal")
        self.log_text.pack(fill="both", expand=True)

    # ---------- label refresh ----------

    def set_color_row(self, swatch, label, rgb, empty_text):
        if rgb is None:
            swatch.config(bg="#444444")
            label.config(text=empty_text)
        else:
            swatch.config(bg=rgb_to_hex(rgb))
            label.config(text=f"RGB {tuple(rgb)}")

    def image_text(self, name, path):
        if os.path.exists(path):
            try:
                w, h = Image.open(path).size
                return f"{name}: ada ({w}x{h})"
            except Exception:
                return f"{name}: ada"
        return f"{name}: BELUM ada"

    def show_bite_preview(self):
        """Tampilkan preview kalibrasi (kotak merah = judul, hijau = area bar)."""

        img = None

        if offset_ok(self.cfg.get("bite_offset")) and os.path.exists(BITE_PREVIEW_FILE):
            try:
                img = Image.open(BITE_PREVIEW_FILE).convert("RGB")
            except Exception:
                img = None

        if img is None:
            self.bite_prev_photo = None
            self.bite_prev_lbl.config(
                image="",
                text=(
                    "(preview kalibrasi muncul di sini: "
                    "kotak MERAH = bite.png, kotak HIJAU = area bar)"
                )
            )
            return

        w, h = img.size

        if w > BITE_PREVIEW_WIDTH:
            img = img.resize((BITE_PREVIEW_WIDTH, max(1, int(h * BITE_PREVIEW_WIDTH / w))))

        self.bite_prev_photo = ImageTk.PhotoImage(img)
        self.bite_prev_lbl.config(image=self.bite_prev_photo, text="")

    def refresh_labels(self):
        r = self.cfg["region"]
        self.region_lbl.config(
            text=f"x={r['left']} y={r['top']}  ukuran {r['width']}x{r['height']}"
        )

        # ---- status auto-lokasi
        self.bite_lbl.config(text=self.image_text("bite.png", BITE_FILE))

        off = self.cfg.get("bite_offset")
        has_file = os.path.exists(BITE_FILE)

        if offset_ok(off):
            if has_file:
                self.bite_off_lbl.config(
                    text=(
                        f"Auto: AKTIF  (bar = bite + ({off['dx']}, {off['dy']}), "
                        f"ukuran {off['width']}x{off['height']})"
                    )
                )
            else:
                self.bite_off_lbl.config(
                    text="Auto: offset ada, tapi bite.png belum ada -> pakai area tetap"
                )
        elif isinstance(off, dict):
            self.bite_off_lbl.config(
                text=(
                    f"Auto: offset TIDAK VALID (ukuran {off.get('width')}x"
                    f"{off.get('height')} terlalu kecil). Kalibrasi ulang. "
                    "Sementara pakai area tetap."
                )
            )
        else:
            self.bite_off_lbl.config(
                text="Auto: MATI (belum dikalibrasi) -> pakai area tetap"
            )

        self.show_bite_preview()

        rod = self.cfg["rod"]
        self.rod_lbl.config(text=f"Titik ROD: ({rod['x']}, {rod['y']})")

        self.set_color_row(
            self.idle_sw, self.idle_lbl, self.cfg["color_idle"],
            "belum diambil (pakai aturan oranye lama)"
        )
        self.set_color_row(
            self.fish_sw, self.fish_lbl, self.cfg["color_fishing"],
            "belum diambil (opsional, tapi disarankan)"
        )

        pts = self.cfg.get("click_points") or []

        if pts:
            self.click_lbl.config(
                text=f"{len(pts)} titik tersimpan. Tiap lempar dipilih acak."
            )
        else:
            self.click_lbl.config(
                text="Belum ada titik. Bot klik di titik ROD "
                     f"({rod['x']}, {rod['y']})."
            )

        self.click_list.delete(0, "end")

        for i, p in enumerate(pts):
            self.click_list.insert("end", f"{i + 1}. ({p['x']}, {p['y']})")

        self.close_lbl.config(text=self.image_text("close.png", CLOSE_FILE))
        self.habis_lbl.config(text=self.image_text("habis.png", HABIS_FILE))
        self.energi_lbl.config(
            text=self.image_text("energi_habis.png", ENERGI_FILE)
        )

    # ---------- util ----------

    def bot_running(self):
        return self.bot is not None and self.bot.is_alive()

    def collect_vars(self):
        try:
            self.cfg["color_tolerance"] = int(self.tol_var.get())
            self.cfg["close_confidence"] = float(self.close_conf_var.get())
            self.cfg["habis_confidence"] = float(self.habis_conf_var.get())
            self.cfg["energi_confidence"] = float(self.energi_conf_var.get())
            self.cfg["bite_confidence"] = float(self.bite_conf_var.get())
            self.cfg["deadzone"] = int(self.dead_var.get())
            self.cfg["click_count"] = max(
                1, min(50, int(self.click_count_var.get()))
            )

            lo = max(0.0, float(self.delay_min_var.get()))
            hi = max(0.0, float(self.delay_max_var.get()))

            if lo > hi:
                lo, hi = hi, lo

            self.delay_min_var.set(round(lo, 2))
            self.delay_max_var.set(round(hi, 2))

            self.cfg["click_delay_min"] = round(lo, 2)
            self.cfg["click_delay_max"] = round(hi, 2)

        except (tk.TclError, ValueError):
            messagebox.showerror(
                "Input salah",
                "Ada angka yang tidak valid di toleransi / confidence / "
                "deadzone / jumlah titik / delay."
            )
            return False

        save_config(self.cfg)
        return True

    def run_overlay(self, mode, hint, callback, count=1):
        if self.bot_running():
            messagebox.showinfo("Bot sedang jalan", "Hentikan bot dulu.")
            return

        if not self.collect_vars():
            return

        self.busy = True
        self.root.withdraw()

        def open_overlay():
            try:
                Overlay(
                    self.root, mode, hint,
                    lambda res: self._overlay_done(res, callback),
                    count=count
                )
            except Exception as e:
                self.busy = False
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                messagebox.showerror("Gagal membuka overlay", repr(e))

        self.root.after(350, open_overlay)

    def _overlay_done(self, res, callback):
        self.busy = False
        self.root.deiconify()
        self.root.attributes("-topmost", True)

        if res is not None:
            callback(res)
        else:
            self.info_var.set("Dibatalkan.")

    def countdown(self, secs, text, done):
        if secs > 0:
            self.info_var.set(f"{text} {secs}...")
            self.root.after(
                1000, lambda: self.countdown(secs - 1, text, done)
            )
        else:
            done()

    # ---------- aksi setup ----------

    def pick_region(self):
        self.run_overlay(
            "region",
            "Munculkan bar mancing di game, lalu DRAG area bar pelangi",
            self._on_region
        )

    def _on_region(self, res):
        self.cfg["region"] = res["region"]
        save_config(self.cfg)
        self.refresh_labels()
        r = res["region"]
        self.info_var.set(
            f"Area bar tetap tersimpan: {r['width']}x{r['height']} di ({r['left']}, {r['top']})"
        )

    # ---------- auto-lokasi (bite.png) ----------

    def pick_bite(self):
        self.run_overlay("bite", "", self._on_bite)

    def _on_bite(self, res):
        title = res["title"]
        bar = res["bar"]
        screen = res["screen"]
        ox, oy = res["origin"]

        t = title["region"]
        b = bar["region"]

        # Validasi: kotak area bar HARUS berisi bar pelangi
        if not bar_in_region(screen, b, ox, oy):
            messagebox.showerror(
                "Bar pelangi tidak terdeteksi",
                "Area bar yang dipilih tidak berisi bar pelangi.\n\n"
                "Pastikan popup 'YOU HAVE A BITE!' terlihat di layar beku, "
                "drag judulnya (kotak merah), dan kalau diminta, drag kotak "
                "hijau yang melingkupi seluruh bar pelangi.\n\n"
                "Setelan lama TIDAK diubah."
            )
            self.info_var.set("Kalibrasi dibatalkan: bar pelangi tidak terdeteksi.")
            return

        try:
            title["image"].save(BITE_FILE)
        except Exception as e:
            messagebox.showerror("Gagal simpan bite.png", repr(e))
            return

        # Offset bar terhadap pojok kiri atas bite.png (dari screenshot yang sama)
        self.cfg["bite_offset"] = {
            "dx": int(b["left"] - t["left"]),
            "dy": int(b["top"] - t["top"]),
            "width": int(b["width"]),
            "height": int(b["height"]),
        }

        # Area tetap ikut diperbarui sebagai cadangan
        self.cfg["region"] = b

        # Preview: kotak merah (judul) + hijau (area bar)
        try:
            make_bite_preview(screen, t, b, ox, oy).save(BITE_PREVIEW_FILE)
        except Exception:
            pass

        save_config(self.cfg)
        self.refresh_labels()

        off = self.cfg["bite_offset"]
        cara = "OTOMATIS" if res.get("auto") else "MANUAL"

        self.info_var.set(
            f"Kalibrasi {cara} selesai: bite.png {t['width']}x{t['height']}, "
            f"bar = bite + ({off['dx']}, {off['dy']}) ukuran "
            f"{off['width']}x{off['height']}. Cek preview, lalu 'Tes cari bite.png'."
        )

    def test_bite(self):
        if not self.collect_vars():
            return

        if not os.path.exists(BITE_FILE):
            self.info_var.set("bite.png belum ada. Klik Kalibrasi dulu.")
            return

        self.busy = True
        self.countdown(
            3,
            "Munculkan popup 'YOU HAVE A BITE!' di game. Tes dalam",
            self._do_test_bite
        )

    def _do_test_bite(self):
        self.busy = False

        tpl = cv2.imread(BITE_FILE, cv2.IMREAD_COLOR)

        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                screen = cv2.cvtColor(
                    np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR
                )
        except Exception as e:
            messagebox.showerror("Gagal ambil layar", repr(e))
            return

        val, loc = match_template_img(screen, tpl)
        conf = self.cfg["bite_confidence"]

        if loc is None:
            self.info_var.set("bite.png tidak bisa dicocokkan (gambar tidak valid).")
            return

        gx = mon["left"] + loc[0]
        gy = mon["top"] + loc[1]

        if val >= conf:
            text = (
                f"KETEMU: kemiripan {val:.2f} (batas {conf:.2f}) "
                f"di ({gx}, {gy})"
            )

            off = self.cfg.get("bite_offset")

            if offset_ok(off):
                text += (
                    f" -> area bar ({gx + off['dx']}, {gy + off['dy']}) "
                    f"{off['width']}x{off['height']}"
                )
        else:
            text = (
                f"TIDAK ketemu: kemiripan tertinggi {val:.2f} "
                f"< batas {conf:.2f} (terbaik di ({gx}, {gy}))"
            )

        self.info_var.set(text)

    def disable_auto(self):
        self.cfg["bite_offset"] = None
        save_config(self.cfg)
        self.refresh_labels()
        self.info_var.set("Auto-lokasi dimatikan. Bot pakai area bar tetap.")

    # ---------- ROD ----------

    def pick_rod(self):
        self.run_overlay(
            "point",
            "Saat BELUM mancing: klik tepat di tombol ROD",
            self._on_rod
        )

    def _on_rod(self, res):
        self.cfg["rod"] = {"x": res["x"], "y": res["y"]}
        self.cfg["color_idle"] = list(res["rgb"])
        save_config(self.cfg)
        self.refresh_labels()
        self.info_var.set(
            f"Titik ROD ({res['x']}, {res['y']}) dan warna BELUM mancing "
            f"{res['rgb']} tersimpan."
        )

    def pick_fishing_color(self):
        if self.bot_running():
            messagebox.showinfo("Bot sedang jalan", "Hentikan bot dulu.")
            return

        self.collect_vars()
        self.busy = True
        self.countdown(
            5,
            "Lempar pancing di game (jadi SEDANG mancing). Warna dibaca dalam",
            self._read_fishing
        )

    def _read_fishing(self):
        self.busy = False

        rod = self.cfg["rod"]

        try:
            rgb = read_pixel_once(rod["x"], rod["y"])
        except Exception as e:
            messagebox.showerror("Gagal baca warna", repr(e))
            return

        self.cfg["color_fishing"] = list(rgb)
        save_config(self.cfg)
        self.refresh_labels()

        idle = self.cfg.get("color_idle")

        if idle is not None and color_dist(rgb, idle) <= self.cfg["color_tolerance"]:
            messagebox.showwarning(
                "Warna mirip",
                "Warna SEDANG mancing hampir sama dengan BELUM mancing.\n"
                "Mungkin game belum berubah saat dibaca. Coba ambil ulang."
            )

        self.info_var.set(f"Warna SEDANG mancing tersimpan: {rgb}")

    def test_color(self):
        if not self.collect_vars():
            return

        rod = self.cfg["rod"]

        try:
            rgb = read_pixel_once(rod["x"], rod["y"])
        except Exception as e:
            messagebox.showerror("Gagal baca warna", repr(e))
            return

        if rod_is_idle(rgb, self.cfg):
            state = "BELUM mancing (bot akan klik)"
        else:
            state = "SEDANG mancing / bukan idle (bot menunggu)"

        self.info_var.set(
            f"Warna di ({rod['x']}, {rod['y']}) = RGB {rgb} -> {state}"
        )

    # ---------- lokasi klik (banyak titik) ----------

    def pick_clicks(self, replace):
        try:
            n = int(self.click_count_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Input salah", "Jumlah titik harus angka.")
            return

        n = max(1, min(50, n))
        self.click_count_var.set(n)

        if replace:
            hint = f"BUAT ULANG: klik {n} lokasi lempar pancing yang berbeda"
        else:
            hint = f"TAMBAH: klik {n} lokasi lempar pancing lagi"

        self.run_overlay(
            "points", hint,
            lambda res: self._on_clicks(res, replace),
            count=n
        )

    def _on_clicks(self, res, replace):
        new_pts = res["points"]

        if replace:
            self.cfg["click_points"] = new_pts
        else:
            self.cfg["click_points"] = (
                list(self.cfg.get("click_points") or []) + new_pts
            )

        save_config(self.cfg)
        self.refresh_labels()

        total = len(self.cfg["click_points"])
        self.info_var.set(
            f"{len(new_pts)} titik "
            f"{'menggantikan daftar lama' if replace else 'ditambahkan'}. "
            f"Total sekarang {total} titik."
        )

    def delete_selected_clicks(self):
        sel = list(self.click_list.curselection())

        if not sel:
            self.info_var.set("Pilih dulu titik di daftar yang mau dihapus.")
            return

        pts = list(self.cfg.get("click_points") or [])

        for i in sorted(sel, reverse=True):
            if 0 <= i < len(pts):
                pts.pop(i)

        self.cfg["click_points"] = pts
        save_config(self.cfg)
        self.refresh_labels()
        self.info_var.set(f"{len(sel)} titik dihapus. Sisa {len(pts)} titik.")

    def clear_clicks(self):
        if not (self.cfg.get("click_points") or []):
            return

        if not messagebox.askyesno(
            "Hapus semua titik", "Hapus semua titik lokasi klik?"
        ):
            return

        self.cfg["click_points"] = []
        save_config(self.cfg)
        self.refresh_labels()
        self.info_var.set("Semua titik dihapus. Bot akan klik di titik ROD.")

    # ---------- gambar ----------

    def snip_image(self, which):
        if which == "close":
            path = CLOSE_FILE
            hint = "Munculkan popup/tombol CLOSE di game, lalu DRAG tombolnya"
        elif which == "habis":
            path = HABIS_FILE
            hint = "Munculkan tanda UMPAN HABIS di game, lalu DRAG tulisan/ikonnya"
        else:
            path = ENERGI_FILE
            hint = "Munculkan tanda ENERGI HABIS di game, lalu DRAG tulisan/ikonnya"

        def done(res):
            try:
                res["image"].save(path)
            except Exception as e:
                messagebox.showerror("Gagal simpan gambar", repr(e))
                return

            self.refresh_labels()
            w, h = res["image"].size
            self.info_var.set(
                f"{os.path.basename(path)} tersimpan ({w}x{h}) di {BASE_DIR}"
            )

        self.run_overlay("region", hint, done)

    # ---------- bot ----------

    def set_preview(self, arr):
        self.latest_preview = arr

    def start(self):
        if self.bot_running() or self.busy:
            return

        if not self.collect_vars():
            return

        self.bot = FishingBot(
            copy.deepcopy(self.cfg),
            self.dry_var.get(),
            log=lambda m: self.q.put(("log", m)),
            set_status=lambda s: self.q.put(("status", s)),
            set_preview=self.set_preview,
            on_finish=lambda r: self.q.put(("done", r)),
        )

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Berjalan... (SPASI = berhenti)")
        self.update_title(True)
        self.nb.select(1)
        self.root.focus_set()

        self.bot.start()

    def stop(self):
        if self.bot_running():
            self.bot.stop_event.set()
            self.status_var.set("Menghentikan...")
            self.root.focus_set()

    def on_bot_done(self, reason):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"Berhenti: {reason}")
        self.update_title(False)
        self.append_log(f"[BERHENTI] {reason}")

        if reason == "HABIS":
            messagebox.showinfo(
                "Umpan habis", "Umpan habis, program dihentikan."
            )
        elif reason == "ENERGI":
            messagebox.showinfo(
                "Energi habis", "Energi habis, program dihentikan."
            )

    # ---------- polling (thread -> GUI) ----------

    def append_log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

        lines = int(self.log_text.index("end-1c").split(".")[0])

        if lines > 300:
            self.log_text.delete("1.0", "60.0")

    def show_preview(self, arr):
        h, w = arr.shape[:2]
        pw = PREVIEW_WIDTH
        ph = max(1, int(pw * h / w))

        img = Image.fromarray(arr).resize((pw, ph))
        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_lbl.config(image=self.preview_photo, text="")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()

                if kind == "log":
                    self.append_log(val)
                elif kind == "status":
                    self.status_var.set(val)
                elif kind == "hotkey_stop":
                    self.status_var.set("Menghentikan (SPASI)...")
                    self.append_log("[SPASI] Tombol berhenti ditekan.")
                elif kind == "hotkey_start":
                    if not self.busy and not self.bot_running():
                        self.append_log("[SPASI] Tombol mulai ditekan.")
                        self.start()
                elif kind == "done":
                    self.on_bot_done(val)
        except queue.Empty:
            pass

        if self.latest_preview is not None:
            arr = self.latest_preview
            self.latest_preview = None

            try:
                self.show_preview(arr)
            except Exception:
                pass

        self.root.after(40, self.poll)

    def on_close(self):
        if self.bot_running():
            self.bot.stop_event.set()
            self.bot.join(timeout=2)

        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass

        try:
            self.collect_vars()
        except Exception:
            pass

        self.root.destroy()


# =====================================
# 8. MAIN
# =====================================

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
