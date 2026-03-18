#include <math.h>
#include <stdint.h>
#include <stdlib.h>

#pragma pack(push, 1)
struct Command {
  int16_t n_steps;
  uint16_t step_interval_us;
  uint16_t rest_duration_ms;
  uint8_t n_repeats;
  uint8_t init_wait_duration_s;
};
#pragma pack(pop)

const int IN1 = 8;
const int IN2 = 9;
const int IN3 = 10;
const int IN4 = 11;

// More accurate practical value for 28BYJ-48 half-step output revolution
const float HALF_STEPS_PER_REV = 4096.0f;

// Half-step sequence (IN1, IN2, IN3, IN4)
const uint8_t halfStepSeq[8][4] = {{1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0},
                                   {0, 1, 1, 0}, {0, 0, 1, 0}, {0, 0, 1, 1},
                                   {0, 0, 0, 1}, {1, 0, 0, 1}};

int stepIndex = 0;

void setCoils(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
  digitalWrite(IN1, a);
  digitalWrite(IN2, b);
  digitalWrite(IN3, c);
  digitalWrite(IN4, d);
}

void releaseMotor() { setCoils(0, 0, 0, 0); }

void oneHalfStep(int dir) {
  stepIndex += dir;
  if (stepIndex > 7)
    stepIndex = 0;
  if (stepIndex < 0)
    stepIndex = 7;

  setCoils(halfStepSeq[stepIndex][0], halfStepSeq[stepIndex][1],
           halfStepSeq[stepIndex][2], halfStepSeq[stepIndex][3]);
}

void move_n_steps(int16_t n_steps, uint16_t step_interval_us) {
  int dir = (n_steps > 0) ? +1 : -1;
  int steps = abs(n_steps);

  for (int i = 0; i < steps; i++) {
    oneHalfStep(dir);
    delayMicroseconds(step_interval_us);
  }
  releaseMotor();
}

void restForDuration(uint16_t rest_duration_ms) {
  if (rest_duration_ms <= 0)
    return;
  delay(rest_duration_ms);
}

void setup() {
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);

  releaseMotor();

  Serial.begin(115200);
  while (!Serial) {
    ;
  }
}

Command data;

void loop() {
  if (Serial.available() >= sizeof(Command)) {
    Serial.readBytes((char *)&data, sizeof(Command));

    if (data.n_steps == 1 || data.n_steps == -1) {
      oneHalfStep(data.n_steps);
    } else {
      if (data.init_wait_duration_s > 0) {
        delay(data.init_wait_duration_s * 1000);
      }
      for (uint8_t i = 0; i < data.n_repeats - 1; ++i) {
        move_n_steps(i % 2 == 0 ? data.n_steps : -data.n_steps,
                     data.step_interval_us);
        restForDuration(data.rest_duration_ms);
      }
      move_n_steps(data.n_steps, data.step_interval_us);
    }
  }
}
