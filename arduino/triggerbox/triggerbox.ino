#include <Arduino.h>
#include "esp_timer.h"

// =============================================================================
//  triggerbox — configurable camera-trigger + light controller (protocol v2)
//
//  Target board: Arduino Nano ESP32 (ESP32-S3), mounted on the EPFL
//  "common CCS light and camera trigger circuit" (common-trigger-circuit).
//
//  The host (octacam's `triggerbox` plugin, or the bundled triggerbox_send.py)
//  sends a self-describing arm packet over USB serial. Unlike the older fixed
//  single-line/shared-duty firmware, every output is chosen at RUN TIME:
//    * any number (<= MAX_CAM) of camera-trigger lines, each with its own pin,
//      pulse width, and phase delay;
//    * up to 3 light channels, each independently one of four modes:
//        off / strobe (frame-locked) / continuous / pulse_train (opto).
//  Pins are addressed by an index into a fixed label table (below), so the host
//  can drive whichever screw-terminal lines the rig is actually wired to without
//  reflashing. All three CCS channels are electrically identical (NPN sink,
//  active-HIGH), so any channel can do any job — there is no fixed illumination
//  vs. stimulation role.
//
//  Timing is software (micros() for the frame clock; esp_timer for the run
//  clock). Every frame-locked edge is derived from one per-frame t0, so cameras
//  and strobe lights stay phase-locked. Outputs are written with digitalWrite on
//  transitions only; several edges landing on the same loop iteration are
//  emitted sequentially (~1-2 us apart on the S3) — negligible against the frame
//  period and the strobe guard, but not literally simultaneous. (A register-
//  batched / hardware-timer variant is possible future work if ns coincidence is
//  ever needed.)
//
//  Light polarity: each CCS channel drives an NPN sink transistor, so an Arduino
//  pin HIGH sinks the channel to GND -> light ON.
//
//  ---- Host -> Arduino wire protocol (little-endian) -------------------------
//    Arm:      [0xA5]                                     magic
//              [version : u8 = 2]
//              [payload_len : u16]                        bytes that follow, pre-checksum
//              --- payload (payload_len bytes) ---
//              [fps : u16]                                1..5000
//              [duration_ms : u32]                        0 = run until cancel
//              [n_cam : u8]  [n_light : u8]
//              n_cam   x camera_record (5 B): [pin_id u8][pulse_us u16][delay_us u16]
//              n_light x light_record  (18 B): [pin_id u8][mode u8][p0 u32][p1 u32][p2 u32][p3 u32]
//              --- end payload ---
//              [checksum : u8]                            XOR over version+len+payload
//    Cancel:   [0xCA]
//    Identify: [0x3F] '?'   -> replies "TRIGGERBOX 2\n"
//
//    light mode fields:
//      0 off        : hold LOW
//      1 strobe     : p0 = phase_delay_us (from frame t0), p1 = on_us   (frame-locked)
//      2 continuous : ON for the whole run
//      3 pulse_train: p0 = pulse_us, p1 = interval_us (=1e6/freq),
//                     p2 = start_delay_us, p3 = train_duration_us (0 = whole run)
//
//  ---- Arduino -> Host (newline-terminated ASCII tokens) ---------------------
//    "R\n"  running    — arm accepted, clocks started
//    "D\n"  done       — requested duration elapsed, back to idle
//    "C\n"  cancelled  — cancel received (or a valid re-arm superseded a run)
//    "E<c>\n" rejected — arm refused; <c> is a reason code:
//                          v version   c checksum   o oversize/count
//                          l length    f fps        p bad pin id
//                          r reserved pin            d duplicate pin   m bad mode
//    "TRIGGERBOX 2\n"    reply to an identify request
// =============================================================================

// ---- Canonical pin-label table (SINGLE SOURCE OF TRUTH) --------------------
// Index -> Arduino pin. Mirrored byte-for-byte by PIN_LABELS in the Python
// plugin; a unit test asserts the two stay in sync. Indices 0/1/2 (D2/D3/D4)
// are reserved for the status LED and rejected as trigger/light outputs.
//   idx:   0   1   2   3   4   5   6   7   8    9   10   11   12  13  14  15  16  17  18  19
//   label: D2  D3  D4  D5  D6  D7  D8  D9  D10  D11 D12  D13  A0  A1  A2  A3  A4  A5  A6  A7
constexpr uint8_t kNumPins = 20;
static const uint8_t kPinTable[kNumPins] = {
    D2, D3, D4, D5, D6, D7, D8, D9, D10, D11,
    D12, D13, A0, A1, A2, A3, A4, A5, A6, A7};
constexpr uint32_t kReservedMask = 0x7;  // idx 0,1,2 = status LED

// Status RGB LED (board indicator on D2/D3/D4; HIGH = colour on).
constexpr uint8_t kLedRed   = D2;
constexpr uint8_t kLedGreen = D3;
constexpr uint8_t kLedBlue  = D4;

// ---- Serial protocol constants ---------------------------------------------
constexpr uint8_t  kArmMagic        = 0xA5;
constexpr uint8_t  kCancelMagic     = 0xCA;
constexpr uint8_t  kIdentifyMagic   = 0x3F;  // '?'
constexpr uint8_t  kProtocolVersion = 2;
constexpr uint32_t kArmStaleMs      = 250;   // discard a partial packet after this
constexpr long     kBaudRate        = 115200;
constexpr char     kVersion[]       = "TRIGGERBOX 2";

constexpr uint16_t kMaxFps   = 5000;
constexpr uint8_t  kMaxCam   = 12;
constexpr uint8_t  kMaxLight = 3;
// Fixed payload prefix (fps,duration,n_cam,n_light) + records.
constexpr uint16_t kMaxPayload = 8 + 5 * kMaxCam + 18 * kMaxLight;  // 122
// Frame buffer holds [version, len_lo, len_hi, ...payload..., checksum].
constexpr uint16_t kBufSize = 3 + kMaxPayload + 1;                  // 126

constexpr uint32_t kDefaultCamPulseUs = 500;
constexpr uint32_t kMinCamPulseUs     = 5;
constexpr uint32_t kMinLedOnUs        = 1;

// ---- Light modes -----------------------------------------------------------
constexpr uint8_t kModeOff        = 0;
constexpr uint8_t kModeStrobe     = 1;
constexpr uint8_t kModeContinuous = 2;
constexpr uint8_t kModePulseTrain = 3;

// ---- Output records --------------------------------------------------------
struct Cam {
  uint8_t  gpio;
  uint32_t delay_us;
  uint32_t pulse_us;
  bool     high;
};
struct Light {
  uint8_t  gpio;
  uint8_t  mode;
  uint32_t p0, p1, p2, p3;
  uint64_t cyc_us;  // pulse_train: next cycle start, relative to run start
  bool     high;
};

static Cam   g_cam[kMaxCam];
static Light g_lt[kMaxLight];
static uint8_t g_ncam   = 0;
static uint8_t g_nlight = 0;

// Every pin currently driven as an output — so idle/re-arm can force the exact
// set that was last active LOW, regardless of which runtime pins were chosen.
static uint8_t g_active_gpio[kMaxCam + kMaxLight];
static uint8_t g_active_n = 0;

// ---- State machine ---------------------------------------------------------
enum class State : uint8_t { IDLE, RUNNING };
static State    g_state         = State::IDLE;
static uint32_t g_period_us     = 0;
static uint32_t g_duration_ms   = 0;
static uint32_t g_run_start_ms  = 0;
static uint32_t g_frame_t0_us   = 0;           // micros() at current frame edge
static uint64_t g_run_start_us  = 0;           // esp_timer_get_time() at arm (64-bit)

// ---- Non-blocking packet reassembly ----------------------------------------
enum class Rx : uint8_t { CMD, COLLECT };
static Rx       g_rx          = Rx::CMD;
static uint8_t  g_buf[kBufSize];
static uint16_t g_got         = 0;
static uint16_t g_need        = 0;    // total bytes to collect for the frame
static uint16_t g_want_len    = 0;    // payload_len from the header
static uint32_t g_deadline_ms = 0;

// ---- Little-endian readers -------------------------------------------------
static inline uint16_t rd16(const uint8_t *b) {
  return (uint16_t)b[0] | ((uint16_t)b[1] << 8);
}
static inline uint32_t rd32(const uint8_t *b) {
  return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) |
         ((uint32_t)b[3] << 24);
}

// ---- Output helpers --------------------------------------------------------
static void set_status(bool r, bool g, bool b) {
  digitalWrite(kLedRed, r ? HIGH : LOW);
  digitalWrite(kLedGreen, g ? HIGH : LOW);
  digitalWrite(kLedBlue, b ? HIGH : LOW);
}

static inline void set_level(uint8_t gpio, bool &cur, bool want) {
  if (want != cur) {
    digitalWrite(gpio, want ? HIGH : LOW);
    cur = want;
  }
}

static void drive_active_low() {
  for (uint8_t i = 0; i < g_active_n; i++) digitalWrite(g_active_gpio[i], LOW);
  for (uint8_t i = 0; i < g_ncam; i++) g_cam[i].high = false;
  for (uint8_t i = 0; i < g_nlight; i++) g_lt[i].high = false;
}

static void emit(char c) {
  Serial.write(c);
  Serial.write('\n');
}
static void reject(char code) {
  Serial.write('E');
  Serial.write(code);
  Serial.write('\n');
}

// Recompute every output's level from the clocks and apply the transitions.
static void apply_outputs(uint32_t elapsed, uint64_t run) {
  for (uint8_t i = 0; i < g_ncam; i++) {
    Cam &c = g_cam[i];
    bool w = (elapsed >= c.delay_us) && (elapsed < c.delay_us + c.pulse_us);
    set_level(c.gpio, c.high, w);
  }
  for (uint8_t i = 0; i < g_nlight; i++) {
    Light &L = g_lt[i];
    bool w = false;
    switch (L.mode) {
      case kModeStrobe:
        w = (elapsed >= L.p0) && (elapsed < L.p0 + L.p1);
        break;
      case kModeContinuous:
        w = true;
        break;
      case kModePulseTrain:
        if (run < (uint64_t)L.p2) {
          w = false;
        } else if (L.p3 && run >= (uint64_t)L.p2 + (uint64_t)L.p3) {
          w = false;
        } else {
          while (run - L.cyc_us >= (uint64_t)L.p1) L.cyc_us += L.p1;
          w = (run - L.cyc_us) < (uint64_t)L.p0;
        }
        break;
      default:  // kModeOff
        w = false;
        break;
    }
    set_level(L.gpio, L.high, w);
  }
}

// ---- State transitions -----------------------------------------------------
static void enter_idle(char reason) {
  drive_active_low();
  g_active_n = 0;
  g_ncam = 0;
  g_nlight = 0;
  g_state = State::IDLE;
  set_status(false, true, false);  // green = idle / ready
  if (reason) emit(reason);
}

static void enter_running(uint16_t fps, uint32_t duration_ms, const Cam *cams,
                          uint8_t ncam, const Light *lts, uint8_t nlight) {
  // A valid re-arm supersedes any run in progress; tell the host the old ended.
  bool was_running = (g_state == State::RUNNING);
  drive_active_low();
  if (was_running) emit('C');

  g_period_us = (1000000UL + fps / 2) / fps;
  if (g_period_us < 2) g_period_us = 2;

  g_active_n = 0;

  g_ncam = ncam;
  for (uint8_t i = 0; i < ncam; i++) {
    Cam c = cams[i];
    pinMode(c.gpio, OUTPUT);
    digitalWrite(c.gpio, LOW);
    uint32_t delay = c.delay_us;
    if (delay > g_period_us - 1) delay = g_period_us - 1;
    uint32_t pulse = c.pulse_us ? c.pulse_us : kDefaultCamPulseUs;
    if (pulse < kMinCamPulseUs) pulse = kMinCamPulseUs;
    uint32_t maxp = (g_period_us > delay + 1) ? (g_period_us - 1 - delay) : 1;
    if (pulse > maxp) pulse = maxp;
    g_cam[i] = {c.gpio, delay, pulse, false};
    g_active_gpio[g_active_n++] = c.gpio;
  }

  g_nlight = nlight;
  for (uint8_t i = 0; i < nlight; i++) {
    Light L = lts[i];
    pinMode(L.gpio, OUTPUT);
    digitalWrite(L.gpio, LOW);
    L.high = false;
    L.cyc_us = L.p2;  // first pulse_train cycle starts at start_delay
    if (L.mode == kModeStrobe) {
      if (L.p0 > g_period_us - 1) L.p0 = g_period_us - 1;  // clamp phase delay
      if (L.p1 >= g_period_us) {
        L.mode = kModeContinuous;  // on-time >= period -> never strobes off
      } else if (L.p1 > 0 && L.p1 < kMinLedOnUs) {
        L.p1 = kMinLedOnUs;
      }
    } else if (L.mode == kModePulseTrain) {
      if (L.p1 == 0) {
        L.mode = kModeOff;  // interval 0 is undefined
      } else {
        if (L.p0 >= L.p1) L.p0 = L.p1 - 1;  // pulse < interval
        if (L.p0 < 1) L.p0 = 1;
      }
    }
    g_lt[i] = L;
    g_active_gpio[g_active_n++] = L.gpio;
  }

  g_duration_ms = duration_ms;
  g_run_start_ms = millis();
  g_frame_t0_us = micros();
  g_run_start_us = (uint64_t)esp_timer_get_time();
  g_state = State::RUNNING;
  set_status(true, false, false);  // red = recording
  apply_outputs(0, 0);             // emit the first frame's edges immediately
  emit('R');
}

// ---- Arm parse + validate (staging; commits only on full success) ----------
static void parse_and_validate(const uint8_t *payload, uint16_t len) {
  if (len < 8) { reject('l'); return; }
  uint16_t fps = rd16(payload);
  uint32_t duration_ms = rd32(payload + 2);
  uint8_t ncam = payload[6];
  uint8_t nlight = payload[7];
  if (fps < 1 || fps > kMaxFps) { reject('f'); return; }
  if (ncam > kMaxCam || nlight > kMaxLight) { reject('o'); return; }
  if (len != (uint16_t)(8 + 5 * ncam + 18 * nlight)) { reject('l'); return; }

  Cam tmpc[kMaxCam];
  Light tmpl[kMaxLight];
  uint32_t used = 0;
  const uint8_t *p = payload + 8;

  for (uint8_t i = 0; i < ncam; i++) {
    uint8_t idx = p[0];
    if (idx >= kNumPins) { reject('p'); return; }
    if ((1u << idx) & kReservedMask) { reject('r'); return; }
    if (used & (1u << idx)) { reject('d'); return; }
    used |= (1u << idx);
    tmpc[i].gpio = kPinTable[idx];
    tmpc[i].pulse_us = rd16(p + 1);
    tmpc[i].delay_us = rd16(p + 3);
    tmpc[i].high = false;
    p += 5;
  }
  for (uint8_t i = 0; i < nlight; i++) {
    uint8_t idx = p[0];
    uint8_t mode = p[1];
    if (idx >= kNumPins) { reject('p'); return; }
    if ((1u << idx) & kReservedMask) { reject('r'); return; }
    if (used & (1u << idx)) { reject('d'); return; }
    if (mode > kModePulseTrain) { reject('m'); return; }
    used |= (1u << idx);
    tmpl[i].gpio = kPinTable[idx];
    tmpl[i].mode = mode;
    tmpl[i].p0 = rd32(p + 2);
    tmpl[i].p1 = rd32(p + 6);
    tmpl[i].p2 = rd32(p + 10);
    tmpl[i].p3 = rd32(p + 14);
    tmpl[i].cyc_us = 0;
    tmpl[i].high = false;
    p += 18;
  }

  enter_running(fps, duration_ms, tmpc, ncam, tmpl, nlight);
}

// ---- setup / loop ----------------------------------------------------------
void setup() {
  // Status LED pins are dedicated outputs.
  pinMode(kLedRed, OUTPUT);
  pinMode(kLedGreen, OUTPUT);
  pinMode(kLedBlue, OUTPUT);
  // Every other allowlisted pin: hold LOW via an internal pulldown until the
  // first arm chooses it as an output. (The old firmware pre-drove its fixed
  // pins LOW; with runtime pins we can't know them yet, so keep them from
  // floating into a camera trigger input.)
  for (uint8_t idx = 3; idx < kNumPins; idx++) pinMode(kPinTable[idx], INPUT_PULLDOWN);

  // No blocking `while (!Serial)` — must come up even without a host attached.
  Serial.begin(kBaudRate);
  enter_idle(0);  // green, no status token
}

void loop() {
  // ---- Serial ingest: one byte per iteration so the frame clock never stalls.
  if (Serial.available() > 0) {
    const uint8_t b = (uint8_t)Serial.read();
    if (g_rx == Rx::COLLECT) {
      g_buf[g_got++] = b;
      if (g_got == 3) {
        // Header complete: [version, len_lo, len_hi].
        g_want_len = (uint16_t)g_buf[1] | ((uint16_t)g_buf[2] << 8);
        if (g_want_len > kMaxPayload) {
          reject('o');
          g_rx = Rx::CMD;
        } else {
          g_need = 3 + g_want_len + 1;  // + checksum
        }
      } else if (g_got == g_need) {
        // Full frame collected: [version,len,payload,checksum].
        uint8_t sum = 0;
        for (uint16_t i = 0; i < (uint16_t)(3 + g_want_len); i++) sum ^= g_buf[i];
        if (sum != g_buf[g_need - 1]) {
          reject('c');
        } else if (g_buf[0] != kProtocolVersion) {
          reject('v');
        } else {
          parse_and_validate(g_buf + 3, g_want_len);
        }
        g_rx = Rx::CMD;
      }
    } else if (b == kCancelMagic) {
      enter_idle('C');
    } else if (b == kIdentifyMagic) {
      Serial.print(kVersion);
      Serial.write('\n');
    } else if (b == kArmMagic) {
      g_rx = Rx::COLLECT;
      g_got = 0;
      g_need = 3;  // collect the header first
      g_deadline_ms = millis() + kArmStaleMs;
    }
    // else: unknown byte discarded to stay byte-aligned.
  }

  // Discard a partial packet that never completed.
  if (g_rx == Rx::COLLECT &&
      (int32_t)(millis() - g_deadline_ms) >= 0) {
    g_rx = Rx::CMD;
  }

  // ---- Clocks (RUNNING only) -----------------------------------------------
  if (g_state == State::RUNNING) {
    uint32_t now = micros();
    uint32_t elapsed = now - g_frame_t0_us;  // wraparound-safe
    if (elapsed >= g_period_us) {
      if (elapsed >= 2UL * g_period_us) {
        g_frame_t0_us = now;  // slipped a whole frame: resync, no burst
      } else {
        g_frame_t0_us += g_period_us;
      }
      elapsed = now - g_frame_t0_us;
    }
    uint64_t run = (uint64_t)((uint64_t)esp_timer_get_time() - g_run_start_us);
    apply_outputs(elapsed, run);

    if (g_duration_ms != 0 && (millis() - g_run_start_ms) >= g_duration_ms) {
      enter_idle('D');
    }
  }
}
