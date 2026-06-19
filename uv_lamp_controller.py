from __future__ import annotations

import asyncio
import csv
import datetime as dt
import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # The GUI still opens far enough to explain what is missing.
    BleakClient = None
    BleakScanner = None


BLE_DEVICE_NAME = "ThermoCouple"
BLE_SERVICE_UUID = "7f3fd100-9a7e-4f4f-a5f1-f6c5437fd801"
BLE_DATA_UUID = "7f3fd101-9a7e-4f4f-a5f1-f6c5437fd801"
BLE_COMMAND_UUID = "7f3fd102-9a7e-4f4f-a5f1-f6c5437fd801"
BLE_SCAN_TIMEOUT_S = 6.0
BLE_CONNECT_TIMEOUT_S = BLE_SCAN_TIMEOUT_S + 4.0
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_CONTROL_AVERAGE_SAMPLES = 5
DEFAULT_MIN_RELAY_DWELL_S = 1.0


@dataclass
class Sample:
    wall_time: dt.datetime
    elapsed_s: float
    arduino_ms: int
    thermocouple_c: float
    internal_c: float
    ok: bool
    fault_bits: int
    raw: str
    lamp: str


class TrendPlot(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style="Panel.TFrame")
        self.samples: list[Sample] = []
        self.lower_limit: float | None = None
        self.upper_limit: float | None = None

        self.canvas = tk.Canvas(
            self,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground="#c9ced6",
        )
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

    def set_limits(self, lower: float, upper: float) -> None:
        self.lower_limit = lower
        self.upper_limit = upper
        self.redraw()

    def set_samples(self, samples: list[Sample]) -> None:
        self.samples = samples
        self.redraw()

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 220)
        left = 64
        right = 22
        top = 24
        bottom = 46
        plot_w = width - left - right
        plot_h = height - top - bottom

        canvas.create_rectangle(left, top, width - right, height - bottom, fill="#fbfcfd", outline="#c9ced6")

        valid = [sample for sample in self.samples if sample.ok and math.isfinite(sample.thermocouple_c)]
        if not valid:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for temperature data",
                fill="#667085",
                font=("Segoe UI", 12),
            )
            self._draw_axis_labels(width, height, left, bottom)
            return

        x_min = valid[0].elapsed_s
        x_max = valid[-1].elapsed_s
        if x_max <= x_min:
            x_max = x_min + 1.0

        y_values = [sample.thermocouple_c for sample in valid]
        if self.lower_limit is not None:
            y_values.append(self.lower_limit)
        if self.upper_limit is not None:
            y_values.append(self.upper_limit)

        y_min = min(y_values)
        y_max = max(y_values)
        y_pad = max((y_max - y_min) * 0.12, 1.0)
        y_min -= y_pad
        y_max += y_pad

        def x_map(value: float) -> float:
            return left + ((value - x_min) / (x_max - x_min)) * plot_w

        def y_map(value: float) -> float:
            return top + (1.0 - ((value - y_min) / (y_max - y_min))) * plot_h

        if self.lower_limit is not None and self.upper_limit is not None and self.lower_limit < self.upper_limit:
            zone_top = y_map(self.upper_limit)
            zone_bottom = y_map(self.lower_limit)
            canvas.create_rectangle(left, zone_top, width - right, zone_bottom, fill="#edf8f0", outline="")

        self._draw_grid(canvas, left, top, width - right, height - bottom, x_min, x_max, y_min, y_max)

        if self.lower_limit is not None:
            y = y_map(self.lower_limit)
            canvas.create_line(left, y, width - right, y, fill="#2e8b57", width=2, dash=(5, 4))
            canvas.create_text(width - right - 6, y - 9, text=f"Lower {self.lower_limit:.2f}", anchor="e", fill="#2e8b57")

        if self.upper_limit is not None:
            y = y_map(self.upper_limit)
            canvas.create_line(left, y, width - right, y, fill="#b54708", width=2, dash=(5, 4))
            canvas.create_text(width - right - 6, y - 9, text=f"Upper {self.upper_limit:.2f}", anchor="e", fill="#b54708")

        points: list[float] = []
        for sample in valid:
            points.extend([x_map(sample.elapsed_s), y_map(sample.thermocouple_c)])
        if len(points) >= 4:
            canvas.create_line(*points, fill="#1f6feb", width=2.5)
        else:
            x = x_map(valid[0].elapsed_s)
            y = y_map(valid[0].thermocouple_c)
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#1f6feb", outline="")

        self._draw_linear_trend(canvas, valid, x_min, x_max, x_map, y_map)
        self._draw_axis_labels(width, height, left, bottom)

    def _draw_grid(
        self,
        canvas: tk.Canvas,
        left: int,
        top: int,
        right: int,
        bottom: int,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> None:
        for i in range(6):
            x = left + (right - left) * i / 5
            elapsed = x_min + (x_max - x_min) * i / 5
            canvas.create_line(x, top, x, bottom, fill="#e6eaf0")
            canvas.create_text(x, bottom + 16, text=self._format_elapsed(elapsed), fill="#667085", font=("Segoe UI", 9))

            y = top + (bottom - top) * i / 5
            value = y_max - (y_max - y_min) * i / 5
            canvas.create_line(left, y, right, y, fill="#e6eaf0")
            canvas.create_text(left - 10, y, text=f"{value:.1f}", anchor="e", fill="#667085", font=("Segoe UI", 9))

    def _draw_axis_labels(self, width: int, height: int, left: int, bottom: int) -> None:
        self.canvas.create_text(width / 2, height - 12, text="Elapsed time", fill="#344054", font=("Segoe UI", 9, "bold"))
        self.canvas.create_text(23, (height - bottom) / 2 + 8, text="Temp (C)", angle=90, fill="#344054", font=("Segoe UI", 9, "bold"))

    def _draw_linear_trend(
        self,
        canvas: tk.Canvas,
        samples: list[Sample],
        x_min: float,
        x_max: float,
        x_map,
        y_map,
    ) -> None:
        if len(samples) < 3:
            return

        n = len(samples)
        sx = sum(sample.elapsed_s for sample in samples)
        sy = sum(sample.thermocouple_c for sample in samples)
        sxx = sum(sample.elapsed_s * sample.elapsed_s for sample in samples)
        sxy = sum(sample.elapsed_s * sample.thermocouple_c for sample in samples)
        denominator = n * sxx - sx * sx
        if abs(denominator) < 1e-9:
            return

        slope = (n * sxy - sx * sy) / denominator
        intercept = (sy - slope * sx) / n
        y_start = intercept + slope * x_min
        y_end = intercept + slope * x_max
        canvas.create_line(
            x_map(x_min),
            y_map(y_start),
            x_map(x_max),
            y_map(y_end),
            fill="#c46a00",
            width=2,
            dash=(7, 4),
        )
        canvas.create_text(
            x_map(x_max) - 6,
            y_map(y_end) + 12,
            text="Trend",
            anchor="e",
            fill="#9a4d00",
            font=("Segoe UI", 9, "bold"),
        )

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:d}:{sec:02d}"


class UVLampControllerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("UV Lamp Controller")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(background="#e9edf2")

        self.ble_client = None
        self.ble_loop: asyncio.AbstractEventLoop | None = None
        self.ble_connected_event = threading.Event()
        self.ble_connect_error: str | None = None
        self.connected_port: str | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.polling_connection = False
        self.connection_queue: queue.Queue[tuple[str, str]] = queue.Queue()

        self.samples: list[Sample] = []
        self.control_filter_values: list[float] = []
        self.running = False
        self.run_start = 0.0
        self.sample_zero_arduino_ms: int | None = None
        self.sample_zero_wall_time: dt.datetime | None = None
        self.timer_end = 0.0
        self.goal_duration_s = 0
        self.uv_on_accumulated_s = 0.0
        self.uv_on_since_s: float | None = None
        self.startup_heating = False
        self.last_lamp_change_s = 0.0
        self.last_lamp_command: str | None = None
        self.csv_handle = None
        self.csv_writer = None
        self.log_path: Path | None = None

        self.lower_var = tk.StringVar(value="26.0")
        self.upper_var = tk.StringVar(value="30.0")
        self.hours_var = tk.StringVar(value="0")
        self.minutes_var = tk.StringVar(value="30")
        self.seconds_var = tk.StringVar(value="0")
        self.goal_mode_var = tk.StringVar(value="total")
        self.status_var = tk.StringVar(value="Idle")
        self.current_temp_var = tk.StringVar(value="--.-- C")
        self.internal_temp_var = tk.StringVar(value="--.-- C")
        self.lamp_var = tk.StringVar(value="OFF")
        self.timer_var = tk.StringVar(value="00:30:00")
        self.uv_timer_var = tk.StringVar(value="00:00:00")
        self.log_var = tk.StringVar(value="No active log")

        self._configure_styles()
        self._build_ui()
        self.status_var.set("BLE ready")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#e9edf2", foreground="#1f2937")
        style.configure("TFrame", background="#e9edf2")
        style.configure("Panel.TFrame", background="#f6f8fb", relief="solid", borderwidth=1)
        style.configure("PanelBody.TFrame", background="#f6f8fb")
        style.configure("Header.TFrame", background="#1f2937")
        style.configure("Header.TLabel", background="#1f2937", foreground="#ffffff", font=("Segoe UI", 16, "bold"))
        style.configure("HeaderSmall.TLabel", background="#1f2937", foreground="#cbd5e1", font=("Segoe UI", 9))
        style.configure("PanelTitle.TLabel", background="#f6f8fb", foreground="#344054", font=("Segoe UI", 10, "bold"))
        style.configure("Metric.TLabel", background="#f6f8fb", foreground="#101828", font=("Segoe UI", 15, "bold"))
        style.configure("Muted.TLabel", background="#f6f8fb", foreground="#667085")
        style.configure("Status.TLabel", background="#344054", foreground="#ffffff", padding=(10, 4), font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=(12, 7))
        style.configure("Start.TButton", background="#1f7a4d", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("Start.TButton", background=[("active", "#17613d"), ("disabled", "#a7c8b7")])
        style.configure("Stop.TButton", background="#b42318", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("Stop.TButton", background=[("active", "#8f1d14"), ("disabled", "#e8b4af")])
        style.configure("Reset.TButton", background="#475467", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("Reset.TButton", background=[("active", "#344054")])
        style.configure("LampOn.TButton", background="#0b6bcb", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("LampOn.TButton", background=[("active", "#0959a8"), ("disabled", "#a9c9e8")])
        style.configure("LampOff.TButton", background="#7a271a", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        style.map("LampOff.TButton", background=[("active", "#5c1d13"), ("disabled", "#d6afa8")])
        style.configure("Treeview", rowheight=26, background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 12))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="UV Lamp Controller", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="MAX31855 + UNO R4 WiFi + D7 relay", style="HeaderSmall.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

        controls = ttk.Frame(self, style="Panel.TFrame", padding=14)
        controls.grid(row=1, column=0, sticky="nsw", padx=(14, 8), pady=14)
        controls.columnconfigure(0, weight=1)

        self._build_connection_panel(controls).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._build_limits_panel(controls).grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self._build_timer_panel(controls).grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self._build_run_panel(controls).grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self._build_log_panel(controls).grid(row=4, column=0, sticky="ew")

        main = ttk.Frame(self)
        main.grid(row=1, column=1, sticky="nsew", padx=(8, 14), pady=14)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        main.rowconfigure(2, weight=0)

        metrics = ttk.Frame(main, style="Panel.TFrame", padding=12)
        metrics.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for column in range(5):
            metrics.columnconfigure(column, weight=1)
        self._metric(metrics, 0, "Thermocouple", self.current_temp_var)
        self._metric(metrics, 1, "Chip temp", self.internal_temp_var)
        self._metric(metrics, 2, "Lamp command", self.lamp_var)
        self._metric(metrics, 3, "UV on time", self.uv_timer_var)
        self._metric(metrics, 4, "Goal remaining", self.timer_var)

        self.plot = TrendPlot(main)
        self.plot.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        table_panel = ttk.Frame(main, style="Panel.TFrame", padding=10)
        table_panel.grid(row=2, column=0, sticky="nsew")
        table_panel.columnconfigure(0, weight=1)
        ttk.Label(table_panel, text="Live Data", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))

        columns = ("time", "elapsed", "uv_on", "thermo", "control", "internal", "lamp", "fault")
        self.table = ttk.Treeview(table_panel, columns=columns, show="headings", height=8)
        headings = {
            "time": "Time",
            "elapsed": "Elapsed",
            "uv_on": "UV On",
            "thermo": "Thermocouple (C)",
            "control": "Control (C)",
            "internal": "Chip Temp (C)",
            "lamp": "Lamp",
            "fault": "Fault",
        }
        widths = {"time": 90, "elapsed": 85, "uv_on": 85, "thermo": 115, "control": 100, "internal": 100, "lamp": 70, "fault": 105}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="center")
        self.table.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_panel, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)

    def _build_connection_panel(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="BLE Connection", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.connect_button = ttk.Button(frame, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=1, column=0, sticky="ew")
        ttk.Label(
            frame,
            text=f"Scans for {BLE_DEVICE_NAME}",
            style="Muted.TLabel",
            wraplength=250,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        return frame

    def _build_limits_panel(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        frame.columnconfigure((0, 1), weight=1)
        ttk.Label(frame, text="Temperature Zone", style="PanelTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Lower (C)", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="Upper (C)", style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Entry(frame, textvariable=self.lower_var, width=10).grid(row=2, column=0, sticky="ew", pady=(3, 0))
        ttk.Entry(frame, textvariable=self.upper_var, width=10).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))
        return frame

    def _build_timer_panel(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        frame.columnconfigure((0, 1, 2), weight=1)
        ttk.Label(frame, text="Run Timer", style="PanelTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        for column, (label, variable, limit) in enumerate(
            (("Hours", self.hours_var, 99), ("Minutes", self.minutes_var, 59), ("Seconds", self.seconds_var, 59))
        ):
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=1, column=column, sticky="w")
            tk.Spinbox(
                frame,
                from_=0,
                to=limit,
                textvariable=variable,
                width=7,
                justify="center",
                relief="solid",
                bd=1,
                font=("Segoe UI", 10),
            ).grid(row=2, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0), pady=(3, 0))
        goal_frame = ttk.Frame(frame, style="PanelBody.TFrame")
        goal_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(goal_frame, text="Goal", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(
            goal_frame,
            text="Total time",
            value="total",
            variable=self.goal_mode_var,
            command=self._refresh_idle_timer_display,
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            goal_frame,
            text="UV time",
            value="uv",
            variable=self.goal_mode_var,
            command=self._refresh_idle_timer_display,
        ).pack(side="left", padx=(12, 0))
        return frame

    def _build_run_panel(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        frame.columnconfigure((0, 1, 2), weight=1)
        ttk.Label(frame, text="Run Control", style="PanelTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.start_button = ttk.Button(frame, text="Start", style="Start.TButton", command=self.start_run)
        self.start_button.grid(row=1, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = ttk.Button(frame, text="Stop", style="Stop.TButton", command=lambda: self.stop_run("Stopped by user"))
        self.stop_button.grid(row=1, column=1, sticky="ew", padx=(5, 5))
        self.stop_button.state(["disabled"])
        self.reset_button = ttk.Button(frame, text="Reset", style="Reset.TButton", command=self.reset_run)
        self.reset_button.grid(row=1, column=2, sticky="ew", padx=(5, 0))
        ttk.Label(frame, text="Manual Lamp", style="Muted.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 3))
        self.manual_on_button = ttk.Button(frame, text="Lamp ON", style="LampOn.TButton", command=lambda: self.manual_set_lamp(True))
        self.manual_on_button.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 5))
        self.manual_off_button = ttk.Button(frame, text="Lamp OFF", style="LampOff.TButton", command=lambda: self.manual_set_lamp(False))
        self.manual_off_button.grid(row=3, column=2, sticky="ew", padx=(5, 0))
        self.manual_on_button.state(["disabled"])
        self.manual_off_button.state(["disabled"])
        return frame

    def _build_log_panel(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="CSV Logging", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(frame, textvariable=self.log_var, style="Muted.TLabel", wraplength=250, justify="left").grid(row=1, column=0, sticky="ew")
        return frame

    @staticmethod
    def _metric(parent: tk.Misc, column: int, title: str, variable: tk.StringVar) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 4))
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, text=title, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=variable, style="Metric.TLabel").pack(anchor="w")

    def toggle_connection(self) -> None:
        if self.running:
            return

        if self._is_connected():
            self.disconnect_connection("Disconnected")
        else:
            self.connect_ble()

    def connect_ble(self) -> bool:
        if self._is_ble_connected():
            return True

        if BleakClient is None or BleakScanner is None:
            messagebox.showerror("Missing dependency", "Install bleak first:\n\npython -m pip install -r requirements.txt")
            return False

        self._clear_connection_queue()
        self.stop_event.clear()
        self.ble_connected_event.clear()
        self.ble_connect_error = None
        self.status_var.set(f"Scanning for {BLE_DEVICE_NAME} BLE...")
        self.reader_thread = threading.Thread(target=self._ble_thread_loop, name="ble-reader", daemon=True)
        self.reader_thread.start()
        self._start_connection_polling()

        deadline = time.monotonic() + BLE_CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.ble_connected_event.wait(timeout=0.05):
                break
            if self.reader_thread is not None and not self.reader_thread.is_alive() and self.ble_connect_error:
                break
            self.update_idletasks()

        if self._is_ble_connected():
            self.status_var.set(f"Connected to {self.connected_port or BLE_DEVICE_NAME}")
            self._update_connection_controls()
            return True

        reason = self.ble_connect_error or f"Could not find {BLE_DEVICE_NAME} over BLE."
        self.disconnect_connection(reason, release_lamp=False)
        messagebox.showerror("BLE connection", reason)
        return False

    def _ble_thread_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self.ble_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ble_client_session())
        except Exception as exc:
            self.ble_connect_error = f"BLE error: {exc}"
            if not self.stop_event.is_set():
                self.connection_queue.put(("error", self.ble_connect_error))
        finally:
            self.ble_client = None
            self.ble_loop = None
            self.ble_connected_event.clear()
            try:
                loop.close()
            except Exception:
                pass

    async def _ble_client_session(self) -> None:
        device = await BleakScanner.find_device_by_filter(self._ble_device_matches, timeout=BLE_SCAN_TIMEOUT_S)
        if device is None:
            self.ble_connect_error = f"Could not find {BLE_DEVICE_NAME} over BLE."
            return

        name = device.name or BLE_DEVICE_NAME
        self.connected_port = f"BLE {name} ({device.address})"
        async with BleakClient(device, disconnected_callback=self._ble_disconnected_callback) as client:
            self.ble_client = client
            await client.start_notify(BLE_DATA_UUID, self._ble_notification_handler)
            self.ble_connected_event.set()
            self.connection_queue.put(("status", f"Connected to {self.connected_port}"))

            while not self.stop_event.is_set() and client.is_connected:
                await asyncio.sleep(0.1)

            if client.is_connected:
                try:
                    await client.stop_notify(BLE_DATA_UUID)
                except Exception:
                    pass

    @staticmethod
    def _ble_device_matches(device, advertisement_data) -> bool:
        advertised_name = (device.name or advertisement_data.local_name or "").strip()
        advertised_uuids = {uuid.lower() for uuid in (advertisement_data.service_uuids or [])}
        return advertised_name == BLE_DEVICE_NAME or BLE_SERVICE_UUID.lower() in advertised_uuids

    def _ble_notification_handler(self, _sender, data: bytearray) -> None:
        text = bytes(data).decode("utf-8", errors="replace").replace("\x00", "").strip()
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.connection_queue.put(("line", line))

    def _ble_disconnected_callback(self, _client) -> None:
        if not self.stop_event.is_set():
            self.connection_queue.put(("error", "BLE disconnected"))

    def disconnect_connection(self, reason: str, release_lamp: bool = True) -> None:
        if self.running:
            self.stop_run(reason)

        if release_lamp and self._is_connected():
            self.set_lamp(True, force=True)
            if self._is_ble_connected():
                time.sleep(0.15)

        reader_thread = self.reader_thread
        self.stop_event.set()
        if self._is_ble_connected() and self.ble_loop is not None and self.ble_client is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self.ble_client.disconnect(), self.ble_loop)
                future.result(timeout=1.0)
            except Exception:
                pass

        if reader_thread is not None and reader_thread.is_alive() and reader_thread is not threading.current_thread():
            reader_thread.join(timeout=1.5)

        self.ble_client = None
        self.ble_loop = None
        self.ble_connected_event.clear()
        self.ble_connect_error = None
        self.connected_port = None
        self.reader_thread = None
        self.last_lamp_command = None
        self.lamp_var.set("ON" if release_lamp else "OFF")
        self._clear_connection_queue()
        self._update_connection_controls()
        self.status_var.set(reason)

    def _is_ble_connected(self) -> bool:
        return self.ble_client is not None and bool(getattr(self.ble_client, "is_connected", False))

    def _is_connected(self) -> bool:
        return self._is_ble_connected()

    def _connection_name(self) -> str:
        return self.connected_port or BLE_DEVICE_NAME

    def _start_connection_polling(self) -> None:
        if not self.polling_connection:
            self.polling_connection = True
            self.after(100, self._poll_connection_queue)

    def _update_connection_controls(self) -> None:
        connected = self._is_connected()

        self.connect_button.configure(text="Disconnect" if connected else "Connect")
        if self.running:
            self.connect_button.state(["disabled"])
        elif connected:
            self.connect_button.state(["!disabled"])
        else:
            self.connect_button.state(["!disabled"])

        manual_state = ["!disabled"] if connected and not self.running else ["disabled"]
        self.manual_on_button.state(manual_state)
        self.manual_off_button.state(manual_state)

    def start_run(self) -> None:
        if self.running:
            return

        self._clear_connection_queue()

        try:
            lower = float(self.lower_var.get())
            upper = float(self.upper_var.get())
        except ValueError:
            messagebox.showerror("Temperature zone", "Lower and upper temperatures must be numbers.")
            return
        if lower >= upper:
            messagebox.showerror("Temperature zone", "Lower temperature must be less than upper temperature.")
            return

        try:
            duration_s = self._timer_seconds()
        except ValueError:
            messagebox.showerror("Run timer", "Timer fields must be whole numbers.")
            return
        if duration_s <= 0:
            messagebox.showerror("Run timer", "Set a timer longer than 0 seconds.")
            return

        if not self.connect_ble():
            return
        port = self._connection_name()

        self.samples.clear()
        self.control_filter_values.clear()
        for item in self.table.get_children():
            self.table.delete(item)
        self.current_temp_var.set("--.-- C")
        self.internal_temp_var.set("--.-- C")
        self.lamp_var.set("OFF")
        self.plot.set_limits(lower, upper)
        self.plot.set_samples(self.samples)

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        DATA_DIR.mkdir(exist_ok=True)
        self.log_path = DATA_DIR / f"uv_lamp_log_{timestamp}.csv"
        self.csv_handle = self.log_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_handle)
        self.csv_writer.writerow(
            [
                "timestamp",
                "elapsed_s",
                "uv_on_s",
                "goal_mode",
                "arduino_ms",
                "thermocouple_C",
                "control_temp_C",
                "internal_C",
                "sensor_ok",
                "fault_bits",
                "raw",
                "lamp",
            ]
        )
        self.log_var.set(str(self.log_path))

        self.running = True
        self.run_start = time.monotonic()
        self.sample_zero_arduino_ms = None
        self.sample_zero_wall_time = None
        self.goal_duration_s = duration_s
        self.timer_end = self.run_start + duration_s
        self.uv_on_accumulated_s = 0.0
        self.uv_on_since_s = None
        self.startup_heating = True
        self.last_lamp_change_s = 0.0
        self.last_lamp_command = None
        self.status_var.set(f"Warm-up to upper on {port}")
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self._update_connection_controls()
        self.timer_var.set(self._format_duration(duration_s))
        self.uv_timer_var.set("00:00:00")

        self.set_lamp(False, force=True)
        self.last_lamp_change_s = 0.0
        self.after(200, self._update_timer)

    def reset_run(self) -> None:
        if self.running:
            self.stop_run("Reset")
        elif self._is_connected():
            self.set_lamp(False, force=True)

        self._clear_connection_queue()
        self.samples.clear()
        self.control_filter_values.clear()
        for item in self.table.get_children():
            self.table.delete(item)

        self.current_temp_var.set("--.-- C")
        self.internal_temp_var.set("--.-- C")
        self.lamp_var.set("OFF")
        self.goal_duration_s = 0
        self.sample_zero_arduino_ms = None
        self.sample_zero_wall_time = None
        self.uv_on_accumulated_s = 0.0
        self.uv_on_since_s = None
        self.startup_heating = False
        self.last_lamp_change_s = 0.0
        self.last_lamp_command = None
        self.log_path = None
        self.log_var.set("No active log")
        self.timer_var.set(self._format_timer_value())
        self.uv_timer_var.set("00:00:00")

        try:
            lower = float(self.lower_var.get())
            upper = float(self.upper_var.get())
            if lower < upper:
                self.plot.set_limits(lower, upper)
        except ValueError:
            pass
        self.plot.set_samples(self.samples)
        self.status_var.set("Ready for next run")

    def stop_run(self, reason: str) -> None:
        was_running = self.running
        if not was_running and not self._is_connected():
            return

        self.running = False
        self.startup_heating = False
        self.set_lamp(False, force=True)

        if self.csv_handle is not None:
            try:
                self.csv_handle.flush()
                self.csv_handle.close()
            finally:
                self.csv_handle = None
                self.csv_writer = None

        self.start_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        self._update_connection_controls()
        self.status_var.set(reason)
        self.lamp_var.set("OFF")
        self.uv_timer_var.set(self._format_duration(self._current_uv_on_s()))

    def manual_set_lamp(self, on: bool) -> None:
        if self.running:
            self.status_var.set("Manual lamp disabled during recipe")
            return

        if not self._is_connected():
            self.status_var.set("Connect before manual lamp control")
            return

        self.set_lamp(on, force=True)
        self.status_var.set(f"Manual lamp {'ON' if on else 'OFF'} sent")

    def set_lamp(self, on: bool, force: bool = False) -> None:
        command_state = "ON" if on else "OFF"
        if not force and self.last_lamp_command == command_state:
            return

        if not force:
            dwell_remaining = self._lamp_dwell_remaining_s()
            if dwell_remaining > 0:
                self.status_var.set(f"Relay dwell hold: {math.ceil(dwell_remaining)} s")
                return

        now = time.monotonic()
        state_changed = self.last_lamp_command != command_state
        if state_changed:
            self._track_uv_lamp_transition(on, now)

        self.last_lamp_command = command_state
        if state_changed:
            self.last_lamp_change_s = now
        self.lamp_var.set(command_state)
        self.uv_timer_var.set(self._format_duration(self._current_uv_on_s(now)))
        self._send_command(f"LAMP_{command_state}")

    def _track_uv_lamp_transition(self, lamp_on: bool, now: float) -> None:
        if lamp_on:
            self.uv_on_since_s = now
            return

        if self.uv_on_since_s is not None:
            self.uv_on_accumulated_s += max(0.0, now - self.uv_on_since_s)
            self.uv_on_since_s = None

    def _current_uv_on_s(self, now: float | None = None) -> float:
        total = self.uv_on_accumulated_s
        if self.uv_on_since_s is not None:
            total += max(0.0, (now or time.monotonic()) - self.uv_on_since_s)
        return total

    def _send_command(self, command: str) -> None:
        if self._is_ble_connected():
            self._send_ble_command(command)

    def _send_ble_command(self, command: str) -> None:
        if self.ble_loop is None or self.ble_client is None:
            return

        async def write_command() -> None:
            if self.ble_client is not None and self.ble_client.is_connected:
                await self.ble_client.write_gatt_char(BLE_COMMAND_UUID, f"{command}\n".encode("ascii"), response=True)

        future = asyncio.run_coroutine_threadsafe(write_command(), self.ble_loop)

        def report_result(done_future) -> None:
            try:
                exc = done_future.exception()
            except Exception as err:
                self.connection_queue.put(("error", f"BLE write failed: {err}"))
                return
            if exc is not None:
                self.connection_queue.put(("error", f"BLE write failed: {exc}"))

        future.add_done_callback(report_result)

    def _clear_connection_queue(self) -> None:
        while True:
            try:
                self.connection_queue.get_nowait()
            except queue.Empty:
                break

    def _lamp_dwell_remaining_s(self) -> float:
        if self.last_lamp_change_s <= 0:
            return 0.0
        return max(0.0, self._min_lamp_dwell_s() - (time.monotonic() - self.last_lamp_change_s))

    def _poll_connection_queue(self) -> None:
        while True:
            try:
                kind, payload = self.connection_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "error":
                if self.running:
                    self.stop_run(f"Connection error: {payload}")
                self.disconnect_connection(f"Connection error: {payload}", release_lamp=False)
                return

            if kind == "status":
                self.status_var.set(payload)
                continue

            self._handle_connection_line(payload)

        if not self._is_connected():
            self._clear_connection_queue()
            self.polling_connection = False
            return

        self.after(100, self._poll_connection_queue)

    def _handle_connection_line(self, line: str) -> None:
        if line.startswith("DATA,"):
            if self.running:
                sample = self._parse_sample(line)
                if sample is not None:
                    self._record_sample(sample)
        elif line == "READY":
            self.status_var.set("Device ready")
        elif line.startswith("ACK,"):
            self.status_var.set(line.replace(",", ": ", 1))
        elif line.startswith("ERR,"):
            self.status_var.set(line)

    def _parse_sample(self, line: str) -> Sample | None:
        parts = line.split(",")
        if len(parts) != 8:
            return None
        try:
            arduino_ms = int(parts[1])
            wall_time, elapsed_s = self._sample_timing(arduino_ms)
            return Sample(
                wall_time=wall_time,
                elapsed_s=elapsed_s,
                arduino_ms=arduino_ms,
                thermocouple_c=float(parts[2]),
                internal_c=float(parts[3]),
                ok=parts[4] == "1",
                fault_bits=int(parts[5]),
                raw=parts[6],
                lamp=parts[7],
            )
        except ValueError:
            return None

    def _sample_timing(self, arduino_ms: int) -> tuple[dt.datetime, float]:
        if self.sample_zero_arduino_ms is None or self.sample_zero_wall_time is None:
            self.sample_zero_arduino_ms = arduino_ms
            self.sample_zero_wall_time = dt.datetime.now()

        elapsed_s = self._millis_delta_ms(self.sample_zero_arduino_ms, arduino_ms) / 1000.0
        wall_time = self.sample_zero_wall_time + dt.timedelta(seconds=elapsed_s)
        return wall_time, elapsed_s

    @staticmethod
    def _millis_delta_ms(start_ms: int, current_ms: int) -> int:
        return (current_ms - start_ms) & 0xFFFFFFFF

    def _record_sample(self, sample: Sample) -> None:
        self.samples.append(sample)
        control_temp = self._update_control_filter(sample)
        uv_on_s = self._current_uv_on_s()
        if self.csv_writer is not None and self.csv_handle is not None:
            self.csv_writer.writerow(
                [
                    sample.wall_time.isoformat(timespec="seconds"),
                    f"{sample.elapsed_s:.3f}",
                    f"{uv_on_s:.3f}",
                    self.goal_mode_var.get(),
                    sample.arduino_ms,
                    f"{sample.thermocouple_c:.3f}",
                    "" if control_temp is None else f"{control_temp:.3f}",
                    f"{sample.internal_c:.3f}",
                    int(sample.ok),
                    sample.fault_bits,
                    sample.raw,
                    sample.lamp,
                ]
            )
            self.csv_handle.flush()

        self.current_temp_var.set(f"{sample.thermocouple_c:.2f} C")
        self.internal_temp_var.set(f"{sample.internal_c:.2f} C")

        fault_text = "OK" if sample.ok else f"Fault {sample.fault_bits}"
        control_text = "--" if control_temp is None else f"{control_temp:.2f}"
        self.table.insert(
            "",
            0,
            values=(
                sample.wall_time.strftime("%H:%M:%S"),
                TrendPlot._format_elapsed(sample.elapsed_s),
                self._format_duration(uv_on_s),
                f"{sample.thermocouple_c:.2f}",
                control_text,
                f"{sample.internal_c:.2f}",
                sample.lamp,
                fault_text,
            ),
        )
        rows = self.table.get_children()
        if len(rows) > 250:
            self.table.delete(rows[-1])

        self.plot.set_samples(self.samples)
        self._apply_temperature_control(sample, control_temp)

    def _update_control_filter(self, sample: Sample) -> float | None:
        if not sample.ok or not math.isfinite(sample.thermocouple_c):
            return None

        self.control_filter_values.append(sample.thermocouple_c)
        window_size = self._filter_window_size()
        if len(self.control_filter_values) > window_size:
            del self.control_filter_values[:-window_size]

        return sum(self.control_filter_values) / len(self.control_filter_values)

    def _apply_temperature_control(self, sample: Sample, control_temp: float | None) -> None:
        if not self.running:
            return

        try:
            lower = float(self.lower_var.get())
            upper = float(self.upper_var.get())
        except ValueError:
            self.set_lamp(False, force=True)
            return

        if not sample.ok:
            self.startup_heating = False
            self.set_lamp(False, force=True)
            self.status_var.set("Sensor fault: lamp off")
        elif control_temp is None:
            self.set_lamp(False, force=True)
        elif self.startup_heating:
            if control_temp >= upper:
                self.startup_heating = False
                self.set_lamp(False, force=True)
                self.status_var.set("Upper reached: band control")
            else:
                self.set_lamp(True)
                self.status_var.set("Warm-up: heating to upper")
        elif control_temp <= lower:
            self.set_lamp(True)
        elif control_temp >= upper:
            self.set_lamp(False)

    def _update_timer(self) -> None:
        if not self.running:
            return

        now = time.monotonic()
        uv_on_s = self._current_uv_on_s(now)
        self.uv_timer_var.set(self._format_duration(uv_on_s))

        if self.goal_mode_var.get() == "uv":
            remaining_s = self.goal_duration_s - uv_on_s
            complete_reason = "UV timer complete"
        else:
            remaining_s = self.timer_end - now
            complete_reason = "Timer complete"

        remaining = max(0, int(math.ceil(remaining_s)))
        self.timer_var.set(self._format_duration(remaining))

        if remaining_s <= 0:
            self.stop_run(complete_reason)
            return

        self.after(250, self._update_timer)

    def _timer_seconds(self) -> int:
        hours = int(self.hours_var.get())
        minutes = int(self.minutes_var.get())
        seconds = int(self.seconds_var.get())
        return hours * 3600 + minutes * 60 + seconds

    def _filter_window_size(self) -> int:
        return DEFAULT_CONTROL_AVERAGE_SAMPLES

    def _min_lamp_dwell_s(self) -> float:
        return DEFAULT_MIN_RELAY_DWELL_S

    def _format_timer_value(self) -> str:
        try:
            seconds_total = max(0, self._timer_seconds())
        except ValueError:
            seconds_total = 0
        return self._format_duration(seconds_total)

    def _refresh_idle_timer_display(self) -> None:
        if not self.running:
            self.timer_var.set(self._format_timer_value())
            self.uv_timer_var.set("00:00:00")

    @staticmethod
    def _format_duration(seconds_total: float) -> str:
        seconds_total = max(0, int(seconds_total))
        hours, remainder = divmod(seconds_total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def on_close(self) -> None:
        if self.running:
            self.stop_run("Closed")
        if self._is_connected():
            self.disconnect_connection("Closed")
        self.destroy()


if __name__ == "__main__":
    app = UVLampControllerApp()
    app.mainloop()
