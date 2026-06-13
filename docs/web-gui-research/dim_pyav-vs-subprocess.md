QUESTION:
Should the recording path switch from the current design (one ffmpeg child process per camera, fed raw GRAY8 frames over a pipe — see src/octacam/writer.py) to in-process encoding via PyAV (libav* bindings)? Motivation considered: PyAV is "more direct" (no subprocess, pipe, or stderr-drain plumbing) and might be faster. Both ship a one-command-install ffmpeg (imageio-ffmpeg bundles the CLI binary; PyAV statically links the libav* libraries), so distribution is a wash — the real questions are throughput and robustness.

RECOMMENDATION:
Keep imageio-ffmpeg + the subprocess writer. PyAV is genuinely more direct in code, but on the workload that defines this tool — 8 cameras encoding 1080p gray concurrently — it is ~23-30% SLOWER and, worse, hard-capped by the GIL so the gap cannot be tuned away. It also forfeits process-level failure isolation (an in-process libav fault would kill all 8 recordings at once, vs one camera with subprocesses). The cleanliness is not worth a throughput regression on the one workload that matters. Revisit only if Python free-threading (3.13+ no-GIL) removes the cap, or if PyAV becomes attractive for some other reason — in which case the only way to keep the parallelism would be PyAV-in-subprocesses (multiprocessing), which discards the very directness that motivated the swap.

SETUP:
Benchmarked on the actual acquisition machine (scapelinux, 20 logical cores). PyAV 17.1.0 / libavcodec 62.28. Identical synthetic 1080p GRAY8 frames (textured field panned horizontally for genuine motion-compensation work + mild per-frame noise; white noise avoided as pathological) fed into BOTH paths. Encoder settings matched octacam.writer production args exactly: libx264, preset ultrafast, crf 16, pix_fmt gray (true monochrome 4:0:0). Output to /dev/shm (tmpfs) so disk is never the bottleneck. Output file sizes matched to 0.1% (248.5 vs 248.3 MB), confirming PyAV honored pix_fmt=gray and produced the same 4:0:0 stream (a silent 4:2:0 fallback would be ~33% larger). Script: docs/web-gui-research/bench_pyav_vs_subprocess.py. Numbers stable across 3 runs.

RESULTS (600 frames/stream, aggregate fps):

  Scenario                                   subprocess     PyAV        winner
  single stream, default x264 threads          ~893 fps    ~551 fps     subprocess +62%
  8x parallel, 1 x264 thread each (controlled)  ~812 fps    ~910 fps     PyAV +12%
  8x parallel, default x264 threads (PROD)     ~1178 fps    ~905 fps     subprocess +30%

The bottom row is the production configuration (8 cameras, un-pinned x264 threads, exactly what writer.py does today) and it reproduces the ">1200 fps aggregate / 150 fps x 8" figure validated in dim_encoding.md — so the harness is realistic. There, PyAV is ~23-30% slower. The middle row is the only PyAV win and is an artificial pinned config that is itself slower (905) than what production already achieves (1178).

WHY (and why it is not tunable):
PyAV's aggregate is GIL-capped at ~905 fps. It returns ~113 fps/stream whether x264 runs 1 internal thread or many (113.5 vs 113.1) — more encoder threads buy nothing, because the ceiling is not the encoder. It is the per-frame Python work that MUST hold the GIL: VideoFrame.from_ndarray (a ~2 MB memcpy), container.mux, pts bookkeeping, multiplied across 8 threads. libx264 releases the GIL while encoding, but the 8 worker threads serialize on that Python-side glue. The subprocess design sidesteps it entirely: 8 independent OS processes, no shared GIL, each child's libx264 freely uses cores -> scales to 1178 fps. The single-stream diagnostic confirms the mechanism from the other side: py-process CPU was 7.8s over 1.1s wall for PyAV (libx264 worker threads run INSIDE the Python process and get throttled) vs 0.3s for subprocess (those threads run invisibly in the child).

This benchmark UNDERSTATES the gap. In production the same 20 cores also run 8 grab loops, the FastAPI/uvicorn server, and preview JPEG encoding — all Python, all competing for the same GIL PyAV's encoders need. The subprocess design keeps encode CPU out of the Python process entirely; PyAV would put it back in contention.

ROBUSTNESS:
Subprocess crash is isolated — writer.py flags that one camera's writes as failed and the other 7 keep recording (writer.py _on_sink_failure). An in-process libav segfault takes down the whole Python process and all 8 recordings simultaneously. Independent of speed, this favors keeping subprocesses for a long-running unattended acquisition tool.

WHAT PYAV WOULD HAVE SIMPLIFIED (the real upside, for the record):
No pipe, no _write_all partial-write handling, no stderr-drain thread, no subprocess lifecycle/finalize-timeout logic (writer.py:264-340 shrinks substantially). transcode_raw and _remux become library calls instead of subprocess.run. This is a genuine readability win — just not one worth 30% throughput plus the loss of isolation.
