import base64
import ctypes
import os
import pathlib
import subprocess
import tempfile
import tkinter as tk
import wave
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk


CREATE_NO_WINDOW = 0x08000000
CONFIG_FILE = pathlib.Path(__file__).with_name("eyecare_202020_config.txt")
CONFIG_VERSION = "9"
COMPATIBLE_CONFIG_VERSIONS = {"3", "4", "5", "6", "7", "8", CONFIG_VERSION}
DEFAULT_WORK_MINUTES = "20"
DEFAULT_BREAK_SECONDS = "20"
DEFAULT_VOICE_MODE = "tts"
DEFAULT_AUDIO_PATH = ""
FORCE_UNLOCK_HOTKEY = "Ctrl + Alt + U"
DEFAULT_ALERT_MESSAGE = (
    "工作时间到了，请离开屏幕，看向 20 英尺（约 6 米）外的地方，休息一下眼睛。"
    "It's time to take a break from work. "
)


def encode_powershell(script):
    """把 PowerShell 脚本编码成 -EncodedCommand 需要的格式，避免中文和引号问题。"""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def ps_quote(text):
    """把普通文本转成 PowerShell 单引号字符串。"""
    return "'" + str(text).replace("'", "''") + "'"


def run_powershell_background(script):
    """后台运行 PowerShell，不显示黑色命令行窗口。"""
    encoded = encode_powershell(script)
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-EncodedCommand",
            encoded,
        ],
        creationflags=CREATE_NO_WINDOW,
    )


def run_powershell_capture(script, timeout=60):
    """运行 PowerShell 并等待结果，用于生成临时 TTS 音频。"""
    encoded = encode_powershell(script)
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-EncodedCommand",
            encoded,
        ],
        creationflags=CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def mci_quote(text):
    """给 MCI 命令使用的路径加引号。"""
    return '"' + str(text).replace('"', "") + '"'


def mci_send(command, return_length=0):
    """调用 Windows MCI 播放控制接口。"""
    if os.name != "nt":
        raise RuntimeError("音频试听控制需要 Windows MCI。")

    if return_length > 0:
        buffer = ctypes.create_unicode_buffer(return_length)
        error = ctypes.windll.winmm.mciSendStringW(command, buffer, return_length, 0)
    else:
        buffer = None
        error = ctypes.windll.winmm.mciSendStringW(command, None, 0, 0)
    if error:
        message = ctypes.create_unicode_buffer(255)
        ctypes.windll.winmm.mciGetErrorStringW(error, message, 255)
        raise RuntimeError(message.value or f"MCI error {error}")
    return buffer.value.strip() if buffer is not None else ""


def get_wav_duration_seconds(path):
    """读取 wav 文件时长。"""
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        if rate <= 0:
            return None
        return frames / float(rate)


def get_monitor_rects(root):
    """获取所有显示器区域；失败时退回主屏幕尺寸。"""
    if os.name != "nt":
        return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]

    rects = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def callback(hmonitor, _hdc, _lprect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    try:
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(callback), 0)
    except Exception:
        rects = []

    if not rects:
        rects = [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
    return rects


class EyeCareApp:
    """20-20-20 护眼提醒器主程序。"""

    def __init__(self, root):
        self.root = root
        self.root.title("20-20-20 护眼提醒器")
        self.root.geometry("700x700")
        self.root.minsize(660, 660)
        self.root.configure(bg="#eef2f7")
        self.root.attributes("-topmost", False)

        self.is_running = False
        self.is_breaking = False
        self.time_left = int(float(DEFAULT_WORK_MINUTES) * 60)
        self.total_work_seconds = int(float(DEFAULT_WORK_MINUTES) * 60)
        self.break_time_left = int(float(DEFAULT_BREAK_SECONDS))
        self.timer_id = None
        self.break_timer_id = None
        self.overlay_windows = []
        self.preview_state = "idle"
        self.preview_alias = "eyecare_preview"
        self.preview_audio_path = None
        self.preview_temp_audio_path = None
        self.preview_poll_id = None
        self.break_alias = "eyecare_break"
        self.break_temp_audio_path = None
        self.break_stop_id = None

        self._build_style()
        self._build_ui()
        self._load_config()
        self._reset_timer_from_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_style(self):
        """设置 ttk 主题，让界面比默认 Tk 控件更整洁。"""
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.style.configure("Root.TFrame", background="#eef2f7")
        self.style.configure("Card.TFrame", background="#ffffff", relief="flat")
        self.style.configure("Title.TLabel", background="#eef2f7", foreground="#1f2937", font=("Microsoft YaHei UI", 18, "bold"))
        self.style.configure("Subtitle.TLabel", background="#eef2f7", foreground="#64748b", font=("Microsoft YaHei UI", 10))
        self.style.configure("CardTitle.TLabel", background="#ffffff", foreground="#111827", font=("Microsoft YaHei UI", 11, "bold"))
        self.style.configure("Body.TLabel", background="#ffffff", foreground="#334155", font=("Microsoft YaHei UI", 10))
        self.style.configure("Hint.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 9))
        self.style.configure("Timer.TLabel", background="#ffffff", foreground="#0f766e", font=("Consolas", 34, "bold"))
        self.style.configure("Status.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft YaHei UI", 10))
        self.style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 8))
        self.style.configure("Soft.TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        self.style.configure("Danger.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 8))
        self.style.configure("Horizontal.TProgressbar", troughcolor="#e2e8f0", background="#14b8a6", thickness=10)
        self.style.map("Accent.TButton", background=[("active", "#0f766e")])

    def _build_ui(self):
        """创建主界面。"""
        root_frame = ttk.Frame(self.root, style="Root.TFrame", padding=24)
        root_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(root_frame, text="20-20-20 护眼提醒器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root_frame,
            text="默认根据输入文字生成语音，也支持改用自定义提示音。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 18))

        timer_card = ttk.Frame(root_frame, style="Card.TFrame", padding=20)
        timer_card.pack(fill=tk.X)

        ttk.Label(timer_card, text="当前倒计时", style="CardTitle.TLabel").pack(anchor="w")
        self.timer_label = ttk.Label(timer_card, text="20:00", style="Timer.TLabel")
        self.timer_label.pack(anchor="center", pady=(8, 2))
        self.status_label = ttk.Label(timer_card, text="准备就绪", style="Status.TLabel")
        self.status_label.pack(anchor="center")

        self.progress = ttk.Progressbar(timer_card, mode="determinate", maximum=100, style="Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(16, 0))

        button_row = ttk.Frame(timer_card, style="Card.TFrame")
        button_row.pack(anchor="center", pady=(18, 0))
        self.timer_toggle_button = ttk.Button(button_row, text="开始", style="Accent.TButton", command=self.toggle_timer)
        self.timer_toggle_button.pack(side=tk.LEFT, padx=5)
        self.import_button = ttk.Button(button_row, text="导入保存设置", style="Soft.TButton", command=self.import_saved_config)
        self.import_button.pack(side=tk.LEFT, padx=5)
        self.reset_button = ttk.Button(button_row, text="重置为默认", style="Soft.TButton", command=self.reset_timer)
        self.reset_button.pack(side=tk.LEFT, padx=5)

        settings_card = ttk.Frame(root_frame, style="Card.TFrame", padding=20)
        settings_card.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        ttk.Label(settings_card, text="提醒设置", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(settings_card, text="工作时长（分钟）", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 6))
        self.work_minutes_var = tk.StringVar(value=DEFAULT_WORK_MINUTES)
        ttk.Entry(settings_card, textvariable=self.work_minutes_var, width=12).grid(row=1, column=1, sticky="w", padx=(10, 24), pady=(16, 6))

        ttk.Label(settings_card, text="休息时长（秒）", style="Body.TLabel").grid(row=1, column=2, sticky="w", pady=(16, 6))
        self.break_seconds_var = tk.StringVar(value=DEFAULT_BREAK_SECONDS)
        ttk.Entry(settings_card, textvariable=self.break_seconds_var, width=12).grid(row=1, column=3, sticky="w", padx=(10, 0), pady=(16, 6))

        ttk.Label(settings_card, text="语音提示文字", style="Body.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(12, 3))
        self.message_hint_label = ttk.Label(
            settings_card,
            text="默认使用这里输入的文字生成语音；选择自定义语音文件后，此处会变灰且不可编辑。",
            style="Hint.TLabel",
        )
        self.message_hint_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.message_text = tk.Text(
            settings_card,
            height=5,
            wrap=tk.WORD,
            relief=tk.FLAT,
            bg="#f8fafc",
            fg="#111827",
            insertbackground="#111827",
            font=("Microsoft YaHei UI", 10),
        )
        self.message_text.grid(row=4, column=0, columnspan=4, sticky="ew")
        self.message_text.insert("1.0", DEFAULT_ALERT_MESSAGE)

        self.voice_mode_var = tk.StringVar(value=DEFAULT_VOICE_MODE)
        mode_row = ttk.Frame(settings_card, style="Card.TFrame")
        mode_row.grid(row=5, column=0, columnspan=4, sticky="w", pady=(14, 8))
        ttk.Radiobutton(mode_row, text="使用 Windows 原生 TTS", variable=self.voice_mode_var, value="tts", command=self._toggle_audio_path).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="使用自定义语音文件", variable=self.voice_mode_var, value="file", command=self._toggle_audio_path).pack(side=tk.LEFT, padx=(18, 0))

        ttk.Label(settings_card, text="自定义语音文件路径", style="Body.TLabel").grid(row=6, column=0, sticky="w", pady=(4, 0))
        self.audio_path_var = tk.StringVar(value=DEFAULT_AUDIO_PATH)
        self.audio_entry = ttk.Entry(settings_card, textvariable=self.audio_path_var)
        self.audio_entry.grid(row=6, column=1, columnspan=2, sticky="ew", padx=(10, 8), pady=(4, 0))
        self.browse_button = ttk.Button(settings_card, text="浏览", style="Soft.TButton", command=self.browse_audio_file)
        self.browse_button.grid(row=6, column=3, sticky="ew", pady=(4, 0))

        self.unlock_hint_label = ttk.Label(
            settings_card,
            text=f"应急解锁：休息遮罩期间按 {FORCE_UNLOCK_HOTKEY}，可立即结束休息锁定并返回主界面。",
            style="Hint.TLabel",
        )
        self.unlock_hint_label.grid(row=7, column=0, columnspan=4, sticky="w", pady=(14, 4))

        self.audio_duration_hint_label = ttk.Label(
            settings_card,
            text="休息提醒语音应短于休息时长；过长时程序会弹窗提示并在休息结束前提前停止播放。",
            style="Hint.TLabel",
        )
        self.audio_duration_hint_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(0, 4))

        test_row = ttk.Frame(settings_card, style="Card.TFrame")
        test_row.grid(row=9, column=0, columnspan=4, sticky="e", pady=(10, 0))
        self.preview_button = ttk.Button(test_row, text="试听提示", style="Soft.TButton", command=self.start_preview)
        self.preview_button.pack(side=tk.LEFT, padx=5)
        self.preview_pause_button = ttk.Button(test_row, text="暂停试听", style="Soft.TButton", command=self.pause_preview)
        self.preview_pause_button.pack(side=tk.LEFT, padx=5)
        self.preview_resume_button = ttk.Button(test_row, text="播放试听", style="Soft.TButton", command=self.resume_preview)
        self.preview_resume_button.pack(side=tk.LEFT, padx=5)
        self.preview_stop_button = ttk.Button(test_row, text="结束试听", style="Soft.TButton", command=self.stop_preview)
        self.preview_stop_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(test_row, text="保存设置", style="Soft.TButton", command=self.save_config).pack(side=tk.LEFT, padx=5)

        settings_card.columnconfigure(1, weight=1)
        settings_card.columnconfigure(2, weight=0)
        settings_card.columnconfigure(3, weight=0)

        self._toggle_audio_path()
        self._refresh_import_button_state()
        self._refresh_preview_buttons()

    def _toggle_audio_path(self):
        """根据语音模式启用或禁用音频路径和提示文字输入。"""
        use_file = self.voice_mode_var.get() == "file"
        state = "normal" if use_file else "disabled"
        self.audio_entry.configure(state=state)
        self.browse_button.configure(state=state)
        if use_file:
            self.message_text.configure(
                state="disabled",
                bg="#e5e7eb",
                fg="#64748b",
                insertbackground="#64748b",
            )
            self.message_hint_label.configure(
                text="当前使用自定义语音文件，提示文字不会参与播放，因此已灰色锁定。"
            )
        else:
            self.message_text.configure(
                state="normal",
                bg="#f8fafc",
                fg="#111827",
                insertbackground="#111827",
            )
            self.message_hint_label.configure(
                text="默认使用这里输入的文字生成语音；选择自定义语音文件后，此处会变灰且不可编辑。"
            )

    def _set_timer_button_text(self, text):
        """刷新计时切换按钮文本。"""
        if hasattr(self, "timer_toggle_button"):
            self.timer_toggle_button.configure(text=text)

    def _refresh_preview_buttons(self):
        """根据试听状态启用或禁用试听控制按钮。"""
        if not hasattr(self, "preview_button"):
            return

        is_idle = self.preview_state == "idle"
        is_playing = self.preview_state == "playing"
        is_paused = self.preview_state == "paused"

        self.preview_button.configure(state="normal" if is_idle else "disabled")
        self.preview_pause_button.configure(state="normal" if is_playing else "disabled")
        self.preview_resume_button.configure(state="normal" if is_paused else "disabled")
        self.preview_stop_button.configure(state="normal" if self.preview_state in {"playing", "paused"} else "disabled")

    def _delete_temp_audio(self, path):
        """删除临时音频文件。"""
        if not path:
            return
        try:
            pathlib.Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _mci_close(self, alias):
        """关闭 MCI 设备别名。"""
        try:
            mci_send(f"close {alias}")
        except RuntimeError:
            pass

    def _mci_open(self, alias, path):
        """打开音频文件到指定 MCI 别名。"""
        self._mci_close(alias)
        quoted_path = mci_quote(path)
        try:
            mci_send(f"open {quoted_path} alias {alias}")
        except RuntimeError:
            self._mci_close(alias)
            suffix = pathlib.Path(path).suffix.lower()
            device_type = "waveaudio" if suffix == ".wav" else "mpegvideo"
            mci_send(f"open {quoted_path} type {device_type} alias {alias}")

    def _mci_play(self, alias, path):
        """用 MCI 播放音频。"""
        self._mci_open(alias, path)
        mci_send(f"play {alias}")

    def _mci_pause(self, alias):
        """暂停 MCI 播放。"""
        mci_send(f"pause {alias}")

    def _mci_resume(self, alias):
        """继续 MCI 播放。"""
        try:
            mci_send(f"resume {alias}")
        except RuntimeError:
            mci_send(f"play {alias}")

    def _mci_stop(self, alias):
        """停止并关闭 MCI 播放。"""
        try:
            mci_send(f"stop {alias}")
        except RuntimeError:
            pass
        self._mci_close(alias)

    def _mci_mode(self, alias):
        """读取 MCI 播放状态。"""
        try:
            return mci_send(f"status {alias} mode", 64)
        except RuntimeError:
            return ""

    def _mci_duration_seconds(self, path):
        """用 MCI 读取常见音频文件时长。"""
        alias = "eyecare_probe"
        try:
            self._mci_open(alias, path)
            value = mci_send(f"status {alias} length", 64)
            milliseconds = int(value)
            if milliseconds > 0:
                return milliseconds / 1000.0
        except (RuntimeError, ValueError):
            return None
        finally:
            self._mci_close(alias)
        return None

    def _audio_duration_seconds(self, path):
        """尽量读取音频时长。"""
        audio_path = pathlib.Path(path)
        if audio_path.suffix.lower() == ".wav":
            try:
                duration = get_wav_duration_seconds(audio_path)
                if duration:
                    return duration
            except (OSError, wave.Error):
                pass
        return self._mci_duration_seconds(str(audio_path))

    def _synthesize_tts_to_wav(self, text):
        """把 Windows TTS 合成为临时 wav 文件，便于试听控制和时长检查。"""
        temp_file = tempfile.NamedTemporaryFile(prefix="eyecare_tts_", suffix=".wav", delete=False)
        temp_path = temp_file.name
        temp_file.close()

        script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Volume = 100
$synth.Rate = 0
$synth.SetOutputToWaveFile({ps_quote(temp_path)})
$synth.Speak({ps_quote(text)})
$synth.Dispose()
"""
        try:
            result = run_powershell_capture(script, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            self._delete_temp_audio(temp_path)
            raise ValueError(f"无法生成 Windows TTS 试听音频：{exc}") from exc

        if result.returncode != 0 or not pathlib.Path(temp_path).exists():
            self._delete_temp_audio(temp_path)
            error = (result.stderr or result.stdout or "Windows TTS 未返回有效音频。").strip()
            raise ValueError(f"无法生成 Windows TTS 试听音频：{error}")

        return temp_path

    def _prepare_alert_audio(self, warn_file_fallback=True):
        """准备当前提醒音频，返回播放路径、临时文件路径和时长。"""
        if self.voice_mode_var.get() == "file":
            raw_path = self.audio_path_var.get().strip().strip('"')
            audio_path = pathlib.Path(raw_path) if raw_path else None
            if audio_path and audio_path.exists() and audio_path.is_file():
                duration = self._audio_duration_seconds(str(audio_path))
                return str(audio_path), None, duration
            if warn_file_fallback:
                messagebox.showwarning("语音文件不可用", "自定义语音文件不可用，已改用 Windows 原生 TTS。")

        temp_path = self._synthesize_tts_to_wav(self.get_message())
        duration = self._audio_duration_seconds(temp_path)
        return temp_path, temp_path, duration

    def _apply_default_settings(self):
        """把界面设置恢复为程序内置默认值。"""
        self.work_minutes_var.set(DEFAULT_WORK_MINUTES)
        self.break_seconds_var.set(DEFAULT_BREAK_SECONDS)
        self.voice_mode_var.set(DEFAULT_VOICE_MODE)
        self.audio_path_var.set(DEFAULT_AUDIO_PATH)
        self.message_text.configure(state="normal")
        self.message_text.delete("1.0", tk.END)
        self.message_text.insert("1.0", DEFAULT_ALERT_MESSAGE)
        self._toggle_audio_path()

    def _load_config(self):
        """启动时读取上次保存的设置；旧版或损坏配置会被静默忽略。"""
        if not CONFIG_FILE.exists():
            return

        try:
            values = self._read_config_file(CONFIG_FILE)
            self._apply_config_values(values)
        except ValueError:
            return

    def _read_config_file(self, config_path):
        """读取并校验配置文件，返回键值字典。"""
        values = {}
        try:
            for line in pathlib.Path(config_path).read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value
        except OSError as exc:
            raise ValueError(f"无法读取配置文件：{exc}") from exc

        # 旧版无版本号配置会被忽略；v3-v6 配置结构可兼容读取。
        if values.get("config_version") not in COMPATIBLE_CONFIG_VERSIONS:
            raise ValueError("该文件不是当前版本可读取的设置文件，请先用新版程序保存一次设置。")

        return values

    def _apply_config_values(self, values):
        """把配置文件中的值应用到界面。"""
        work_minutes = values.get("work_minutes", DEFAULT_WORK_MINUTES).strip()
        break_seconds = values.get("break_seconds", DEFAULT_BREAK_SECONDS).strip()
        voice_mode = values.get("voice_mode", DEFAULT_VOICE_MODE).strip()
        audio_path = values.get("audio_path", DEFAULT_AUDIO_PATH).strip()
        message = values.get("message", DEFAULT_ALERT_MESSAGE).replace("\\n", "\n")

        if voice_mode not in {"tts", "file"}:
            raise ValueError("配置文件中的语音模式无效。")
        self._validate_durations(work_minutes, break_seconds)

        self.work_minutes_var.set(work_minutes)
        self.break_seconds_var.set(break_seconds)
        self.voice_mode_var.set(voice_mode)
        self.audio_path_var.set(audio_path)
        self.message_text.configure(state="normal")
        self.message_text.delete("1.0", tk.END)
        self.message_text.insert("1.0", message)
        self._toggle_audio_path()

    def _saved_config_is_available(self):
        """判断当前目录是否存在可导入的新版保存配置。"""
        if not CONFIG_FILE.exists():
            return False
        try:
            self._read_config_file(CONFIG_FILE)
        except ValueError:
            return False
        return True

    def _refresh_import_button_state(self):
        """第一次使用且没有新版保存配置时，禁用导入按钮。"""
        if not hasattr(self, "import_button"):
            return
        state = "normal" if self._saved_config_is_available() else "disabled"
        self.import_button.configure(state=state)

    def _build_config_content(self):
        """生成保存配置文件的内容。"""
        message = self.get_message().replace("\n", "\\n")
        return "\n".join(
            [
                f"config_version={CONFIG_VERSION}",
                f"work_minutes={self.work_minutes_var.get().strip()}",
                f"break_seconds={self.break_seconds_var.get().strip()}",
                f"voice_mode={self.voice_mode_var.get()}",
                f"audio_path={self.audio_path_var.get().strip()}",
                f"message={message}",
            ]
        )

    def _stop_work_timer(self):
        """停止主倒计时，不改变当前界面设置。"""
        self.is_running = False
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self._set_timer_button_text("开始")

    def import_saved_config(self):
        """从用户选择的配置文件导入保存设置。"""
        if self.is_breaking:
            messagebox.showinfo("正在休息", "休息锁定中不能导入设置，倒计时结束后再操作。")
            return
        if not self._saved_config_is_available():
            self._refresh_import_button_state()
            return

        path = filedialog.askopenfilename(
            title="导入保存设置",
            initialdir=str(CONFIG_FILE.parent),
            initialfile=CONFIG_FILE.name,
            filetypes=[
                ("护眼提醒器设置文件", "*.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        try:
            values = self._read_config_file(path)
            self._apply_config_values(values)
            self._stop_work_timer()
            self._reset_timer_from_ui()
            CONFIG_FILE.write_text(self._build_config_content(), encoding="utf-8")
        except ValueError as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        except OSError as exc:
            messagebox.showerror("导入失败", f"无法保存导入后的配置：{exc}")
            return

        self.status_label.configure(text="已导入保存设置")
        self._refresh_import_button_state()

    def _validate_durations(self, work_minutes_text, break_seconds_text):
        """校验时长文本，并返回秒数。"""
        try:
            work_minutes = float(str(work_minutes_text).strip())
            break_seconds = int(float(str(break_seconds_text).strip()))
        except ValueError as exc:
            raise ValueError("工作时长和休息时长必须是数字。") from exc

        if work_minutes <= 0:
            raise ValueError("工作时长必须大于 0。")
        if break_seconds <= 0:
            raise ValueError("休息时长必须大于 0。")
        if break_seconds > 3600:
            raise ValueError("休息时长最长建议不超过 3600 秒，避免误锁太久。")

        work_seconds = int(work_minutes * 60)
        if work_seconds < 1:
            raise ValueError("工作时长至少需要 1 秒。")

        return work_seconds, break_seconds

    def save_config(self):
        """保存用户设置，方便下次启动继续使用。"""
        try:
            self._read_settings()
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc))
            return

        try:
            CONFIG_FILE.write_text(self._build_config_content(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入设置文件：{exc}")
            self._refresh_import_button_state()
            return

        self.status_label.configure(text="设置已保存")
        self._refresh_import_button_state()

    def browse_audio_file(self):
        """选择自定义语音文件。"""
        path = filedialog.askopenfilename(
            title="选择语音文件",
            filetypes=[
                ("常见音频文件", "*.wav *.mp3 *.m4a *.wma"),
                ("WAV 文件", "*.wav"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.voice_mode_var.set("file")
            self.audio_path_var.set(path)
            self._toggle_audio_path()

    def get_message(self):
        """获取用户填写的提示文本。"""
        message = self.message_text.get("1.0", tk.END).strip()
        return message or DEFAULT_ALERT_MESSAGE

    def _read_settings(self):
        """读取并校验 UI 上的时长设置。"""
        return self._validate_durations(self.work_minutes_var.get(), self.break_seconds_var.get())

    def _reset_timer_from_ui(self):
        """把倒计时重置为当前 UI 设置中的工作时长。"""
        work_seconds, _break_seconds = self._read_settings()
        self.total_work_seconds = work_seconds
        self.time_left = work_seconds
        self._update_timer_display()

    def toggle_timer(self):
        """在开始、暂停和继续之间切换。"""
        if self.is_running:
            self.pause_timer()
        else:
            self.start_timer()

    def start_timer(self):
        """开始或继续工作倒计时。"""
        if self.is_breaking:
            return

        try:
            work_seconds, _break_seconds = self._read_settings()
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc))
            return

        if not self.is_running:
            if self.time_left <= 0 or self.time_left > work_seconds:
                self.total_work_seconds = work_seconds
                self.time_left = work_seconds
            self.is_running = True
            self.status_label.configure(text="工作中")
            self._set_timer_button_text("暂停")
            self._tick_work()

    def pause_timer(self):
        """暂停工作倒计时。"""
        if not self.is_running:
            return
        self.is_running = False
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self.status_label.configure(text="已暂停")
        self._set_timer_button_text("继续")

    def reset_timer(self):
        """停止当前倒计时，并恢复当前界面默认设置。"""
        if self.is_breaking:
            messagebox.showinfo("正在休息", "休息锁定中，倒计时结束后会自动返回。")
            return
        self.is_running = False
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self._set_timer_button_text("开始")
        self._apply_default_settings()
        self._refresh_import_button_state()
        try:
            self._reset_timer_from_ui()
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc))
            return
        self.status_label.configure(text="已恢复默认设置")

    def _tick_work(self):
        """工作倒计时每秒更新一次。"""
        if not self.is_running:
            return

        if self.time_left > 0:
            self._update_timer_display()
            self.time_left -= 1
            self.timer_id = self.root.after(1000, self._tick_work)
            return

        self.is_running = False
        self.timer_id = None
        self._set_timer_button_text("开始")
        self._update_timer_display()
        try:
            _work_seconds, break_seconds = self._read_settings()
        except ValueError:
            break_seconds = int(float(DEFAULT_BREAK_SECONDS))
        self.play_break_alert(break_seconds)
        self.show_break_overlay(break_seconds)

    def _update_timer_display(self):
        """刷新主界面的时间和进度条。"""
        mins, secs = divmod(max(self.time_left, 0), 60)
        self.timer_label.configure(text=f"{mins:02d}:{secs:02d}")

        if self.total_work_seconds > 0:
            used = self.total_work_seconds - max(self.time_left, 0)
            self.progress.configure(value=max(0, min(100, used / self.total_work_seconds * 100)))
        else:
            self.progress.configure(value=0)

    def start_preview(self):
        """开始试听当前提醒音频。"""
        if self.preview_state != "idle":
            return

        self.preview_state = "preparing"
        self._refresh_preview_buttons()
        self.status_label.configure(text="正在准备试听")
        self.root.update_idletasks()

        try:
            audio_path, temp_path, _duration = self._prepare_alert_audio()
            self._mci_play(self.preview_alias, audio_path)
        except ValueError as exc:
            self.preview_state = "idle"
            self._refresh_preview_buttons()
            messagebox.showerror("试听失败", str(exc))
            return
        except RuntimeError as exc:
            self.preview_state = "idle"
            self._refresh_preview_buttons()
            self._delete_temp_audio(locals().get("temp_path"))
            messagebox.showerror("试听失败", f"无法播放当前提示音：{exc}")
            return

        self.preview_audio_path = audio_path
        self.preview_temp_audio_path = temp_path
        self.preview_state = "playing"
        self.status_label.configure(text="正在试听")
        self._refresh_preview_buttons()
        self._poll_preview()

    def _poll_preview(self):
        """轮询试听是否已经结束。"""
        if self.preview_state not in {"playing", "paused"}:
            return

        mode = self._mci_mode(self.preview_alias)
        if self.preview_state == "playing" and mode in {"stopped", "not ready", ""}:
            self._finish_preview("试听结束")
            return

        self.preview_poll_id = self.root.after(300, self._poll_preview)

    def _finish_preview(self, status_text):
        """清理试听播放状态。"""
        if self.preview_poll_id:
            try:
                self.root.after_cancel(self.preview_poll_id)
            except tk.TclError:
                pass
            self.preview_poll_id = None

        self._mci_stop(self.preview_alias)
        self._delete_temp_audio(self.preview_temp_audio_path)
        self.preview_audio_path = None
        self.preview_temp_audio_path = None
        self.preview_state = "idle"
        self.status_label.configure(text=status_text)
        self._refresh_preview_buttons()

    def pause_preview(self):
        """暂停试听。"""
        if self.preview_state != "playing":
            return
        try:
            self._mci_pause(self.preview_alias)
        except RuntimeError as exc:
            messagebox.showerror("暂停失败", str(exc))
            return
        self.preview_state = "paused"
        self.status_label.configure(text="试听已暂停")
        self._refresh_preview_buttons()

    def resume_preview(self):
        """继续播放试听。"""
        if self.preview_state != "paused":
            return
        try:
            self._mci_resume(self.preview_alias)
        except RuntimeError as exc:
            messagebox.showerror("播放失败", str(exc))
            return
        self.preview_state = "playing"
        self.status_label.configure(text="正在试听")
        self._refresh_preview_buttons()

    def stop_preview(self):
        """结束试听。"""
        if self.preview_state == "idle":
            return
        self._finish_preview("试听已结束")

    def speak_with_windows_tts(self, text):
        """调用 Windows 自带 System.Speech 进行中文语音播报。"""
        script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Volume = 100
$synth.Rate = 0
$synth.Speak({ps_quote(text)})
$synth.Dispose()
"""
        try:
            run_powershell_background(script)
        except OSError as exc:
            messagebox.showerror("TTS 启动失败", f"无法调用 Windows 语音服务：{exc}")

    def play_break_alert(self, break_seconds):
        """播放休息提醒，并保证不会超过休息倒计时。"""
        self.stop_preview()
        self.stop_break_alert()

        try:
            audio_path, temp_path, duration = self._prepare_alert_audio()
        except (ValueError, RuntimeError) as exc:
            messagebox.showwarning("提示音不可用", f"无法播放休息提示音：{exc}")
            return

        stop_early_ms = max(200, int(max(break_seconds - 0.5, 0.2) * 1000))
        stop_delay_ms = None

        if duration is None:
            messagebox.showwarning(
                "无法确认提示音时长",
                "程序无法确认当前提示音时长。为避免超过休息时间，提示音会在休息结束前提前停止。",
            )
            stop_delay_ms = stop_early_ms
        elif duration >= break_seconds:
            messagebox.showwarning(
                "提示音时长过长",
                f"当前提示音约 {duration:.1f} 秒，休息时长为 {break_seconds} 秒。\n"
                "休息提醒语音应短于休息时长，本次会在休息结束前提前停止播放。",
            )
            stop_delay_ms = stop_early_ms
        else:
            stop_delay_ms = int((duration + 0.5) * 1000)

        try:
            self._mci_play(self.break_alias, audio_path)
        except RuntimeError as exc:
            self._delete_temp_audio(temp_path)
            messagebox.showwarning("提示音不可用", f"无法播放休息提示音：{exc}")
            return

        self.break_temp_audio_path = temp_path
        self._schedule_break_alert_stop(stop_delay_ms)

    def _schedule_break_alert_stop(self, delay_ms):
        """安排停止休息提示音。"""
        if self.break_stop_id:
            try:
                self.root.after_cancel(self.break_stop_id)
            except tk.TclError:
                pass

        def stop_callback():
            self.break_stop_id = None
            self.stop_break_alert()

        self.break_stop_id = self.root.after(delay_ms, stop_callback)

    def stop_break_alert(self):
        """停止休息提示音并清理临时文件。"""
        if self.break_stop_id:
            try:
                self.root.after_cancel(self.break_stop_id)
            except tk.TclError:
                pass
            self.break_stop_id = None
        self._mci_stop(self.break_alias)
        self._delete_temp_audio(self.break_temp_audio_path)
        self.break_temp_audio_path = None

    def show_break_overlay(self, break_seconds=None):
        """显示全屏休息遮罩。"""
        if break_seconds is None:
            try:
                _work_seconds, break_seconds = self._read_settings()
            except ValueError:
                break_seconds = 20

        self.is_breaking = True
        self.break_time_left = break_seconds
        self.status_label.configure(text="休息中")

        self.overlay_windows = []
        monitors = get_monitor_rects(self.root)

        for index, (x, y, width, height) in enumerate(monitors):
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.geometry(f"{width}x{height}+{x}+{y}")
            win.configure(bg="#07111f")
            win.attributes("-topmost", True)
            win.protocol("WM_DELETE_WINDOW", self.disable_close)
            win.bind("<Alt-F4>", self.disable_event)
            win.bind("<Escape>", self.disable_event)
            win.bind("<Control-c>", self.disable_event)
            win.bind("<Control-Alt-u>", self.force_unlock_break)
            win.bind("<Control-Alt-U>", self.force_unlock_break)

            if index == 0:
                self._build_overlay_content(win)
                self.primary_overlay = win
            else:
                ttk.Label(
                    win,
                    text="休息时间",
                    foreground="#e5e7eb",
                    background="#07111f",
                    font=("Microsoft YaHei UI", 28, "bold"),
                ).pack(expand=True)

            self.overlay_windows.append(win)

        self.root.withdraw()
        self.root.bind_all("<Control-Alt-u>", self.force_unlock_break)
        self.root.bind_all("<Control-Alt-U>", self.force_unlock_break)
        self.primary_overlay.focus_force()
        self.primary_overlay.grab_set_global()
        self._tick_break()

    def _build_overlay_content(self, win):
        """创建休息遮罩中的主要提示内容。"""
        frame = tk.Frame(win, bg="#07111f")
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(
            frame,
            text="休息时间",
            bg="#07111f",
            fg="#f8fafc",
            font=("Microsoft YaHei UI", 42, "bold"),
        ).pack(pady=(0, 18))

        tk.Label(
            frame,
            text="请离开屏幕，看向 20 英尺（约 6 米）外的地方。",
            bg="#07111f",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 18),
        ).pack(pady=(0, 22))

        self.break_countdown_label = tk.Label(
            frame,
            text="",
            bg="#07111f",
            fg="#5eead4",
            font=("Consolas", 58, "bold"),
        )
        self.break_countdown_label.pack()

        self.break_lock_status = tk.Label(
            frame,
            text="休息结束后会自动解锁并返回工作倒计时。",
            bg="#07111f",
            fg="#94a3b8",
            font=("Microsoft YaHei UI", 12),
        )
        self.break_lock_status.pack(pady=(24, 0))

        tk.Label(
            frame,
            text="请保持离开屏幕，让眼睛完成这次休息。",
            bg="#07111f",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 10),
        ).pack(pady=(10, 0))

    def _tick_break(self):
        """休息倒计时每秒更新一次。"""
        if not self.is_breaking:
            return

        if self.break_time_left > 0:
            mins, secs = divmod(self.break_time_left, 60)
            self.break_countdown_label.configure(text=f"{mins:02d}:{secs:02d}")
            for win in self.overlay_windows:
                win.attributes("-topmost", True)
            self.primary_overlay.focus_force()
            self.break_time_left -= 1
            self.break_timer_id = self.root.after(1000, self._tick_break)
            return

        self.finish_break()

    def finish_break(self, restart_next=True, forced=False):
        """结束休息、解除锁定；正常结束时自动开始下一轮。"""
        self.is_breaking = False
        if self.break_timer_id:
            self.root.after_cancel(self.break_timer_id)
            self.break_timer_id = None

        self.stop_break_alert()
        self.root.unbind_all("<Control-Alt-u>")
        self.root.unbind_all("<Control-Alt-U>")

        try:
            self.primary_overlay.grab_release()
        except Exception:
            pass

        for win in self.overlay_windows:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.overlay_windows = []

        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        try:
            self._reset_timer_from_ui()
        except ValueError:
            self.total_work_seconds = int(float(DEFAULT_WORK_MINUTES) * 60)
            self.time_left = self.total_work_seconds

        if restart_next:
            self.speak_with_windows_tts("休息结束，继续加油吧。")
            self.status_label.configure(text="休息结束，已开始下一轮")
            self.start_timer()
        elif forced:
            self._set_timer_button_text("开始")
            self.status_label.configure(text="已通过组合键强制解锁，倒计时已停止")
        else:
            self._set_timer_button_text("开始")
            self.status_label.configure(text="休息已结束")

    def force_unlock_break(self, _event=None):
        """应急结束休息锁定，返回主界面并停止下一轮自动开始。"""
        if not self.is_breaking:
            return "break"
        self.finish_break(restart_next=False, forced=True)
        return "break"

    def disable_close(self):
        """休息遮罩不允许手动关闭。"""
        return None

    def disable_event(self, _event=None):
        """拦截按键、鼠标点击和关闭快捷键。"""
        return "break"

    def on_close(self):
        """主窗口关闭时清理定时器。"""
        if self.is_breaking:
            messagebox.showinfo("正在休息", "休息锁定中，倒计时结束后会自动关闭。")
            return

        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
        self.stop_preview()
        self.stop_break_alert()
        self.root.destroy()


if __name__ == "__main__":
    # Windows 高 DPI 屏幕下让 Tkinter 字体和控件更清晰。
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    app_root = tk.Tk()
    app = EyeCareApp(app_root)
    app_root.mainloop()
