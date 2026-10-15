// Background-processing notice for detached `octacam process` jobs.
//
// A job that is running while the GUI is open keeps going until its current
// step is done (a grid encode, one take's transfer, ...), then pauses until the
// GUI closes — the server marks this GUI session "capture-active", which every
// job checks between steps. Nothing here changes that; it only makes it
// visible: a banner over the camera grid while a job is running or paused, an
// event-log entry (and a desktop notification, if allowed) when it pauses or
// ends, and busyMessage() for the Record tab's "record anyway?" confirmation.
//
// Fed by /api/system's `jobs` and WS "jobs" messages: {live: [...], ended: [...]}
// (see AppState.refresh_jobs / process_jobs.notice_dict).

const PHASE_LABELS = {
  transcode: "transcoding",
  transfer: "copying data to the NAS",
  grid: "building a grid video",
  render: "rendering",
};

function phaseLabel(phase) {
  return PHASE_LABELS[phase] || phase || "starting";
}

export function formatEta(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  const min = Math.round(seconds / 60);
  if (min < 1) return "<1 min left";
  if (min < 60) return `~${min} min left`;
  return `~${Math.floor(min / 60)} h ${min % 60} min left`;
}

function isManual(job) {
  return (job.paused_reason || "").includes("manual");
}

// "item 3 of 16" — the step running now (1-based), or the count done when paused.
function stepText(job) {
  if (!job.files_total) return "";
  if (job.paused) return `${job.files_done} of ${job.files_total} done`;
  const current = Math.min(job.files_done + 1, job.files_total);
  return `${current} of ${job.files_total}`;
}

function warningsText(job) {
  const n = job.warning_count || 0;
  return n ? ` ⚠ ${n} warning${n === 1 ? "" : "s"} so far — see \`octacam jobs attach\`.` : "";
}

export function describeJob(job) {
  const step = stepText(job);
  if (job.paused) {
    const where = step ? ` (${phaseLabel(job.phase)}, ${step})` : "";
    return (
      (isManual(job)
        ? `Background processing is paused by hand${where} — resume it with ` +
          "`octacam jobs resume`."
        : `Background processing is paused${where} — it resumes when you close octacam.`) +
      warningsText(job)
    );
  }
  const parts = [job.current_file, step, formatEta(job.eta_s)].filter(Boolean);
  const detail = parts.length ? ` (${parts.join(", ")})` : "";
  return (
    `Background processing is ${phaseLabel(job.phase)}${detail}. ` +
    "It pauses once this step is done, until you close octacam." +
    warningsText(job)
  );
}

export class JobsNotice {
  constructor({ notify } = {}) {
    this.notify = notify || (() => {});
    this.banner = document.getElementById("jobs-banner");
    this.live = [];
    this._paused = new Map(); // job_id -> paused, as last seen
    this._ended = new Set(); // job ids already announced as ended
    this._first = true;
  }

  apply(jobs) {
    const live = Array.isArray(jobs?.live) ? jobs.live : [];
    const ended = Array.isArray(jobs?.ended) ? jobs.ended : [];
    // Announce transitions only — not what was already so when the page loaded.
    if (!this._first) {
      for (const job of live) {
        const was = this._paused.get(job.job_id);
        if (job.paused && was === false && !isManual(job)) {
          this._announce(
            "info",
            "Background processing paused — it resumes when you close octacam."
          );
        }
      }
      for (const job of ended) {
        if (this._ended.has(job.job_id)) continue;
        const n = job.warning_count || 0;
        if (job.state === "done" && !n) {
          this._announce("info", "Background processing finished.");
        } else if (job.state === "done") {
          const first = (job.warnings || [])[0];
          this._announce(
            "warning",
            `Background processing finished with ${n} warning${n === 1 ? "" : "s"}` +
              (first ? ` — first: ${first}` : "") +
              ` — review them with \`octacam jobs attach ${job.job_id}\`.`
          );
        } else {
          this._announce(
            job.state === "failed" ? "error" : "warning",
            `Background processing ${job.state}` + (job.error ? `: ${job.error}` : ".")
          );
        }
      }
    }
    for (const job of ended) this._ended.add(job.job_id);
    this._paused = new Map(live.map((j) => [j.job_id, Boolean(j.paused)]));
    this._first = false;
    this.live = live;
    this.render();
  }

  // A confirmation message while a job is still running a step (not yet
  // paused) — recording then shares the workstation with it. Null otherwise.
  busyMessage() {
    const busy = this.live.find((j) => !j.paused);
    if (!busy) return null;
    const eta = formatEta(busy.eta_s);
    return (
      `Background processing is still ${phaseLabel(busy.phase)}` +
      (busy.current_file ? ` (${busy.current_file}` + (eta ? `, ${eta}` : "") + ")" : "") +
      ". Recording now shares the workstation with it, which may overload the CPU. " +
      "It pauses by itself once this step is done.\n\nStart recording anyway?"
    );
  }

  render() {
    const banner = this.banner;
    if (!banner) return;
    banner.textContent = "";
    if (!this.live.length) {
      banner.classList.add("hidden");
      return;
    }
    for (const job of this.live) {
      const line = document.createElement("div");
      line.className = "jobs-line" + (job.paused ? " paused" : " running");
      line.textContent = describeJob(job);
      banner.appendChild(line);
    }
    if (canAskForNotifications()) {
      const ask = document.createElement("button");
      ask.type = "button";
      ask.className = "jobs-notify-btn";
      ask.textContent = "Notify me";
      ask.title = "Show a desktop notification when background processing pauses or ends";
      ask.addEventListener("click", async () => {
        try {
          await Notification.requestPermission();
        } catch {
          // Unsupported or blocked: the banner and event log still work.
        }
        this.render();
      });
      banner.appendChild(ask);
    }
    banner.classList.remove("hidden");
  }

  _announce(level, message) {
    this.notify(level, message);
    if (notificationsAllowed()) {
      try {
        new Notification("octacam", { body: message });
      } catch {
        // Some browsers only allow notifications from a service worker.
      }
    }
  }
}

function notificationsSupported() {
  return typeof window !== "undefined" && "Notification" in window && window.isSecureContext;
}

function canAskForNotifications() {
  return notificationsSupported() && Notification.permission === "default";
}

function notificationsAllowed() {
  return notificationsSupported() && Notification.permission === "granted";
}
