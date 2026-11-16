#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大鲸鱼下载器 —— PyQt5 桌面版（多线程 / 断点续传 / SHA256 校验）。

直接运行： pythonw main.py
"""
import hashlib
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PyQt5.QtCore import QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QProgressBar, QPushButton, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)


class SegStrip(QWidget):
    """一条横条里画出 N 个分段的小进度块，宽度永远跟着容器走。"""

    def __init__(self, parent=None):
        super(SegStrip, self).__init__(parent)
        self.values = [0.0]
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_count(self, n):
        self.values = [0.0] * max(1, int(n))
        self.update()

    def set_values(self, vals):
        self.values = [max(0.0, min(1.0, float(v))) for v in vals] or [0.0]
        self.update()

    def paintEvent(self, _ev):
        n = len(self.values)
        if n <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        gap = 4.0 if n > 1 else 0.0
        w = (self.width() - gap * (n - 1)) / float(n)
        h = float(self.height())
        if w <= 0.5:
            return
        r = min(4.0, w / 2.0, h / 2.0)
        for i, v in enumerate(self.values):
            x = i * (w + gap)
            p.setBrush(QColor("#1b2540"))
            p.drawRoundedRect(QRectF(x, 0, w, h), r, r)
            if v > 0.002:
                fw = w * v
                p.setBrush(QColor("#4c8dff"))
                p.drawRoundedRect(QRectF(x, 0, max(fw, r * 2 if fw > 0 else 0), h), r, r)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BUF = 256 * 1024
MIN_CHUNK = 4 * 1024 * 1024
DEFAULT_DIR = r"D:\下载"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "下载日志.txt")


class RangeUnsupported(Exception):
    pass


class FatalHTTP(Exception):
    """4xx 这类重试也没用的错误，直接停下并给出人话提示。"""

    def __init__(self, code):
        Exception.__init__(self, "HTTP %s" % code)
        self.code = code


# 这些错误重试也没用，直接停
HTTP_HINTS = {
    400: "服务器拒绝了请求（HTTP 400）——检查链接是否完整",
    401: "需要登录或授权（HTTP 401）",
    404: "文件不存在（HTTP 404）——链接可能已经失效",
    405: "服务器不允许这种请求（HTTP 405）",
    410: "文件已被删除（HTTP 410）",
}

# 这些多半是反爬/限流，退避后可以再试
THROTTLE_CODES = (403, 429, 503)

THROTTLE_HINT = ("服务器拒绝了连接（HTTP %s）：这多半是反爬或限流，不是链接坏了。"
                 "等一两分钟再点「开始下载」，已经下好的分片会接着用。")


class Throttled(Exception):
    """403/429/503：多半是限流，退避后还能继续。"""

    def __init__(self, code):
        Exception.__init__(self, "HTTP %s" % code)
        self.code = code


def http_hint(code):
    if code in HTTP_HINTS:
        return HTTP_HINTS[code]
    if code in THROTTLE_CODES:
        return THROTTLE_HINT % code
    return "服务器返回 HTTP %s" % code


def log_line(msg):
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, msg))
    except Exception:
        pass


# --------------------------------------------------------------------------- 工具
def prefer_ipv4():
    """部分站点 IPv6 黑洞会让 urllib 卡 9 秒，这里强制走 IPv4。"""
    orig = socket.getaddrinfo

    def gai(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return orig(host, port, socket.AF_INET, type, proto, flags)
        except socket.gaierror:
            return orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = gai


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0


def eta_text(sec):
    if not sec or sec <= 0 or sec != sec or sec > 86400 * 3:
        return "—"
    s = int(sec)
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    return "%02d:%02d" % (s // 60, s % 60)


def safe_name(name, fallback="download.bin"):
    name = os.path.basename((name or "").strip())
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or fallback


def opener_for(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def extra_headers(url):
    """有些 CDN 缺 Referer / Accept 会直接 403。"""
    p = urlparse(url)
    return {
        "Referer": "%s://%s/" % (p.scheme, p.netloc),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def probe(url, proxy=""):
    """返回 (size, supports_range, filename)；size=-1 表示未知。"""
    filename = safe_name(os.path.basename(urlparse(url).path) or "download.bin")
    op = opener_for(proxy)
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        for _k, _v in extra_headers(url).items():
            req.add_header(_k, _v)
        req.add_header("Range", "bytes=0-0")
        with op.open(req, timeout=20) as resp:
            if getattr(resp, "status", None) == 206:
                m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
                if m:
                    return int(m.group(1)), True, filename
            cl = resp.headers.get("Content-Length")
            if cl:
                return int(cl), False, filename
    except urllib.error.HTTPError as exc:
        if exc.code in HTTP_HINTS:
            raise FatalHTTP(exc.code)
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", UA)
        for _k, _v in extra_headers(url).items():
            req.add_header(_k, _v)
        with op.open(req, timeout=20) as resp:
            size = int(resp.headers.get("Content-Length") or -1)
            ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            if size > 0:
                return size, ranges, filename
    except urllib.error.HTTPError as exc:
        if exc.code in HTTP_HINTS:
            raise FatalHTTP(exc.code)
        if exc.code in THROTTLE_CODES:
            raise Throttled(exc.code)
    except Exception:
        pass
    return -1, False, filename


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(4 * 1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# --------------------------------------------------------------------------- 下载线程
class Downloader(QThread):
    sig_phase = pyqtSignal(str)
    sig_segments = pyqtSignal(int)
    sig_done = pyqtSignal(str, str)
    sig_fail = pyqtSignal(str)

    def __init__(self, url, out_path, threads, proxy="", size=-1, ranges=False, parent=None):
        super(Downloader, self).__init__(parent)
        self.url = url
        self.out = out_path
        self.threads = max(1, min(int(threads or 8), 32))
        self.proxy = proxy or ""
        self.parts_dir = out_path + ".parts"
        self.total = int(size) if size and int(size) > 0 else -1
        self.ranges = bool(ranges) if ranges is not None else False
        self.probed = self.total > 0 and self.ranges
        self.segments = []
        self.received = 0
        self.state = "idle"
        self.error = ""
        self.sha = ""
        self.fatal = None
        self._throttle_code = None
        self._throttle_hits = 0
        self._worker_error = None
        self._cancel = threading.Event()

    # ---- 外部调用 ----
    def cancel(self):
        self._cancel.set()

    def _drop_parts(self):
        try:
            for name in os.listdir(self.parts_dir):
                try:
                    os.remove(os.path.join(self.parts_dir, name))
                except OSError:
                    pass
            os.rmdir(self.parts_dir)
        except OSError:
            pass
        try:
            os.remove(self.out + ".parts_single")
        except OSError:
            pass

    # ---- 主流程 ----
    def run(self):
        try:
            if not self.probed:
                self.sig_phase.emit("探测远端…")
                for _try in range(3):
                    try:
                        size, ranges, _ = probe(self.url, self.proxy)
                        break
                    except Throttled:
                        if _try >= 2:
                            raise
                        log_line("探测被限流，6 秒后重试")
                        self.sig_phase.emit("探测被限流，稍等重试…")
                        time.sleep(6)
                self.total, self.ranges = size, ranges
            self.state = "downloading"
            self.sig_phase.emit("连接中…")
            relaxed = False
            for round_ in range(2):
                try:
                    if self.ranges and self.total > 0:
                        self._multi()
                    else:
                        self._single()
                    break
                except RangeUnsupported as exc:
                    if round_ or self._cancel.is_set():
                        raise
                    log_line("Range 不可用，改用单线程：%s" % exc)
                    self.ranges = False
                    self._drop_parts()
                    self.received = 0
                    self.segments = []
                    self.sig_phase.emit("服务器不支持分段，改用单线程…")
                except Throttled as exc:
                    if relaxed or self._cancel.is_set():
                        raise
                    relaxed = True
                    self.threads = max(2, self.threads // 2)
                    self._throttle_code = None
                    self._throttle_hits = 0
                    log_line("被限流 HTTP %s，降到 %d 线程重试" % (exc.code, self.threads))
                    self.sig_phase.emit("被限流，降到 %d 线程重试…" % self.threads)
                    time.sleep(8)
            if self._cancel.is_set():
                self.state = "cancelled"
                self.sig_phase.emit("已取消")
                return
            self.state = "merging"
            self.sig_phase.emit("正在合并…")
            self._merge()
            self.state = "hashing"
            self.sig_phase.emit("计算校验值…")
            self.sha = sha256_of(self.out)
            got = os.path.getsize(self.out)
            if self.total > 0 and got != self.total:
                raise IOError("大小不一致：本地 %d / 远端 %d" % (got, self.total))
            self.state = "done"
            self.sig_phase.emit("完成")
            self.sig_done.emit(self.out, self.sha)
        except Throttled as exc:
            self.state = "error"
            self.error = THROTTLE_HINT % exc.code
            log_line("限流 url=%s out=%s threads=%d received=%d err=%s"
                     % (self.url, self.out, self.threads, self.received, self.error))
            self.sig_fail.emit(self.error)
        except FatalHTTP as exc:
            self.state = "error"
            self.error = http_hint(exc.code)
            log_line("失败 url=%s out=%s threads=%d received=%d err=%s"
                     % (self.url, self.out, self.threads, self.received, self.error))
            self.sig_fail.emit(self.error)
        except Exception as exc:
            self.state = "error"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            log_line("失败 url=%s out=%s threads=%d received=%d err=%s"
                     % (self.url, self.out, self.threads, self.received, self.error))
            self.sig_fail.emit(self.error)

    # ---- 分段 ----
    def _multi(self):
        os.makedirs(self.parts_dir, exist_ok=True)
        n = min(self.threads, max(1, int((self.total + MIN_CHUNK - 1) // MIN_CHUNK)))
        chunk = (self.total + n - 1) // n
        self.segments = []
        for i in range(n):
            start = i * chunk
            if start >= self.total:
                break
            self.segments.append({
                "i": i, "start": start,
                "end": min(start + chunk - 1, self.total - 1),
                "path": os.path.join(self.parts_dir, "part_%04d" % i),
            })
        self.received = sum(
            (os.path.getsize(s["path"]) if os.path.exists(s["path"]) else 0)
            for s in self.segments)
        self.sig_segments.emit(len(self.segments))
        self._worker_error = None
        workers = [threading.Thread(target=self._worker_safe, args=(s,), daemon=True)
                   for s in self.segments]
        for w in workers:
            w.start()
            time.sleep(0.08)          # 错峰，别一秒内糊上去 n 个连接
        for w in workers:
            w.join()
        if self._throttle_code is not None:
            raise Throttled(self._throttle_code)
        if self.fatal is not None:
            raise FatalHTTP(self.fatal)
        if self._worker_error:
            raise IOError(self._worker_error)

    def _worker_safe(self, seg):
        """把线程里的异常收起来，避免在控制台吐 traceback。"""
        try:
            self._worker(seg)
        except Throttled as exc:
            self._throttle_code = exc.code
        except FatalHTTP:
            pass
        except Exception as exc:
            self._worker_error = str(exc)

    def _worker(self, seg):
        need = seg["end"] - seg["start"] + 1
        have = os.path.getsize(seg["path"]) if os.path.exists(seg["path"]) else 0
        if have >= need:
            return
        for attempt in range(5):
            if self._cancel.is_set() or self.fatal is not None or self._throttle_code is not None:
                return
            try:
                req = urllib.request.Request(self.url)
                req.add_header("User-Agent", UA)
                for k, v in extra_headers(self.url).items():
                    req.add_header(k, v)
                req.add_header("Range", "bytes=%d-%d" % (seg["start"] + have, seg["end"]))
                with opener_for(self.proxy).open(req, timeout=60) as resp:
                    status = getattr(resp, "status", None)
                    if status == 200:
                        raise RangeUnsupported("服务器不支持分段（返回 200）")
                    if status != 206:
                        raise IOError("HTTP %s" % status)
                    with open(seg["path"], "ab") as fh:
                        while True:
                            if self._cancel.is_set():
                                return
                            buf = resp.read(BUF)
                            if not buf:
                                break
                            fh.write(buf)
                            have += len(buf)
                            self.received += len(buf)
                        fh.flush()
                if have >= need:
                    return
            except urllib.error.HTTPError as exc:
                if exc.code in THROTTLE_CODES:
                    self._throttle_hits += 1
                    if attempt >= 4:
                        raise Throttled(exc.code)
                    delay = min(25.0, 4.0 * (attempt + 1)) + random.uniform(0.0, 1.2)
                    if self._throttle_hits == 1:
                        log_line("命中限流 HTTP %s，退避重试" % exc.code)
                        self.sig_phase.emit("被限流，退避重试…")
                    time.sleep(delay)
                    continue
                if exc.code in HTTP_HINTS:
                    self.fatal = exc.code
                    raise FatalHTTP(exc.code)
                raise IOError("HTTP %s" % exc.code)
            except RangeUnsupported:
                raise
            except FatalHTTP:
                raise
            except Exception as exc:
                if attempt >= 4 or self._cancel.is_set():
                    raise IOError("分段 %d 失败：%s" % (seg["i"], exc))
                time.sleep(2 + attempt * 2)

    def _single(self):
        path = self.out + ".parts_single"
        self.segments = [{"i": 0, "path": path, "start": 0, "end": max(self.total, 0)}]
        if os.path.exists(path):
            os.remove(path)
        self.sig_segments.emit(1)
        req = urllib.request.Request(self.url)
        req.add_header("User-Agent", UA)
        for _k, _v in extra_headers(self.url).items():
            req.add_header(_k, _v)
        try:
            resp_ctx = opener_for(self.proxy).open(req, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code in THROTTLE_CODES:
                raise Throttled(exc.code)
            raise FatalHTTP(exc.code)
        with resp_ctx as resp, open(path, "wb") as fh:
            if self.total < 0:
                cl = resp.headers.get("Content-Length")
                if cl:
                    self.total = int(cl)
            while True:
                if self._cancel.is_set():
                    return
                buf = resp.read(BUF)
                if not buf:
                    break
                fh.write(buf)
                self.received += len(buf)

    def _merge(self):
        if self.ranges and self.segments:
            with open(self.out, "wb") as out:
                for seg in sorted(self.segments, key=lambda s: s["i"]):
                    with open(seg["path"], "rb") as src:
                        while True:
                            buf = src.read(4 * 1024 * 1024)
                            if not buf:
                                break
                            out.write(buf)
        else:
            single = self.out + ".parts_single"
            if os.path.exists(single):
                os.replace(single, self.out)
        for path in [s["path"] for s in self.segments] + [self.out + ".parts_single"]:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(self.parts_dir)
        except OSError:
            pass

    # ---- 给界面用的快照 ----
    def snapshot(self):
        done = self.received
        if self.state in ("merging", "hashing", "done"):
            done = self.total if self.total > 0 else done
        segs = []
        for s in self.segments:
            try:
                segs.append((os.path.getsize(s["path"]),
                             s["end"] - s["start"] + 1))
            except OSError:
                segs.append((0, s["end"] - s["start"] + 1))
        return done, segs


# --------------------------------------------------------------------------- 界面
QSS = """
QMainWindow, QWidget#Root { background:#0b1020; }
QLabel { background:transparent; color:#e8eefc;
         font-family:"Microsoft YaHei","Segoe UI"; font-size:13px; }
QWidget { font-family:"Microsoft YaHei","Segoe UI"; font-size:13px; color:#e8eefc; }
QFrame#Card { background:#141b30; border:1px solid #26304d; border-radius:14px; }
QFrame#Stat { background:#101728; border:1px solid #26304d; border-radius:10px; }
QFrame#Line { background:#26304d; border:none; }
#Title { font-size:19px; font-weight:700; }
#Sub { color:#8fa0c4; font-size:12px; }
#K { color:#8fa0c4; font-size:12px; }
#V { color:#e8eefc; font-size:15px; font-weight:600; }
#Pct { color:#e8eefc; font-size:34px; font-weight:700; }
#Hint { color:#8fa0c4; font-size:12px; }
#Phase { color:#8fa0c4; font-size:12px; }
#Hash { color:#7fe3c4; font-size:12px; }
#FormLabel { color:#8fa0c4; font-size:12px; }
QLineEdit, QSpinBox {
    background:#0e1526; border:1px solid #26304d; border-radius:8px;
    padding:7px 10px; color:#e8eefc; selection-background-color:#3572e0; }
QLineEdit:focus, QSpinBox:focus { border-color:#4f8cff; }
QLineEdit::placeholder { color:#5b6885; }
QPushButton { background:#202a45; border:1px solid #26304d; border-radius:9px;
              padding:9px 16px; color:#e8eefc; font-weight:600; }
QPushButton:hover { background:#28345a; }
QPushButton:disabled { color:#5b6885; background:#161f34; border-color:#212a44; }
#Primary { background:#3572e0; border-color:#4f8cff; }
#Primary:hover { background:#4480f0; }
#Primary:disabled { background:#1d2c4d; border-color:#243a63; }
#Danger { background:#3a2030; border-color:#5a2a3a; color:#ff9aa4; }
#Danger:hover { background:#4a2738; }
#Danger:disabled { background:#231a26; border-color:#31222d; color:#6b4a55; }
#Primary:disabled, #Danger:disabled { }
QProgressBar { background:#0e1526; border:1px solid #26304d; border-radius:8px; }
QProgressBar::chunk { border-radius:7px;
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4f8cff,stop:1 #35d0a5); }
#Seg { background:#0e1526; border:none; border-radius:4px; }
#Seg::chunk { background:#4f8cff; border-radius:2px; }
QSpinBox::up-button, QSpinBox::down-button { width:14px; background:#16203a; border:none; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("大鲸鱼下载器")
        self.resize(880, 640)
        self.worker = None
        self.probe_size = -1
        self.probe_ranges = False
        self.last_done = 0
        self.last_ts = 0.0
        self.speed = 0.0
        self.seg_bars = []  # 兼容旧字段

        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.on_tick)

        self.build_ui()
        self.setStyleSheet(QSS)

    # ---------------- UI ----------------
    def build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        logo = QLabel("🐋")
        logo.setStyleSheet("font-size:30px;")
        head.addWidget(logo)
        tbox = QVBoxLayout()
        tbox.setSpacing(0)
        t = QLabel("大鲸鱼下载器")
        t.setObjectName("Title")
        s = QLabel("多线程 · 断点续传 · 自动校验 SHA256")
        s.setObjectName("Sub")
        tbox.addWidget(t)
        tbox.addWidget(s)
        head.addLayout(tbox)
        head.addStretch(1)
        outer.addLayout(head)

        # 输入卡片
        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("粘贴下载链接（http:// 或 https://）")
        self.ed_url.returnPressed.connect(self.on_probe)
        self.btn_probe = QPushButton("探测")
        self.btn_probe.setObjectName("Primary")
        self.btn_probe.clicked.connect(self.on_probe)
        grid.addWidget(self.ed_url, 0, 0, 1, 4)
        grid.addWidget(self.btn_probe, 0, 4)

        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("文件名")
        lbl1 = QLabel("文件名")
        lbl1.setObjectName("FormLabel")
        grid.addWidget(lbl1, 1, 0)
        grid.addWidget(self.ed_name, 1, 1, 1, 4)

        self.ed_dir = QLineEdit(DEFAULT_DIR)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self.on_browse)
        self.btn_open_dir = QPushButton("打开")
        self.btn_open_dir.clicked.connect(self.on_open_dir)
        lbl2 = QLabel("保存到")
        lbl2.setObjectName("FormLabel")
        grid.addWidget(lbl2, 2, 0)
        grid.addWidget(self.ed_dir, 2, 1, 1, 2)
        grid.addWidget(self.btn_browse, 2, 3)
        grid.addWidget(self.btn_open_dir, 2, 4)

        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(1, 32)
        self.sp_threads.setValue(12)
        self.ed_proxy = QLineEdit()
        self.ed_proxy.setPlaceholderText("可选，如 http://127.0.0.1:7897")
        lbl3 = QLabel("线程")
        lbl3.setObjectName("FormLabel")
        grid.addWidget(lbl3, 3, 0)
        grid.addWidget(self.sp_threads, 3, 1)
        lbl4 = QLabel("代理")
        lbl4.setObjectName("FormLabel")
        grid.addWidget(lbl4, 3, 2)
        grid.addWidget(self.ed_proxy, 3, 3, 1, 2)

        self.btn_start = QPushButton("开始下载")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.btn_reveal = QPushButton("打开文件夹")
        self.btn_reveal.setEnabled(False)
        self.btn_reveal.clicked.connect(self.on_reveal)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_reveal)
        row.addStretch(1)
        self.lb_size = QLabel("—")
        self.lb_size.setObjectName("Hint")
        row.addWidget(self.lb_size)
        holder = QWidget()
        holder.setLayout(row)
        grid.addWidget(holder, 4, 0, 1, 5)

        outer.addWidget(card)

        # 进度卡片
        pc = QFrame()
        pc.setObjectName("Card")
        pv = QVBoxLayout(pc)
        pv.setContentsMargins(18, 16, 18, 16)
        pv.setSpacing(10)

        top = QHBoxLayout()
        self.lb_pct = QLabel("0%")
        self.lb_pct.setObjectName("Pct")
        self.lb_phase = QLabel("空闲")
        self.lb_phase.setObjectName("Phase")
        self.lb_phase.setMinimumWidth(96)
        self.lb_phase.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.lb_pct)
        top.addStretch(1)
        top.addWidget(self.lb_phase)
        pv.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(16)
        pv.addWidget(self.bar)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.lb_done = self.make_stat(stats, "已下载")
        self.lb_total = self.make_stat(stats, "总大小")
        self.lb_speed = self.make_stat(stats, "速度")
        self.lb_eta = self.make_stat(stats, "剩余")
        pv.addLayout(stats)

        self.seg_strip = SegStrip()
        pv.addWidget(self.seg_strip)

        self.lb_hash = QLabel("")
        self.lb_hash.setObjectName("Hash")
        self.lb_hash.setWordWrap(True)
        self.lb_hash.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pv.addWidget(self.lb_hash)

        self.lb_msg = QLabel("先粘贴一个链接，点「探测」。")
        self.lb_msg.setObjectName("Hint")
        self.lb_msg.setWordWrap(True)
        self.lb_msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pv.addWidget(self.lb_msg)

        outer.addWidget(pc)
        outer.addStretch(1)

    def make_stat(self, layout, key):
        box = QFrame()
        box.setObjectName("Stat")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(2)
        k = QLabel(key)
        k.setObjectName("K")
        val = QLabel("—")
        val.setObjectName("V")
        v.addWidget(k)
        v.addWidget(val)
        layout.addWidget(box)
        return val

    # ---------------- 动作 ----------------
    def note(self, text):
        self.lb_msg.setText(text)

    def on_browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_dir.text() or DEFAULT_DIR)
        if d:
            self.ed_dir.setText(d)

    def on_open_dir(self):
        path = self.ed_dir.text().strip() or DEFAULT_DIR
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def on_probe(self):
        url = self.ed_url.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            self.note("链接要以 http:// 或 https:// 开头。")
            return
        self.btn_probe.setEnabled(False)
        self.note("探测中…")
        self.lb_size.setText("—")
        QApplication.processEvents()
        try:
            size, ranges, name = probe(url, self.ed_proxy.text().strip())
        except FatalHTTP as exc:
            self.btn_probe.setEnabled(True)
            self.note("探测失败：" + http_hint(exc.code))
            return
        except Exception as exc:
            self.btn_probe.setEnabled(True)
            self.note("探测失败：%s（可能是链接过期、需要登录，或者网络被墙）" % exc)
            return
        self.btn_probe.setEnabled(True)
        self.probe_size, self.probe_ranges = size, ranges
        if name and not self.ed_name.text().strip():
            self.ed_name.setText(name)
        self.btn_start.setEnabled(True)
        txt = human(size) if size > 0 else "未知"
        self.lb_size.setText("%s · %s" % (txt, "可分段" if ranges else "单线程"))
        self.note("探测好了：%s，%s。" % (txt, "支持多线程/续传" if ranges else "服务器不支持分段，将单线程下载"))

    def on_start(self):
        url = self.ed_url.text().strip()
        if not url:
            return
        out_dir = self.ed_dir.text().strip() or DEFAULT_DIR
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:
            self.note("目录不可用：%s" % exc)
            return
        name = safe_name(self.ed_name.text().strip() or "download.bin")
        out_path = os.path.join(out_dir, name)

        self.worker = Downloader(
            url, out_path, self.sp_threads.value(), self.ed_proxy.text().strip(),
            size=self.probe_size, ranges=self.probe_ranges, parent=self)
        self.worker.sig_phase.connect(self.lb_phase.setText)
        self.worker.sig_segments.connect(self.build_seg_bars)
        self.worker.sig_done.connect(self.on_done)
        self.worker.sig_fail.connect(self.on_fail)

        self.last_done = 0
        self.last_ts = time.time()
        self.speed = 0.0
        self.bar.setValue(0)
        self.lb_pct.setText("0%")
        self.lb_hash.setText("")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_reveal.setEnabled(False)
        self.note("开始下载到 %s" % out_path)
        self.worker.start()
        self.timer.start()

    def on_cancel(self):
        if self.worker:
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.note("正在停止…")

    def on_reveal(self):
        if self.worker and os.path.exists(self.worker.out):
            os.system('explorer /select,"%s"' % os.path.normpath(self.worker.out))

    def build_seg_bars(self, n):
        self.seg_strip.set_count(max(1, int(n)))

    # ---------------- 刷新 ----------------
    def on_tick(self):
        w = self.worker
        if not w:
            return
        done, segs = w.snapshot()
        total = w.total
        pct = (100.0 * done / total) if total > 0 else 0.0
        self.lb_pct.setText("%.1f%%" % pct)
        self.bar.setValue(int(min(1000, pct * 10)))
        self.lb_done.setText(human(done))
        self.lb_total.setText(human(total) if total > 0 else "未知")

        if w.received > 0 and self.lb_phase.text() == "连接中…":
            self.lb_phase.setText("下载中")
        now = time.time()
        dt = now - self.last_ts
        if dt > 0.2 and done >= self.last_done:
            inst = (done - self.last_done) / dt
            self.speed = inst if inst > 0 else self.speed * 0.6
            self.last_done, self.last_ts = done, now
        self.lb_speed.setText(human(self.speed) + "/s" if self.speed > 1024 else "—")
        self.lb_eta.setText(eta_text((total - done) / self.speed) if self.speed > 1024 and total > done else "—")

        self.seg_strip.set_values([(got / float(need)) if need > 0 else 0.0 for got, need in segs])

        if w.state in ("done", "error", "cancelled"):
            self.timer.stop()
            self.btn_cancel.setEnabled(False)
            self.btn_start.setEnabled(True)
            if w.state == "done":
                self.lb_phase.setText("完成")
                self.btn_reveal.setEnabled(True)
                self.lb_pct.setText("100%")
                self.bar.setValue(1000)
                self.lb_speed.setText("—")
                self.lb_eta.setText("—")
                self.lb_hash.setText("SHA256  " + w.sha)
                self.note("下载完成：%s" % w.out)

    def on_done(self, path, sha):
        self.note("下载完成：%s" % path)

    def on_fail(self, msg):
        self.lb_phase.setText("失败")
        keep = ""
        if self.worker and (os.path.exists(self.worker.out + ".parts")
                            or os.path.exists(self.worker.out + ".parts_single")):
            keep = "。已下载的分片保留了，点「开始下载」会继续接着下"
        self.note("失败：%s%s" % (msg, keep))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        event.accept()


def dark_title_bar(widget):
    """Win10/11 深色标题栏，失败就算了。"""
    try:
        import ctypes
        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (新版/旧版)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def main():
    prefer_ipv4()
    app = QApplication(sys.argv)
    app.setApplicationName("大鲸鱼下载器")
    win = MainWindow()
    win.show()
    dark_title_bar(win)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
