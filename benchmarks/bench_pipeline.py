"""Phase 0 benchmark: can a pure-Python octacam pipeline keep up?

Mirrors the C++ hot path: one grab thread per camera -> bounded queue
(drop on full) -> one writer
thread per camera, plus a ~30 Hz preview tap per camera simulating the GUI.

Runs against Basler's camera emulator (PYLON_CAMEMU) so no hardware is needed.

Examples:
    uv run benchmarks/bench_pipeline.py --writer null --fps 150
    uv run benchmarks/bench_pipeline.py --writer cv2-mjpg --copy zerocopy-hold --fps 150
    uv run benchmarks/bench_pipeline.py --writer cv2-mjpg --backend procs --fps 150
    uv run benchmarks/bench_pipeline.py --writer cv2-mjpg --trigger software --fps 150
"""

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("PYLON_CAMEMU", "8")

import numpy as np
from pypylon import genicam, pylon

GRAB_TIMEOUT_MS = 100  # matches the C++ GRAB_TIMEOUT_MS
SENTINEL = None


# ---------------------------------------------------------------------------
# Camera setup
# ---------------------------------------------------------------------------


def try_set(cam, name, value):
    try:
        node = getattr(cam, name)
        node.Value = value
        return True
    except (genicam.GenericException, AttributeError):
        return False


def open_camera(index, args):
    tlf = pylon.TlFactory.GetInstance()
    devices = tlf.EnumerateDevices()
    if len(devices) <= index:
        raise RuntimeError(
            f"only {len(devices)} camera(s) found, need index {index}; "
            f"is PYLON_CAMEMU set? (current: {os.environ.get('PYLON_CAMEMU')})"
        )
    cam = pylon.InstantCamera(tlf.CreateDevice(devices[index]))
    cam.Open()
    try_set(cam, "Width", args.width)
    try_set(cam, "Height", args.height)
    try_set(cam, "PixelFormat", "Mono8")
    # The emulator's default 10 ms simulated exposure caps it at ~88 fps;
    # 1 ms is realistic for 150 fps high-speed imaging. The emulator only
    # honors ExposureTimeAbs (writing ExposureTime succeeds but has no effect).
    if not try_set(cam, "ExposureTimeAbs", 1000.0):
        try_set(cam, "ExposureTime", 1000.0)
    if args.trigger == "freerun":
        try_set(cam, "TriggerMode", "Off")
        # Emulated cameras expose AcquisitionFrameRateAbs; real ones AcquisitionFrameRate.
        try_set(cam, "AcquisitionFrameRateEnable", True)
        if not try_set(cam, "AcquisitionFrameRate", float(args.fps)):
            try_set(cam, "AcquisitionFrameRateAbs", float(args.fps))
    else:
        try_set(cam, "TriggerSelector", "FrameStart")
        try_set(cam, "TriggerMode", "On")
        try_set(cam, "TriggerSource", "Software")
    if args.copy == "zerocopy-hold":
        # Held GrabResults pin buffers; the pool must exceed queue + in-flight.
        cam.MaxNumBuffer.Value = max(64, args.queue_size * 2)
    return cam


# ---------------------------------------------------------------------------
# Writers (one thread per camera, drop-on-full bounded queue)
# ---------------------------------------------------------------------------


def consume(item, copy_mode, fn):
    """Run fn(ndarray) on a queue item without an extra copy, then release it.

    pypylon's GetArrayZeroCopy raises on context exit if any reference to the
    view remains, so the view must stay confined to this function (del before
    exit, fn must not keep a reference).
    """
    if copy_mode == "array":
        fn(item)  # already an owned ndarray
        return
    try:
        with item.GetArrayZeroCopy() as view:
            fn(view)
            del view
    except (AttributeError, genicam.GenericException):
        fn(item.Array)  # fallback: copies
    item.Release()


def release(item, copy_mode):
    if copy_mode != "array":
        item.Release()


class WriterThread(threading.Thread):
    """Mirrors OpencvVideoWriter: bounded queue, drop on full, drain on close."""

    def __init__(self, kind, path, args):
        super().__init__(daemon=True)
        self.kind = kind
        self.path = path
        self.args = args
        self.queue = queue.Queue(maxsize=args.queue_size)
        self.written = 0
        self.encode_ns = 0
        self.max_depth = 0
        self._sink = self._open_sink()

    def _open_sink(self):
        w, h, fps = self.args.width, self.args.height, self.args.fps
        if self.kind == "cv2-mjpg":
            import cv2

            sink = cv2.VideoWriter(
                str(self.path),
                cv2.VideoWriter_fourcc(*"MJPG"),
                fps,
                (w, h),
                False,
            )
            if not sink.isOpened():
                raise RuntimeError(f"cv2.VideoWriter failed to open {self.path}")
            return sink
        if self.kind == "pyav-mjpeg":
            import av

            container = av.open(str(self.path), "w")
            stream = container.add_stream("mjpeg", rate=int(round(fps)))
            stream.width, stream.height = w, h
            stream.pix_fmt = "yuvj420p"
            return (container, stream)
        if self.kind == "ffmpeg-x264":
            import subprocess

            from octacam.writer import build_x264_args, find_ffmpeg

            args = build_x264_args(
                find_ffmpeg(),
                str(self.path),
                fps,
                w,
                h,
                self.args.crf,
                self.args.preset,
                "gray",
            )
            return subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        if self.kind == "copy-only":
            return np.empty((h, w), dtype=np.uint8)
        return None  # null

    def submit(self, item, copy_mode):
        """Called from the grab thread. Returns False if the frame was dropped."""
        depth = self.queue.qsize()
        if depth > self.max_depth:
            self.max_depth = depth
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            release(item, copy_mode)
            return False

    def run(self):
        copy_mode = self.args.copy
        while True:
            item = self.queue.get()
            if item is SENTINEL:
                break
            t0 = time.perf_counter_ns()
            self._write(item, copy_mode)
            self.encode_ns += time.perf_counter_ns() - t0
            self.written += 1
        self._close_sink()

    def _write(self, item, copy_mode):
        if self.kind == "null":
            release(item, copy_mode)
        elif self.kind == "copy-only":
            consume(item, copy_mode, lambda arr: np.copyto(self._sink, arr))
        elif self.kind == "cv2-mjpg":
            consume(item, copy_mode, self._sink.write)
        elif self.kind == "ffmpeg-x264":
            from octacam.writer import _write_all

            consume(item, copy_mode, lambda arr: _write_all(self._sink.stdin, arr))
        elif self.kind == "pyav-mjpeg":
            import av

            container, stream = self._sink
            holder = []
            consume(
                item,
                copy_mode,
                # from_ndarray copies, so the zero-copy view does not escape
                lambda arr: holder.append(
                    av.VideoFrame.from_ndarray(arr, format="gray")
                ),
            )
            for packet in stream.encode(holder[0].reformat(format="yuvj420p")):
                container.mux(packet)

    def _close_sink(self):
        if self.kind == "cv2-mjpg":
            self._sink.release()
        elif self.kind == "ffmpeg-x264":
            self._sink.stdin.close()
            self._sink.wait(timeout=30)
        elif self.kind == "pyav-mjpeg":
            container, stream = self._sink
            for packet in stream.encode():
                container.mux(packet)
            container.close()

    def stop(self):
        self.queue.put(SENTINEL)  # writer drains queued frames first, then exits
        self.join()


# ---------------------------------------------------------------------------
# Grab loop (one thread per camera), mirroring Camera::start_record
# ---------------------------------------------------------------------------


@dataclass
class CamStats:
    grabbed: int = 0
    dropped: int = 0
    skipped: int = 0
    timeouts: int = 0
    gil_ns: int = 0
    iter_ns: list = field(default_factory=list)


def grab_loop(cam, writer, stats, stop_evt, args):
    copy_mode = args.copy
    last_preview = 0.0
    cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
    while not stop_evt.is_set():
        res = cam.RetrieveResult(GRAB_TIMEOUT_MS, pylon.TimeoutHandling_Return)
        t0 = time.perf_counter_ns()
        if not res.GrabSucceeded():
            stats.timeouts += 1
            res.Release()
            continue
        skipped = res.GetNumberOfSkippedImages()
        if skipped:
            stats.skipped += skipped
        stats.grabbed += 1

        # Preview tap before submit, so the writer thread has exclusive
        # ownership of held GrabResults. Mirrors the C++ single-slot
        # FrameForDisplay: one clone per ~33 ms (the GUI consumption rate).
        now = time.monotonic()
        take_preview = now - last_preview >= 0.033

        if copy_mode == "array":
            item = res.Array  # the 2 MB copy, under the GIL
            res.Release()
            if take_preview:
                _ = item.copy()
                last_preview = now
        else:
            if take_preview:
                with res.GetArrayZeroCopy() as view:
                    _ = view.copy()
                    del view
                last_preview = now
            item = res

        if not writer.submit(item, copy_mode):
            stats.dropped += 1

        dt = time.perf_counter_ns() - t0
        stats.gil_ns += dt
        stats.iter_ns.append(dt)
    cam.StopGrabbing()


def trigger_loop(cams, fps, stop_evt):
    """Replicates the C++ PreciseTimer software-trigger thread."""
    period = 1.0 / fps
    next_t = time.perf_counter()
    while not stop_evt.is_set():
        # Matches Camera::trigger_once: fire whenever the camera is grabbing.
        for cam in cams:
            try:
                if cam.IsGrabbing():
                    cam.ExecuteSoftwareTrigger()
            except genicam.GenericException:
                pass
        next_t += period
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.perf_counter()  # fell behind; don't burst


def jitter_probe(samples, stop_evt):
    """Measures sleep-overshoot: a proxy for GIL convoys / scheduler delay."""
    while not stop_evt.is_set():
        t0 = time.perf_counter_ns()
        time.sleep(0.005)
        samples.append((time.perf_counter_ns() - t0) / 1e6 - 5.0)


# ---------------------------------------------------------------------------
# Single-process run (used directly for threads backend, per-camera for procs)
# ---------------------------------------------------------------------------


def run_cameras(indices, args, out_dir):
    cams = [open_camera(i, args) for i in indices]
    extension = "mkv" if args.writer == "ffmpeg-x264" else "avi"
    writers = [
        WriterThread(args.writer, out_dir / f"cam{i}.{extension}", args)
        for i in indices
    ]
    stats = [CamStats() for _ in indices]
    stop_evt = threading.Event()

    grabbers = [
        threading.Thread(target=grab_loop, args=(c, w, s, stop_evt, args), daemon=True)
        for c, w, s in zip(cams, writers, stats, strict=True)
    ]
    jitter = []
    threads = [
        threading.Thread(target=jitter_probe, args=(jitter, stop_evt), daemon=True)
    ]
    if args.trigger == "software":
        threads.append(
            threading.Thread(
                target=trigger_loop, args=(cams, args.fps, stop_evt), daemon=True
            )
        )

    try:
        import psutil

        proc = psutil.Process()
        proc.cpu_percent()
    except ImportError:
        proc = None

    for w in writers:
        w.start()
    for t in grabbers + threads:
        t.start()
    t_start = time.perf_counter()
    time.sleep(args.duration)
    stop_evt.set()
    for t in grabbers + threads:
        t.join()
    elapsed = time.perf_counter() - t_start
    cpu = proc.cpu_percent() if proc else float("nan")
    for w in writers:
        w.stop()
    for c in cams:
        c.Close()

    return {
        "elapsed": elapsed,
        "cpu_percent": cpu,
        "jitter_p99_ms": (
            statistics.quantiles(jitter, n=100)[98]
            if len(jitter) >= 100
            else float("nan")
        ),
        "cameras": [
            {
                "index": i,
                "grabbed": s.grabbed,
                "dropped": s.dropped,
                "skipped": s.skipped,
                "timeouts": s.timeouts,
                "written": w.written,
                "max_queue_depth": w.max_depth,
                "gil_ms_per_s": s.gil_ns / elapsed / 1e6,
                "gil_us_per_frame": (s.gil_ns / s.grabbed / 1e3) if s.grabbed else 0.0,
                "iter_p50_us": (
                    statistics.median(s.iter_ns) / 1e3 if s.iter_ns else 0.0
                ),
                "iter_p99_us": (
                    statistics.quantiles(s.iter_ns, n=100)[98] / 1e3
                    if len(s.iter_ns) >= 100
                    else 0.0
                ),
                "encode_ms_per_frame": (
                    w.encode_ns / w.written / 1e6 if w.written else 0.0
                ),
            }
            for i, s, w in zip(indices, stats, writers, strict=True)
        ],
    }


def proc_worker(index, args_dict, out_dir, result_q):
    args = argparse.Namespace(**args_dict)
    try:
        result = run_cameras([index], args, Path(out_dir))
        result_q.put((index, result))
    except Exception as e:  # surface child failures in the parent
        result_q.put((index, {"error": repr(e)}))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report(result, args):
    cams = result["cameras"]
    expected_per_cam = args.fps * args.duration
    total_grabbed = sum(c["grabbed"] for c in cams)
    total_dropped = sum(c["dropped"] for c in cams)
    total_skipped = sum(c["skipped"] for c in cams)
    achieved_fps = total_grabbed / result["elapsed"] / max(len(cams), 1)
    drop_rate = total_dropped / total_grabbed if total_grabbed else float("nan")
    gil_us_frame = (
        sum(c["gil_us_per_frame"] * c["grabbed"] for c in cams) / total_grabbed
        if total_grabbed
        else 0.0
    )
    gil_frac = sum(c["gil_ms_per_s"] for c in cams) / 1e3
    target_total_fps = 8 * 150
    extrapolated = gil_us_frame * 1e-6 * target_total_fps

    print()
    print(
        f"config: {len(cams)} cam(s) x {args.width}x{args.height} @ {args.fps} fps "
        f"({args.trigger}), writer={args.writer}, copy={args.copy}, "
        f"backend={args.backend}, queue={args.queue_size}, {args.duration:.0f}s"
    )
    print(
        f"{'cam':>4} {'grabbed':>8} {'fps':>7} {'drop':>6} {'skip':>5} {'qmax':>5} "
        f"{'gil us/fr':>9} {'p99 us':>8} {'enc ms':>7}"
    )
    for c in cams:
        fps = c["grabbed"] / result["elapsed"]
        print(
            f"{c['index']:>4} {c['grabbed']:>8} {fps:>7.1f} {c['dropped']:>6} "
            f"{c['skipped']:>5} {c['max_queue_depth']:>5} {c['gil_us_per_frame']:>9.0f} "
            f"{c['iter_p99_us']:>8.0f} {c['encode_ms_per_frame']:>7.2f}"
        )
    print(
        f"total: grabbed={total_grabbed} ({100 * total_grabbed / (expected_per_cam * len(cams)):.1f}% "
        f"of configured), achieved {achieved_fps:.1f} fps/cam, "
        f"dropped={total_dropped} ({100 * drop_rate:.3f}%), camera-skipped={total_skipped}"
    )
    print(
        f"GIL (grab loops): {100 * gil_frac:.1f}% measured | {gil_us_frame:.0f} us/frame "
        f"-> {100 * extrapolated:.1f}% extrapolated at 8x150 fps"
    )
    print(
        f"cpu={result['cpu_percent']:.0f}%  sleep-overshoot p99={result['jitter_p99_ms']:.2f} ms"
    )

    return {
        "achieved_fps_per_cam": achieved_fps,
        "drop_rate": drop_rate,
        "camera_skipped": total_skipped,
        "gil_frac_measured": gil_frac,
        "gil_us_per_frame": gil_us_frame,
        "gil_frac_extrapolated_8x150": extrapolated,
        "jitter_p99_ms": result["jitter_p99_ms"],
        "cpu_percent": result["cpu_percent"],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cameras", type=int, default=8)
    p.add_argument("--fps", type=float, default=150.0)
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument(
        "--writer",
        choices=["null", "copy-only", "cv2-mjpg", "pyav-mjpeg", "ffmpeg-x264"],
        default="cv2-mjpg",
    )
    p.add_argument("--crf", type=int, default=16)
    p.add_argument("--preset", default="ultrafast")
    p.add_argument("--copy", choices=["array", "zerocopy-hold"], default="array")
    p.add_argument("--backend", choices=["threads", "procs"], default="threads")
    p.add_argument("--trigger", choices=["freerun", "software"], default="freerun")
    p.add_argument("--queue-size", type=int, default=20)  # matches C++ writer queue
    p.add_argument("--output-dir", type=Path, default=Path("/tmp/octacam_bench"))
    p.add_argument(
        "--json",
        type=Path,
        default=Path(__file__).parent / "results" / "results.jsonl",
    )
    args = p.parse_args()

    os.environ["PYLON_CAMEMU"] = str(args.cameras)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.backend == "threads":
        result = run_cameras(list(range(args.cameras)), args, args.output_dir)
    else:
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        result_q = ctx.Queue()
        args_dict = {**vars(args), "output_dir": str(args.output_dir), "json": None}
        procs = [
            ctx.Process(
                target=proc_worker, args=(i, args_dict, str(args.output_dir), result_q)
            )
            for i in range(args.cameras)
        ]
        for pr in procs:
            pr.start()
        results = [result_q.get(timeout=args.duration + 60) for _ in procs]
        for pr in procs:
            pr.join()
        errors = [(i, r["error"]) for i, r in results if "error" in r]
        if errors:
            sys.exit(f"child process failures: {errors}")
        results.sort()
        result = {
            "elapsed": max(r["elapsed"] for _, r in results),
            "cpu_percent": sum(r["cpu_percent"] for _, r in results),
            "jitter_p99_ms": max(r["jitter_p99_ms"] for _, r in results),
            "cameras": [c for _, r in results for c in r["cameras"]],
        }

    summary = report(result, args)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "config": {
                k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()
            },
            "summary": summary,
            "cameras": result["cameras"],
        }
        with open(args.json, "a") as f:
            cams = record["cameras"]
            for c in cams:
                c.pop("iter_ns", None)
            f.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    main()
