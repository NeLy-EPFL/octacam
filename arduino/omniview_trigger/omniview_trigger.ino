#include <Arduino.h>

// =============================================================================
//  omniview — camera trigger + synchronized LED strobe generator
//
//  Target board: Arduino Nano ESP32 (ESP32-S3), mounted on the EPFL
//  "common CCS light and camera trigger circuit" (common-trigger-circuit).
//
//  The host (octacam's `omniview` plugin, or the bundled omniview_send.py) sends
//  an arm packet over USB serial. The Arduino then free-runs a frame clock: on
//  every frame it emits one rising edge on the shared camera-trigger line (D13,
//  wired to all 4 Basler + 2 FLIR trigger inputs) and, phase-locked to that same
//  edge, strobes the two CCS light channels (D5 = ch1, D6 = ch2) ON for a slice
//  of the frame period set by the strobe duty cycle. It runs for the requested
//  duration (or until cancelled) and then returns to idle.
//
//  Because every edge in a frame is derived from a single per-frame t0, the
//  camera trigger and both light channels are inherently phase-locked — the
//  lights are guaranteed ON while the sensors integrate, with no cross-channel
//  skew. Timing is done in software (micros()); on the 240 MHz ESP32-S3 the
//  per-edge jitter is well under 1 µs — far tighter than USB software triggering.
//
//  Light polarity: each CCS channel is driven through an NPN sink transistor, so
//  an Arduino pin driven HIGH sinks the channel to GND → light ON. This assumes
//  the CCS PD3-3024-3-EI is in an external-trigger mode that follows the input
//  level (light on while the sink line is active). If the controller is set to a
//  one-shot / overdrive strobe mode instead, the on-time is set on the unit and
//  the duty here just needs to be non-zero to deliver a clean rising edge.
//
//  ---- Host -> Arduino wire protocol ----------------------------------------
//    Arm     (11 bytes): [0xA5]
//                        [fps           : uint16 LE]  frames per second (1..5000)
//                        [duration_ms   : uint32 LE]  run length (0 = until cancel)
//                        [duty_permille : uint16 LE]  LED on-time, permille of the
//                                                     frame period (0..1000)
//                        [cam_pulse_us  : uint16 LE]  camera pulse width in µs
//                                                     (0 = firmware default)
//    Cancel  (1 byte):   [0xCA]
//    Identify(1 byte):   [0x3F] '?'   -> replies "OMNIVIEW <version>\n"
//
//  ---- Arduino -> Host (newline-terminated ASCII tokens) ---------------------
//    "R\n"  running   — arm accepted, frame clock started
//    "D\n"  done      — requested duration elapsed, back to idle
//    "C\n"  cancelled — cancel received (or re-arm superseded a run), back to idle
//    "OMNIVIEW <version>\n"  reply to an identify request
//
//  ---- State machine ---------------------------------------------------------
//    IDLE    --(arm)---------------> RUNNING           (emits "R")
//    RUNNING --(duration elapsed)--> IDLE              (emits "D")
//    RUNNING --(new arm)-----------> RUNNING restarted (emits "C" then "R")
//    any     --(cancel)-----------> IDLE              (emits "C")
// =============================================================================

// ---- Pin assignments (match the common-trigger-circuit schematic) ----------
constexpr uint8_t kCameraPin = D13;  // shared trigger to all 6 cameras
constexpr uint8_t kCh1Pin    = D5;   // CCS channel 1 sink (LED ring light 1)
constexpr uint8_t kCh2Pin    = D6;   // CCS channel 2 sink (LED ring light 2)
constexpr uint8_t kCh3Pin    = D7;   // CCS channel 3 sink (unused — held LOW)

// External status RGB LED on the trigger board (common cathode: HIGH = colour
// on). NOTE: this is the board's indicator LED on D2/D3/D4 — not the Nano's own
// on-board RGB (which is on separate pins).
constexpr uint8_t kLedRed   = D2;
constexpr uint8_t kLedGreen = D3;
constexpr uint8_t kLedBlue  = D4;

// ---- Serial protocol constants ---------------------------------------------
constexpr uint8_t  kArmMagic       = 0xA5;
constexpr uint8_t  kCancelMagic    = 0xCA;
constexpr uint8_t  kIdentifyMagic  = 0x3F;  // '?'
constexpr uint8_t  kArmPayloadSize = 10;    // bytes following the arm magic
constexpr uint32_t kArmStaleMs     = 250;   // discard a partial arm packet after this
constexpr long     kBaudRate       = 115200;
constexpr char     kVersion[]      = "OMNIVIEW 1";

constexpr uint16_t kMaxFps = 5000;

// Camera trigger pulse width used when the arm packet leaves cam_pulse_us = 0.
// Wide enough for any Basler / FLIR rising-edge trigger input to latch; always
// clamped below the frame period so the line returns LOW before the next edge.
constexpr uint32_t kDefaultCamPulseUs = 500;
constexpr uint32_t kMinCamPulseUs     = 5;

// Minimum LED on-time so a tiny-but-nonzero duty still emits a strobe pulse
// (duty*period/1000 can truncate to 0 µs at high fps).
constexpr uint32_t kMinLedOnUs        = 1;

// ---- State machine ---------------------------------------------------------
enum class State : uint8_t { IDLE, RUNNING };
static State g_state = State::IDLE;

// ---- Capture parameters (set by the arm packet) ----------------------------
static uint32_t g_period_us      = 0;      // frame period = round(1e6 / fps)
static uint32_t g_cam_pulse_us   = 0;      // camera HIGH time per frame
static uint32_t g_led_on_us      = 0;      // LED HIGH time per frame (duty*period)
static bool     g_led_continuous = false;  // duty == 100% -> lights never strobe off
static uint32_t g_duration_ms    = 0;      // 0 = run until an explicit cancel

// ---- RUNNING-state tracking ------------------------------------------------
static uint32_t g_run_start_ms = 0;
static uint32_t g_frame_t0_us  = 0;  // micros() at the current frame's rising edge
static bool     g_cam_high     = false;
static bool     g_led_high     = false;

// ---- Non-blocking arm-packet reassembly ------------------------------------
// loop() never spins on serial: an arm packet is collected one byte per
// iteration, so the frame clock keeps emitting edges while it (or a fragmented
// packet) arrives. A partial packet that goes stale is discarded.
static uint8_t  g_arm_buf[kArmPayloadSize];
static uint8_t  g_arm_len         = 0;
static bool     g_arm_collecting  = false;
static uint32_t g_arm_deadline_ms = 0;

// ---- Output helpers --------------------------------------------------------

static void set_status(bool r, bool g, bool b) {
  digitalWrite(kLedRed,   r ? HIGH : LOW);
  digitalWrite(kLedGreen, g ? HIGH : LOW);
  digitalWrite(kLedBlue,  b ? HIGH : LOW);
}

static void cameras_low() {
  digitalWrite(kCameraPin, LOW);
  g_cam_high = false;
}

static void lights_low() {
  digitalWrite(kCh1Pin, LOW);
  digitalWrite(kCh2Pin, LOW);
  g_led_high = false;
}

// Raise the camera trigger and (unless the duty is zero) both light channels at
// the same instant, so the strobe is phase-locked to the trigger edge.
static void frame_rising_edge() {
  digitalWrite(kCameraPin, HIGH);
  g_cam_high = true;
  if (g_led_on_us > 0) {
    digitalWrite(kCh1Pin, HIGH);
    digitalWrite(kCh2Pin, HIGH);
    g_led_high = true;
  }
}

// ---- Serial helpers --------------------------------------------------------

// Decodes a fully-received 10-byte arm payload. Returns false on out-of-range
// fps (no serial I/O — the bytes are already buffered in loop()).
static bool decode_arm(const uint8_t *b, uint16_t &fps, uint32_t &duration_ms,
                       uint16_t &duty_permille, uint16_t &cam_pulse_us) {
  fps         = static_cast<uint16_t>(b[0]) | (static_cast<uint16_t>(b[1]) << 8);
  duration_ms = static_cast<uint32_t>(b[2])
              | (static_cast<uint32_t>(b[3]) << 8)
              | (static_cast<uint32_t>(b[4]) << 16)
              | (static_cast<uint32_t>(b[5]) << 24);
  duty_permille = static_cast<uint16_t>(b[6]) | (static_cast<uint16_t>(b[7]) << 8);
  cam_pulse_us  = static_cast<uint16_t>(b[8]) | (static_cast<uint16_t>(b[9]) << 8);
  return fps >= 1 && fps <= kMaxFps;
}

// ---- State transitions -----------------------------------------------------

static void enter_idle(char reason) {
  cameras_low();
  lights_low();
  g_state = State::IDLE;
  set_status(false, true, false);  // green = idle / ready
  if (reason) {
    Serial.write(reason);
    Serial.write('\n');
  }
}

static void enter_running(uint16_t fps, uint32_t duration_ms,
                          uint16_t duty_permille, uint16_t cam_pulse_us) {
  // A new arm supersedes any run in progress; tell the host the old one ended.
  if (g_state == State::RUNNING) {
    cameras_low();
    lights_low();
    Serial.write('C');
    Serial.write('\n');
  }

  // Frame period (rounded), with a safety floor so the loop always progresses.
  g_period_us = (1000000UL + fps / 2) / fps;
  if (g_period_us < 2) g_period_us = 2;

  // LED strobe on-time from the duty cycle. 100% duty means the lights stay on
  // continuously (never strobe off); 0% keeps them dark.
  if (duty_permille > 1000) duty_permille = 1000;
  g_led_on_us = static_cast<uint32_t>(
      (static_cast<uint64_t>(g_period_us) * duty_permille) / 1000ULL);
  g_led_continuous = (duty_permille >= 1000);
  if (g_led_continuous) g_led_on_us = g_period_us;
  // A tiny-but-nonzero duty can truncate to 0 µs at high fps; floor it so the
  // lights still emit a real strobe pulse (kept strictly below the period).
  if (duty_permille > 0 && g_led_on_us < kMinLedOnUs) g_led_on_us = kMinLedOnUs;

  // Camera pulse: substitute the default when 0, then clamp to a detectable
  // minimum and to strictly below the frame period (so the line always returns
  // LOW between frames — required for rising-edge-triggered cameras).
  uint32_t cam = cam_pulse_us ? cam_pulse_us : kDefaultCamPulseUs;
  if (cam < kMinCamPulseUs) cam = kMinCamPulseUs;
  const uint32_t cam_max = (g_period_us > 1) ? (g_period_us - 1) : 1;
  if (cam > cam_max) cam = cam_max;
  g_cam_pulse_us = cam;

  g_duration_ms  = duration_ms;
  g_run_start_ms = millis();
  g_frame_t0_us  = micros();
  frame_rising_edge();  // emit the first frame's edge immediately

  g_state = State::RUNNING;
  set_status(true, false, false);  // red = recording
  Serial.write('R');
  Serial.write('\n');
}

// ---- setup / loop ----------------------------------------------------------

void setup() {
  pinMode(kCameraPin, OUTPUT);
  pinMode(kCh1Pin,    OUTPUT);
  pinMode(kCh2Pin,    OUTPUT);
  pinMode(kCh3Pin,    OUTPUT);
  pinMode(kLedRed,    OUTPUT);
  pinMode(kLedGreen,  OUTPUT);
  pinMode(kLedBlue,   OUTPUT);

  cameras_low();
  lights_low();
  digitalWrite(kCh3Pin, LOW);  // CCS ch3 reserved, held off

  // No blocking `while (!Serial)` — the trigger generator must come up even when
  // the board is powered without a host attached (USB CDC connects later).
  Serial.begin(kBaudRate);
  enter_idle(0);  // green, no status token
}

void loop() {
  // ---- Incoming serial: one byte per iteration so the frame clock is never
  // stalled waiting on the host. An arm packet is reassembled across iterations;
  // while collecting, every byte is payload (so an embedded 0xCA/0xA5 can no
  // longer be misread as a command). Cancel/identify act only between packets.
  if (Serial.available() > 0) {
    const uint8_t b = static_cast<uint8_t>(Serial.read());
    if (g_arm_collecting) {
      g_arm_buf[g_arm_len++] = b;
      if (g_arm_len >= kArmPayloadSize) {
        g_arm_collecting = false;
        uint16_t fps, duty, cam_pulse;
        uint32_t duration_ms;
        if (decode_arm(g_arm_buf, fps, duration_ms, duty, cam_pulse)) {
          enter_running(fps, duration_ms, duty, cam_pulse);
        }
      }
    } else if (b == kCancelMagic) {
      enter_idle('C');
    } else if (b == kIdentifyMagic) {
      Serial.print(kVersion);
      Serial.write('\n');
    } else if (b == kArmMagic) {
      g_arm_collecting  = true;
      g_arm_len         = 0;
      g_arm_deadline_ms = millis() + kArmStaleMs;
    }
    // else: unknown byte discarded to stay byte-aligned.
  }

  // Discard a partial arm packet that never completed, so a stray 0xA5 cannot
  // wedge the command stream behind an arm that will never finish.
  if (g_arm_collecting &&
      static_cast<int32_t>(millis() - g_arm_deadline_ms) >= 0) {
    g_arm_collecting = false;
  }

  // ---- Frame clock (RUNNING only) ------------------------------------------
  if (g_state == State::RUNNING) {
    const uint32_t elapsed = micros() - g_frame_t0_us;  // wraparound-safe

    if (g_cam_high && elapsed >= g_cam_pulse_us) {
      cameras_low();
    }
    if (g_led_high && !g_led_continuous && elapsed >= g_led_on_us) {
      lights_low();
    }
    if (elapsed >= g_period_us) {
      // Normally advance by exactly one period (drift-free). If some hiccup
      // slipped us past a whole extra frame, resync to now and emit a single
      // clean edge rather than a burst of back-to-back catch-up edges.
      if (elapsed >= 2UL * g_period_us) {
        g_frame_t0_us = micros();
      } else {
        g_frame_t0_us += g_period_us;
      }
      frame_rising_edge();
    }

    // Duration check (0 = run until an explicit cancel).
    if (g_duration_ms != 0 && (millis() - g_run_start_ms) >= g_duration_ms) {
      enter_idle('D');
    }
  }
}
