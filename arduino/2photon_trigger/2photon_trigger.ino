#include <Arduino.h>

// =============================================================================
//  Camera trigger generator — 2-photon rig variant
//
//  The host (octacam) sends an arm packet over USB serial; the Arduino waits
//  for a rising edge on kTriggerInPin (ThorSync), then emits a precise
//  camera-frame square wave for the configured duration.
//
//  Host → Arduino wire protocol (7 bytes, little-endian):
//    [0xA5][fps:uint16_t LE][duration_ms:uint32_t LE]
//
//  Abort/cancel (1 byte):
//    [0xCA]  — returns Arduino to IDLE from any state
//
//  Arduino → Host wire protocol (single-byte status):
//    'A' (0x41) — armed, waiting for ThorSync rising edge
//    'T' (0x54) — triggered, capture running
//    'D' (0x44) — done, capture complete, back to IDLE
//
//  State machine:
//    IDLE  ---(arm packet)---> ARMED
//    ARMED ---(ThorSync  )---> RUNNING
//    RUNNING ---(timeout )---> IDLE  (sends 'D')
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
constexpr uint8_t  kArmPayloadSize = 6;   // uint16 fps + uint32 duration_ms
constexpr uint32_t kPayloadWaitMs  = 10;  // max wait for payload bytes after magic

// ---- State machine ---------------------------------------------------------
enum class State : uint8_t { IDLE, ARMED, RUNNING };
static State g_state = State::IDLE;

// ---- Capture parameters set by the arm packet ------------------------------
static uint16_t g_fps         = 0;
static uint32_t g_duration_ms = 0;

// ---- RUNNING-state tracking ------------------------------------------------
static uint32_t g_run_start_ms      = 0;
// ARMED-state: edge detection (non-blocking poll)
static bool     g_last_trigger_high = false;

// ---- Timer1 helpers (CTC mode, /8 prescaler) -------------------------------

ISR(TIMER1_COMPA_vect) {
  static bool level = false;
  level = !level;
  digitalWrite(kCameraPin, level);
  digitalWrite(kSyncPin,   level);
}

static void timer1_start(const uint16_t fps) {
  // ISR toggles the pin → one full output cycle = 2 interrupts → fire at 2×fps.
  const uint32_t ocr = (F_CPU / (8UL * 2UL * static_cast<uint32_t>(fps))) - 1UL;
  cli();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1  = 0;
  OCR1A  = static_cast<uint16_t>(constrain(ocr, 1UL, 65535UL));
  TCCR1B = _BV(WGM12) | _BV(CS11);  // CTC + prescaler /8
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

// Reads the 6-byte arm payload that follows the magic byte.
// Blocks for up to kPayloadWaitMs (a few milliseconds); returns false on
// timeout or if the payload values are out of range.
static bool parse_arm_payload(uint16_t &fps, uint32_t &duration_ms) {
  const uint32_t deadline = millis() + kPayloadWaitMs;
  while (Serial.available() < static_cast<int>(kArmPayloadSize)) {
    if (millis() > deadline) return false;
  }
  uint8_t buf[kArmPayloadSize];
  Serial.readBytes(reinterpret_cast<char *>(buf), kArmPayloadSize);
  fps = static_cast<uint16_t>(buf[0]) | (static_cast<uint16_t>(buf[1]) << 8);
  duration_ms = static_cast<uint32_t>(buf[2])
              | (static_cast<uint32_t>(buf[3]) << 8)
              | (static_cast<uint32_t>(buf[4]) << 16)
              | (static_cast<uint32_t>(buf[5]) << 24);
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

static void enter_armed(const uint16_t fps, const uint32_t duration_ms) {
  g_fps         = fps;
  g_duration_ms = duration_ms;
  // Initialise the edge detector from the current pin level so a signal that
  // is already HIGH at arm time does not immediately fire a false trigger.
  g_last_trigger_high = (digitalRead(kTriggerInPin) == HIGH);
  digitalWrite(kGatePin,   HIGH);
  digitalWrite(kStatusLed, HIGH);
  g_state = State::ARMED;
  Serial.write('A');
}

static void enter_running() {
  g_run_start_ms = millis();
  timer1_start(g_fps);
  g_state = State::RUNNING;
  Serial.write('T');
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
    } else if (b == kArmMagic) {
      Serial.read();  // consume magic
      uint16_t fps;
      uint32_t duration_ms;
      if (parse_arm_payload(fps, duration_ms)) {
        // A new arm packet re-arms from any state (stops the timer if running).
        if (g_state == State::RUNNING) timer1_stop();
        enter_armed(fps, duration_ms);
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

    case State::RUNNING:
      if (millis() - g_run_start_ms >= g_duration_ms) {
        enter_idle();
        Serial.write('D');
      }
      break;
  }
}
