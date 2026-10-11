# -*- coding: utf-8 -*-
"""
동영상 변환기 — AVI / WMV / MKV / MOV / ASF 파일을 MP4로 변환합니다.

- 변환 엔진: ffmpeg (imageio-ffmpeg 패키지에 들어 있는 실행파일, 또는 exe 옆에 둔 ffmpeg.exe)
- 화면: tkinter (끌어다 놓기는 tkinterdnd2가 있을 때만 켜짐)
"""

import os
import re
import sys
import queue
import shutil
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:  # 패키지가 없으면 버튼으로만 파일을 추가
    HAS_DND = False

APP_TITLE = "동영상 변환기 (MP4)"
INPUT_EXTS = (".avi", ".wmv", ".mkv", ".mov", ".asf")

# ffmpeg가 그대로 MP4에 담을 수 있는 코덱 (재인코딩 없이 '원본 그대로' 가능)
MP4_VIDEO_OK = {"h264", "hevc", "mpeg4", "av1"}
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}

# 품질 선택지: (표시 이름, crf, preset)
QUALITY_PRESETS = [
    ("원본 그대로 (가능하면 재인코딩 안 함, 가장 빠름)", None, None),
    ("고화질 (크기 큼)", "18", "medium"),
    ("표준 (권장)", "23", "medium"),
    ("작게 (화질 낮음, 빠름)", "28", "fast"),
]
RESOLUTIONS = [("원본 크기", None), ("1080p (1920×1080)", 1080), ("720p (1280×720)", 720), ("480p (854×480)", 480)]

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------- ffmpeg 찾기
def resource_dir():
    """PyInstaller로 묶였을 때는 임시 풀림 폴더, 아니면 이 파일의 폴더."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def exe_dir():
    """실행파일(또는 스크립트)이 놓인 폴더."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    candidates = [
        os.path.join(exe_dir(), "ffmpeg.exe"),
        os.path.join(resource_dir(), "ffmpeg.exe"),
        os.path.join(resource_dir(), "ffmpeg"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


FFMPEG = find_ffmpeg()


# ---------------------------------------------------------------- 영상 정보
def probe(path):
    """ffmpeg -i 로 길이(초)와 비디오/오디오 코덱 이름을 읽는다."""
    info = {"duration": 0.0, "vcodec": None, "acodec": None}
    try:
        p = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        out = p.stderr
    except Exception:
        return info
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Stream #\d+:\d+.*?Video:\s*([A-Za-z0-9_]+)", out)
    if m:
        info["vcodec"] = m.group(1).lower()
    m = re.search(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)", out)
    if m:
        info["acodec"] = m.group(1).lower()
    return info


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def unique_path(path):
    """같은 이름이 있으면 '이름 (1).mp4' 식으로 번호를 붙인다."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


# ---------------------------------------------------------------- 변환 작업
class Job:
    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path) if os.path.exists(path) else 0
        self.status = "대기"
        self.percent = 0
        self.output = None
        self.error = None


class Converter(threading.Thread):
    """작업 목록을 순서대로 변환하는 백그라운드 스레드. 진행 상황은 큐로 보낸다."""

    def __init__(self, jobs, options, q):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.opt = options
        self.q = q
        self.stop_flag = threading.Event()
        self.proc = None

    def stop(self):
        self.stop_flag.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def run(self):
        for idx, job in enumerate(self.jobs):
            if self.stop_flag.is_set():
                job.status = "취소"
                self.q.put(("update", idx))
                continue
            self.convert_one(idx, job)
        self.q.put(("done", None))

    def build_cmd(self, job, info, out_path):
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", job.path]
        crf, preset, height = self.opt["crf"], self.opt["preset"], self.opt["height"]

        copy_ok = (
            crf is None
            and height is None
            and info["vcodec"] in MP4_VIDEO_OK
            and (info["acodec"] is None or info["acodec"] in MP4_AUDIO_OK)
        )
        if copy_ok:
            cmd += ["-c", "copy"]
            job.note = "재인코딩 없음"
        else:
            if crf is None:  # '원본 그대로'를 골랐지만 코덱이 MP4에 안 맞는 경우
                crf, preset = "20", "medium"
                job.note = "코덱이 MP4에 맞지 않아 재인코딩"
            else:
                job.note = "재인코딩"
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
            if height:
                # 세로 크기를 맞추고 가로는 비율대로 (짝수로 맞춤). 원본이 더 작으면 키우지 않음.
                cmd += ["-vf", f"scale=-2:'min({height},ih)'"]
            else:
                cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
            if info["acodec"]:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-loglevel", "error", out_path]
        return cmd

    def convert_one(self, idx, job):
        job.status = "정보 읽는 중"
        job.percent = 0
        self.q.put(("update", idx))

        info = probe(job.path)
        if info["vcodec"] is None:
            job.status = "실패"
            job.error = "영상 정보를 읽을 수 없습니다 (손상되었거나 지원하지 않는 파일)"
            self.q.put(("update", idx))
            return

        out_dir = self.opt["out_dir"] or os.path.dirname(job.path)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            job.status = "실패"
            job.error = f"출력 폴더를 만들 수 없습니다: {e}"
            self.q.put(("update", idx))
            return
        name = os.path.splitext(os.path.basename(job.path))[0] + ".mp4"
        out_path = os.path.join(out_dir, name)
        if os.path.abspath(out_path) == os.path.abspath(job.path):
            out_path = os.path.join(out_dir, os.path.splitext(name)[0] + " (변환).mp4")
        if not self.opt["overwrite"]:
            out_path = unique_path(out_path)
        job.output = out_path

        cmd = self.build_cmd(job, info, out_path)
        job.status = "변환 중"
        self.q.put(("update", idx))
        self.q.put(("log", f"▶ {os.path.basename(job.path)} → {os.path.basename(out_path)} ({job.note})"))

        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            job.status = "실패"
            job.error = f"ffmpeg 실행 실패: {e}"
            self.q.put(("update", idx))
            return

        duration = info["duration"] or 0
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    t = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if duration > 0:
                    job.percent = max(0, min(99, int(t / duration * 100)))
                    self.q.put(("update", idx))
            elif line.startswith("progress=end"):
                job.percent = 100
        err_text = self.proc.stderr.read()
        code = self.proc.wait()
        self.proc = None

        if self.stop_flag.is_set():
            job.status = "취소"
            job.error = "사용자가 중지함"
            self._remove_partial(out_path)
        elif code == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            job.status = "완료"
            job.percent = 100
            self.q.put(("log", f"   완료: {out_path} ({human_size(os.path.getsize(out_path))})"))
        else:
            job.status = "실패"
            msg = err_text.strip().splitlines()
            job.error = msg[-1] if msg else f"ffmpeg 종료 코드 {code}"
            self._remove_partial(out_path)
            self.q.put(("log", f"   실패: {job.error}"))
        self.q.put(("update", idx))

    @staticmethod
    def _remove_partial(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ---------------------------------------------------------------- 화면
BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("860x600")
        self.minsize(720, 480)
        self.jobs = []
        self.worker = None
        self.q = queue.Queue()

        try:
            self.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("TButton", padding=(10, 5))
        style.configure("Big.TButton", font=("맑은 고딕", 11, "bold"), padding=(16, 8))
        style.configure("Treeview", rowheight=26, font=("맑은 고딕", 10))
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        self.option_add("*Font", ("맑은 고딕", 10))

        self._build()
        self.after(100, self._poll_queue)

        if FFMPEG is None:
            messagebox.showerror(
                "ffmpeg 없음",
                "변환 엔진(ffmpeg.exe)을 찾을 수 없습니다.\n"
                "프로그램과 같은 폴더에 ffmpeg.exe를 넣고 다시 실행해 주세요.",
            )

    # ---- 화면 구성
    def _build(self):
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Button(top, text="파일 추가…", command=self.add_files).pack(side="left")
        ttk.Button(top, text="폴더 추가…", command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="선택 제거", command=self.remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="완료 항목 지우기", command=self.clear_done).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="모두 지우기", command=self.clear_all).pack(side="left", padx=(6, 0))
        hint = "지원: AVI · WMV · MKV · MOV · ASF  →  MP4"
        if HAS_DND:
            hint += "   (파일을 목록에 끌어다 놓아도 됩니다)"
        ttk.Label(top, text=hint, foreground="#555").pack(side="right")

        mid = ttk.Frame(self, padding=(12, 4))
        mid.pack(fill="both", expand=True)
        cols = ("name", "size", "status", "percent")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="파일")
        self.tree.heading("size", text="크기")
        self.tree.heading("status", text="상태")
        self.tree.heading("percent", text="진행")
        self.tree.column("name", width=460, anchor="w")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=120, anchor="center", stretch=False)
        self.tree.column("percent", width=70, anchor="center", stretch=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("fail", foreground="#c62828")
        self.tree.tag_configure("run", foreground="#1565c0")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", self._on_double_click)
        if HAS_DND:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_drop)

        # 설정
        opt = ttk.LabelFrame(self, text="변환 설정", padding=(12, 6))
        opt.pack(fill="x", padx=12, pady=(4, 4))
        ttk.Label(opt, text="품질").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.quality = ttk.Combobox(opt, state="readonly", width=44, values=[q[0] for q in QUALITY_PRESETS])
        self.quality.current(2)
        self.quality.grid(row=0, column=1, sticky="w")
        ttk.Label(opt, text="크기").grid(row=0, column=2, sticky="w", padx=(18, 6))
        self.resolution = ttk.Combobox(opt, state="readonly", width=20, values=[r[0] for r in RESOLUTIONS])
        self.resolution.current(0)
        self.resolution.grid(row=0, column=3, sticky="w")

        ttk.Label(opt, text="저장 위치").grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 6))
        self.out_mode = tk.StringVar(value="same")
        f = ttk.Frame(opt)
        f.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(f, text="원본 파일과 같은 폴더", variable=self.out_mode, value="same",
                        command=self._update_out_state).pack(side="left")
        ttk.Radiobutton(f, text="지정 폴더:", variable=self.out_mode, value="custom",
                        command=self._update_out_state).pack(side="left", padx=(14, 4))
        self.out_dir = tk.StringVar(value="")
        self.out_entry = ttk.Entry(f, textvariable=self.out_dir, width=38)
        self.out_entry.pack(side="left")
        self.out_btn = ttk.Button(f, text="찾아보기…", command=self.choose_out_dir)
        self.out_btn.pack(side="left", padx=(4, 0))

        f2 = ttk.Frame(opt)
        f2.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(f2, text="같은 이름의 MP4가 있으면 덮어쓰기 (끄면 '이름 (1).mp4'로 저장)",
                        variable=self.overwrite).pack(side="left")
        self.open_after = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="모두 끝나면 저장 폴더 열기", variable=self.open_after).pack(side="left", padx=(18, 0))
        self._update_out_state()

        # 진행 / 시작
        bottom = ttk.Frame(self, padding=(12, 4, 12, 6))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(6, 0))
        self.status_lbl = ttk.Label(row, text="파일을 추가한 뒤 [변환 시작]을 누르세요.")
        self.status_lbl.pack(side="left")
        self.start_btn = ttk.Button(row, text="변환 시작", style="Big.TButton", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(row, text="중지", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 8))

        # 기록
        logf = ttk.Frame(self, padding=(12, 0, 12, 10))
        logf.pack(fill="x")
        self.log = tk.Text(logf, height=5, state="disabled", wrap="none", font=("맑은 고딕", 9),
                           background="#f7f7f7", relief="flat")
        self.log.pack(fill="x")

    # ---- 파일 추가/제거
    def _add_paths(self, paths):
        existing = {j.path.lower() for j in self.jobs}
        added, skipped = 0, 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for fn in files:
                        if fn.lower().endswith(INPUT_EXTS):
                            fp = os.path.join(root, fn)
                            if fp.lower() not in existing:
                                self.jobs.append(Job(fp)); existing.add(fp.lower()); added += 1
                continue
            if not p.lower().endswith(INPUT_EXTS):
                skipped += 1
                continue
            if p.lower() in existing:
                continue
            self.jobs.append(Job(p)); existing.add(p.lower()); added += 1
        self._refresh_tree()
        if skipped:
            self._log(f"지원하지 않는 파일 {skipped}개는 건너뛰었습니다. (AVI·WMV·MKV·MOV·ASF만 가능)")
        if added:
            self.status_lbl.config(text=f"{len(self.jobs)}개 파일이 목록에 있습니다.")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="변환할 동영상 선택",
            filetypes=[("동영상 파일", "*.avi *.wmv *.mkv *.mov *.asf"), ("모든 파일", "*.*")],
        )
        if paths:
            self._add_paths(paths)

    def add_folder(self):
        d = filedialog.askdirectory(title="동영상이 들어 있는 폴더 선택")
        if d:
            self._add_paths([d])

    def _on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self._add_paths(paths)

    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        job = self.jobs[int(item)]
        if job.status == "실패" and job.error:
            messagebox.showerror("실패 이유", f"{os.path.basename(job.path)}\n\n{job.error}")
        elif job.status == "완료" and job.output and os.path.exists(job.output):
            self._open_folder(os.path.dirname(job.output), select=job.output)

    def remove_selected(self):
        if self._running():
            return
        sel = {int(i) for i in self.tree.selection()}
        if not sel:
            return
        self.jobs = [j for i, j in enumerate(self.jobs) if i not in sel]
        self._refresh_tree()

    def clear_done(self):
        if self._running():
            return
        self.jobs = [j for j in self.jobs if j.status != "완료"]
        self._refresh_tree()

    def clear_all(self):
        if self._running():
            return
        self.jobs = []
        self._refresh_tree()

    # ---- 설정
    def _update_out_state(self):
        st = "normal" if self.out_mode.get() == "custom" else "disabled"
        self.out_entry.config(state=st)
        self.out_btn.config(state=st)

    def choose_out_dir(self):
        d = filedialog.askdirectory(title="MP4를 저장할 폴더 선택")
        if d:
            self.out_dir.set(os.path.normpath(d))

    def _options(self):
        _, crf, preset = QUALITY_PRESETS[self.quality.current()]
        _, height = RESOLUTIONS[self.resolution.current()]
        out_dir = self.out_dir.get().strip() if self.out_mode.get() == "custom" else ""
        return {"crf": crf, "preset": preset, "height": height,
                "out_dir": out_dir, "overwrite": self.overwrite.get()}

    # ---- 실행
    def _running(self):
        return self.worker is not None and self.worker.is_alive()

    def start(self):
        if self._running():
            return
        if FFMPEG is None:
            messagebox.showerror("ffmpeg 없음", "변환 엔진(ffmpeg.exe)이 없어 변환할 수 없습니다.")
            return
        pending = [j for j in self.jobs if j.status not in ("완료",)]
        if not pending:
            messagebox.showinfo("안내", "변환할 파일이 없습니다. 먼저 파일을 추가해 주세요.")
            return
        opt = self._options()
        if self.out_mode.get() == "custom" and not opt["out_dir"]:
            messagebox.showwarning("저장 위치", "저장할 폴더를 지정하거나 '원본 파일과 같은 폴더'를 선택해 주세요.")
            return
        for j in pending:
            j.status, j.percent, j.error, j.output = "대기", 0, None, None
        self._refresh_tree()
        self._set_running(True)
        self.worker = Converter(pending, opt, self.q)
        self.worker.start()

    def stop(self):
        if self._running():
            self.worker.stop()
            self.status_lbl.config(text="중지하는 중…")

    def _set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.quality.config(state="disabled" if running else "readonly")
        self.resolution.config(state="disabled" if running else "readonly")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "update":
                    self._refresh_tree()
                    self._refresh_overall()
                elif kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._finished()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _finished(self):
        self._set_running(False)
        done = sum(1 for j in self.jobs if j.status == "완료")
        fail = sum(1 for j in self.jobs if j.status == "실패")
        cancel = sum(1 for j in self.jobs if j.status == "취소")
        msg = f"완료 {done}개"
        if fail:
            msg += f", 실패 {fail}개 (실패한 줄을 두 번 누르면 이유를 볼 수 있습니다)"
        if cancel:
            msg += f", 취소 {cancel}개"
        self.status_lbl.config(text=msg)
        self.progress["value"] = 100 if done and not fail and not cancel else self.progress["value"]
        self._log("■ " + msg)
        if self.open_after.get() and done and not cancel:
            outs = [j.output for j in self.jobs if j.status == "완료" and j.output]
            if outs:
                self._open_folder(os.path.dirname(outs[-1]), select=outs[-1])

    # ---- 표시
    def _refresh_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, j in enumerate(self.jobs):
            tag = {"완료": "done", "실패": "fail", "변환 중": "run", "정보 읽는 중": "run"}.get(j.status, "")
            pct = f"{j.percent}%" if j.status in ("변환 중", "완료") else ""
            self.tree.insert("", "end", iid=str(i),
                             values=(j.path, human_size(j.size), j.status, pct), tags=(tag,))
        for s in sel:
            if self.tree.exists(s):
                self.tree.selection_add(s)

    def _refresh_overall(self):
        total = len(self.worker.jobs) if self.worker else 0
        if not total:
            return
        finished = sum(1 for j in self.worker.jobs if j.status in ("완료", "실패", "취소"))
        cur = next((j for j in self.worker.jobs if j.status in ("변환 중", "정보 읽는 중")), None)
        overall = (finished * 100 + (cur.percent if cur else 0)) / total
        self.progress["value"] = overall
        if cur:
            self.status_lbl.config(text=f"{finished + 1}/{total}  {os.path.basename(cur.path)}  —  {cur.status} {cur.percent}%")

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    @staticmethod
    def _open_folder(folder, select=None):
        try:
            if sys.platform == "win32":
                if select and os.path.exists(select):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(select)])
                else:
                    os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            pass

    def on_close(self):
        if self._running():
            if not messagebox.askyesno("종료", "변환이 진행 중입니다. 중지하고 종료할까요?"):
                return
            self.worker.stop()
        self.destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
