#include <Arduino.h>
#include "fw_build_info.h"  // TWOPHOTON_FW_BUILD — source fingerprint (auto-generated)

// =============================================================================
//  Camera trigger generator — 2-photon rig variant
//
//  The host (octacam) sends an arm packet over USB serial; the Arduino waits
//  for a rising edge on kTriggerInPin (ThorSync), then emits a precise
//  camera-frame square wave until either the configured duration elapses or —
//  when idle_ms > 0 ("match ThorImage") — ThorImage stops: kTriggerInPin has
//  stayed LOW for idle_ms. Real ThorSync data shows that pin's source is busy for
//  the whole acquisition (CaptureOn is HIGH throughout; FrameOut is HIGH ~99% of
//  each frame period), so "LOW for idle_ms" means the acquisition is over.
//  duration_ms stays a hard ceiling either way: idle detection can only end a
//  run sooner, never later.
//
//  Host → Arduino wire protocol (9 bytes, little-endian):
//    [0xA5][fps:uint16_t LE][duration_ms:uint32_t LE][idle_ms:uint16_t LE]
//    idle_ms = 0 disables the ThorImage-stop detection (fixed duration).
//
//  Abort/cancel (1 byte):
//    [0xCA]  — returns Arduino to IDLE from any state
//
//  Arduino → Host wire protocol (single-byte status):
//    'A' (0x41) — armed, waiting for ThorSync rising edge
//    'T' (0x54) — triggered, capture running
//    'D' (0x44) — done, capture complete, back to IDLE
//
//  Right before 'D' the board reports why and how the run ended, as one
//  newline-terminated line (it starts with 'S', never a status byte):
//    "STOP <reason> <edges> <high_ms> <run_ms>\n"
//    reason  'I' = ThorImage stopped (input idle), 'C' = duration ceiling hit
//    edges   rising edges seen on kTriggerInPin while running (incl. the start)
//    high_ms total time kTriggerInPin was HIGH while running
//    run_ms  total run time
//  edges/high_ms tell what is wired to the input (a CaptureOn-style gate: one
//  edge, HIGH ≈ run; a FrameOut-style train: one edge per 2P frame).
//
//  Identify (1 byte): [0x3F] '?'  -> replies "2PHOTON 2 <build>\n"
//    The banner starts with '2' (never a status byte), so the host can tell it
//    apart from the bare 'A'/'T'/'D' status bytes. <build> is a short hash of the
//    sketch source (fw_build_info.h) so octacam knows whether this exact firmware
//    is running and can offer to (re)flash it.
//
//  State machine:
//    IDLE  ---(arm packet)---> ARMED
//    ARMED ---(ThorSync  )---> RUNNING
//    RUNNING ---(duration elapsed, or input idle for idle_ms)---> IDLE
//                                          (sends "STOP ..." then 'D')
//    Any state ---(0xCA  )---> IDLE
//    Any state ---(arm   )---> ARMED (re-arms mid-run if needed)
//
//  Target: Arduino Mega 2560 (16 MHz, 16-bit Timer1).
// =============================================================================

// ---- Pin assignments (match the rig wiring) --------------------------------
constexpr uint8_t kTriggerInPin = 4;   // ThorSync input, active HIGH → trigger
constexpr uint8_t kCameraPin    = A0;  // camera frame trigger (square-wave out)
constexpr uint8_t kSyncPin      = A1;  // sync copy of the trigger, for the DAQ
constexpr uint8_t kGatePin      = 38;  // HIGH for the whole capture window
constexpr uint8_t kStatusLed    = LED_BUILTIN;

// ---- Serial protocol constants ---------------------------------------------
constexpr uint8_t  kArmMagic       = 0xA5;
constexpr uint8_t  kCancelMagic    = 0xCA;
constexpr uint8_t  kIdentifyMagic  = 0x3F;  // '?'  -> identity banner
constexpr uint8_t  kArmPayloadSize = 8;   // uint16 fps + uint32 duration_ms + uint16 idle_ms
constexpr uint32_t kPayloadWaitMs  = 10;  // max wait for payload bytes after magic
constexpr char     kVersion[]      = "2PHOTON 2";

// ---- State machine ---------------------------------------------------------
enum class State : uint8_t { IDLE, ARMED, RUNNING };
static State g_state = State::IDLE;

// ---- Capture parameters set by the arm packet ------------------------------
static uint16_t g_fps         = 0;
static uint32_t g_duration_ms = 0;
static uint16_t g_idle_ms     = 0;  // 0 = fixed duration (no ThorImage-stop detection)

// ---- RUNNING-state tracking ------------------------------------------------
static uint32_t g_run_start_ms      = 0;
static uint32_t g_last_active_ms    = 0;  // last time kTriggerInPin read HIGH
static uint32_t g_high_ms           = 0;  // accumulated HIGH time (diagnostic)
static uint32_t g_high_since_ms     = 0;  // start of the current HIGH stretch
static uint32_t g_edges             = 0;  // rising edges while running (diagnostic)
// ARMED-state: edge detection (non-blocking poll)
static bool     g_last_trigger_high = false;

// ---- Timer1 helpers (CTC mode, /8 prescaler) -------------------------------

// The camera/sync pin level the ISR toggles. Reset by every timer1_start, so a
// run's first rising edge always comes one half period after its start edge: a
// level carried over from the previous run's parity made it a whole period
// half the time (octacam's ThorSync-free alignment relies on the phase).
static volatile bool g_cam_level = false;

ISR(TIMER1_COMPA_vect) {
  g_cam_level = !g_cam_level;
  digitalWrite(kCameraPin, g_cam_level);
  digitalWrite(kSyncPin,   g_cam_level);
}

// Timer1 prescaler options, finest first. A fixed /8 prescaler overflowed the
// 16-bit OCR1A below ~15.3 fps and silently ran at ~15.3 fps; selecting the
// smallest prescaler whose CTC TOP still fits in 16 bits lets the whole
// host-accepted 1..10000 fps range map to an accurate rate (and keeps the best
// resolution at high fps).
struct Timer1Prescaler { uint16_t divisor; uint8_t cs_bits; };
static const Timer1Prescaler kTimer1Prescalers[] = {
  {1,    _BV(CS10)},               // /1
  {8,    _BV(CS11)},               // /8
  {64,   _BV(CS11) | _BV(CS10)},   // /64
  {256,  _BV(CS12)},               // /256
  {1024, _BV(CS12) | _BV(CS10)},   // /1024
};

static void timer1_start(const uint16_t fps) {
  // ISR toggles the pin → one full output cycle = 2 interrupts → fire at 2×fps.
  // ticks = timer counts per half-period; CTC TOP (OCR1A) = ticks - 1.
  uint32_t ocr     = 65535UL;                 // slowest fallback (never hit for
  uint8_t  cs_bits = _BV(CS12) | _BV(CS10);   // fps in 1..10000, see range above)
  for (uint8_t i = 0;
       i < sizeof(kTimer1Prescalers) / sizeof(kTimer1Prescalers[0]); ++i) {
    const uint32_t ticks =
        F_CPU / (static_cast<uint32_t>(kTimer1Prescalers[i].divisor) * 2UL
                 * static_cast<uint32_t>(fps));
    if (ticks >= 1UL && ticks <= 65536UL) {
      ocr     = ticks - 1UL;
      cs_bits = kTimer1Prescalers[i].cs_bits;
      break;
    }
  }
  cli();
  g_cam_level = false;
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1  = 0;
  OCR1A  = static_cast<uint16_t>(ocr);
  TCCR1B = _BV(WGM12) | cs_bits;  // CTC mode + selected prescaler
  TIMSK1 = _BV(OCIE1A);
  sei();
}

static void timer1_stop() {
  cli();
  TIMSK1 = 0;
  TCCR1B = 0;
  TCNT1  = 0;
  sei();
  digitalWrite(kCameraPin, LOW);
  digitalWrite(kSyncPin,   LOW);
}

// ---- Serial helpers --------------------------------------------------------

// Reads the 8-byte arm payload that follows the magic byte.
// Blocks for up to kPayloadWaitMs (a few milliseconds); returns false on
// timeout or if the payload values are out of range.
static bool parse_arm_payload(uint16_t &fps, uint32_t &duration_ms,
                              uint16_t &idle_ms) {
  const uint32_t start = millis();
  while (Serial.available() < static_cast<int>(kArmPayloadSize)) {
    if (millis() - start >= kPayloadWaitMs) return false;
  }
  uint8_t buf[kArmPayloadSize];
  Serial.readBytes(reinterpret_cast<char *>(buf), kArmPayloadSize);
  fps = static_cast<uint16_t>(buf[0]) | (static_cast<uint16_t>(buf[1]) << 8);
  duration_ms = static_cast<uint32_t>(buf[2])
              | (static_cast<uint32_t>(buf[3]) << 8)
              | (static_cast<uint32_t>(buf[4]) << 16)
              | (static_cast<uint32_t>(buf[5]) << 24);
  idle_ms = static_cast<uint16_t>(buf[6]) | (static_cast<uint16_t>(buf[7]) << 8);
  return fps >= 1 && fps <= 10000 && duration_ms >= 1;
}

// ---- State transitions -----------------------------------------------------

static void enter_idle() {
  timer1_stop();
  digitalWrite(kGatePin,   LOW);
  digitalWrite(kStatusLed, LOW);
  g_last_trigger_high = false;
  g_state = State::IDLE;
}

static void enter_armed(const uint16_t fps, const uint32_t duration_ms,
                        const uint16_t idle_ms) {
  g_fps         = fps;
  g_duration_ms = duration_ms;
  g_idle_ms     = idle_ms;
  // Initialise the edge detector from the current pin level so a signal that
  // is already HIGH at arm time does not immediately fire a false trigger.
  g_last_trigger_high = (digitalRead(kTriggerInPin) == HIGH);
  digitalWrite(kGatePin,   HIGH);
  digitalWrite(kStatusLed, HIGH);
  g_state = State::ARMED;
  Serial.write('A');
}

static void enter_running() {
  g_run_start_ms   = millis();
  g_last_active_ms = g_run_start_ms;
  g_high_since_ms  = g_run_start_ms;
  g_high_ms        = 0;
  g_edges          = 1;  // the start edge itself
  g_last_trigger_high = true;
  timer1_start(g_fps);
  g_state = State::RUNNING;
  Serial.write('T');
}

// Ends a run: report why + what the input looked like, then 'D'.
static void finish_run(const char reason, const uint32_t now) {
  if (g_last_trigger_high) g_high_ms += now - g_high_since_ms;
  enter_idle();
  Serial.print("STOP ");
  Serial.write(reason);
  Serial.write(' ');
  Serial.print(g_edges);
  Serial.write(' ');
  Serial.print(g_high_ms);
  Serial.write(' ');
  Serial.print(now - g_run_start_ms);
  Serial.write('\n');
  Serial.write('D');
}

// ---- setup / loop ----------------------------------------------------------

void setup() {
  pinMode(kTriggerInPin, INPUT);
  pinMode(kCameraPin,    OUTPUT);
  pinMode(kSyncPin,      OUTPUT);
  pinMode(kGatePin,      OUTPUT);
  pinMode(kStatusLed,    OUTPUT);

  digitalWrite(kCameraPin, LOW);
  digitalWrite(kSyncPin,   LOW);
  digitalWrite(kGatePin,   LOW);
  digitalWrite(kStatusLed, LOW);

  Serial.begin(115200);
  while (!Serial) {}
}

void loop() {
  // ---- Incoming serial: handle in every state for responsive cancel/re-arm.
  if (Serial.available() > 0) {
    const uint8_t b = static_cast<uint8_t>(Serial.peek());

    if (b == kCancelMagic) {
      Serial.read();
      if (g_state != State::IDLE) {
        enter_idle();
      }
    } else if (b == kIdentifyMagic) {
      Serial.read();  // consume '?'
      Serial.print(kVersion);
      Serial.write(' ');
      Serial.print(TWOPHOTON_FW_BUILD);
      Serial.write('\n');
    } else if (b == kArmMagic) {
      Serial.read();  // consume magic
      uint16_t fps;
      uint32_t duration_ms;
      uint16_t idle_ms;
      if (parse_arm_payload(fps, duration_ms, idle_ms)) {
        // A new arm packet re-arms from any state (stops the timer if running).
        if (g_state == State::RUNNING) timer1_stop();
        enter_armed(fps, duration_ms, idle_ms);
      }
    } else {
      Serial.read();  // discard unknown byte
    }
  }

  // ---- State machine -------------------------------------------------------
  switch (g_state) {
    case State::IDLE:
      break;

    case State::ARMED: {
      // Non-blocking rising-edge detection.
      const bool high = (digitalRead(kTriggerInPin) == HIGH);
      if (high && !g_last_trigger_high) {
        enter_running();
      }
      g_last_trigger_high = high;
      break;
    }

    case State::RUNNING: {
      const uint32_t now  = millis();
      const bool     high = (digitalRead(kTriggerInPin) == HIGH);
      if (high) {
        g_last_active_ms = now;
        if (!g_last_trigger_high) {
          ++g_edges;
          g_high_since_ms = now;
        }
      } else if (g_last_trigger_high) {
        g_high_ms += now - g_high_since_ms;
      }
      g_last_trigger_high = high;
      if (now - g_run_start_ms >= g_duration_ms) {
        finish_run('C', now);
      } else if (g_idle_ms > 0 && now - g_last_active_ms >= g_idle_ms) {
        finish_run('I', now);
      }
      break;
    }
  }
}
