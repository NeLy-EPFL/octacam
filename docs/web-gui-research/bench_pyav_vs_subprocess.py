"""Head-to-head: current subprocess-ffmpeg writer vs in-process PyAV.

Both encode identical 1080p GRAY8 frames to x264 (preset ultrafast, crf 16,
pix_fmt gray = monochrome 4:0:0), matching octacam.writer's production args.
Output goes to /dev/shm (tmpfs) so disk is never the bottleneck.

Two experiments:
  single   - one stream, default x264 threading (raw pipeline throughput)
  parallel - N streams at once, x264 threads=1 each. This isolates the
             real question: subprocess (process-per-encoder, GIL-free) vs
             PyAV threads (in-process, sharing one GIL). With each encoder
             pinned to 1 internal thread, scaling tells us purely whether
             Python-side overhead + GIL serializes the in-process path.
"""

import os
import subprocess
import sys
import threading
import time
from fractions import Fraction

import numpy as np

# repo src is two levels up from docs/web-gui-research/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from octacam.writer import _write_all, build_x264_args, find_ffmpeg  # noqa: E402

W, H = 1920, 1080
FPS = 30
CRF = 16
PRESET = "ultrafast"
SHM = "/dev/shm"


def make_frames(n):
    """n realistic-ish 1080p GRAY8 frames: a textured field panned
    horizontally (genuine motion -> real motion-compensation work),
    plus mild per-frame noise so it isn't perfectly predictable."""
    rng = np.random.default_rng(0)
    pan = n  # max horizontal shift
    k = 5
    # oversize by k so the box-blur still yields >= H rows and >= W+pan cols
    base = rng.integers(0, 256, size=(H + k, W + pan + k), dtype=np.uint8)
    # box-blur so it's textured, not white noise (white noise is the
    # pathological incompressible case and unrepresentative of camera video)
    cs = np.cumsum(np.cumsum(base.astype(np.uint32), axis=0), axis=1)
    cs = np.pad(cs, ((1, 0), (1, 0)))
    blur = (cs[k:, k:] - cs[:-k, k:] - cs[k:, :-k] + cs[:-k, :-k]) // (k * k)
    blur = blur.astype(np.uint8)
    bh, bw = blur.shape
    frames = []
    for i in range(n):
        sh = i % (bw - W) if bw > W else 0
        f = blur[:H, sh : sh + W].copy()
        noise = rng.integers(-4, 5, size=(H, W), dtype=np.int16)
        frames.append(np.clip(f.astype(np.int16) + noise, 0, 255).astype(np.uint8))
    return frames


# ---------------------------------------------------------------------------
# Approach A: production path - ffmpeg subprocess, block-feed every frame.
# ---------------------------------------------------------------------------
def encode_subprocess(frames, out, x264_threads=None):
    args = build_x264_args(find_ffmpeg(), out, FPS, W, H, CRF, PRESET, "gray")
    if x264_threads is not None:
        # insert -threads N right before output args (after -i source)
        i = args.index("-c:v")
        args[i:i] = ["-threads", str(x264_threads)]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    for f in frames:
        _write_all(proc.stdin, f)
    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg rc={rc}")


# ---------------------------------------------------------------------------
# Approach B: PyAV - in-process libx264 via the C library, no subprocess.
# ---------------------------------------------------------------------------
def encode_pyav(frames, out, x264_threads=None):
    import av

    container = av.open(out, mode="w")
    stream = container.add_stream("libx264", rate=FPS)
    stream.width = W
    stream.height = H
    stream.pix_fmt = "gray"
    stream.codec_context.options = {"preset": PRESET, "crf": str(CRF)}
    if x264_threads is not None:
        stream.codec_context.thread_count = x264_threads
    stream.codec_context.time_base = Fraction(1, FPS)
    for i, arr in enumerate(frames):
        frame = av.VideoFrame.from_ndarray(arr, format="gray")
        frame.pts = i
        frame.time_base = Fraction(1, FPS)
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():  # flush
        container.mux(pkt)
    container.close()


def run_single(frames):
    n = len(frames)
    print(f"\n=== SINGLE STREAM ({n} frames, {W}x{H}, default x264 threads) ===")
    for name, fn, ext in [
        ("subprocess", encode_subprocess, "mkv"),
        ("pyav", encode_pyav, "mkv"),
    ]:
        out = f"{SHM}/bench_single_{name}.{ext}"
        t0 = time.perf_counter()
        c0 = time.process_time()
        fn(frames, out)
        wall = time.perf_counter() - t0
        cpu = time.process_time() - c0  # python-process CPU only (excl. children)
        sz = os.path.getsize(out) / 1e6
        print(
            f"  {name:11s}: {wall:6.2f}s  {n / wall:7.1f} fps  "
            f"py-cpu={cpu:5.2f}s  out={sz:.1f}MB"
        )


def run_parallel(frames, nstreams, x264_threads):
    n = len(frames)
    total = n * nstreams
    tlabel = f"threads={x264_threads}" if x264_threads else "default threads"
    print(
        f"\n=== PARALLEL x{nstreams} ({n} frames each = {total} total, "
        f"x264 {tlabel} per stream) ==="
    )
    for name, fn, ext in [
        ("subprocess", encode_subprocess, "mkv"),
        ("pyav", encode_pyav, "mkv"),
    ]:
        outs = [f"{SHM}/bench_par_{name}_{i}.{ext}" for i in range(nstreams)]
        threads = [
            threading.Thread(
                target=fn, args=(frames, outs[i]), kwargs={"x264_threads": x264_threads}
            )
            for i in range(nstreams)
        ]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        print(
            f"  {name:11s}: {wall:6.2f}s  aggregate {total / wall:8.1f} fps  "
            f"({n / wall:6.1f} fps/stream)"
        )


if __name__ == "__main__":
    nframes = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    nstreams = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    print(f"Generating {nframes} frames...")
    frames = make_frames(nframes)
    run_single(frames)
    run_parallel(frames, nstreams, x264_threads=1)  # controlled: 1 thread each
    run_parallel(frames, nstreams, x264_threads=None)  # production-realistic: unpinned
