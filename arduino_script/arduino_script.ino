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

constexpr uint8_t in1_pin = 8;
constexpr uint8_t in2_pin = 9;
constexpr uint8_t in3_pin = 10;
constexpr uint8_t in4_pin = 11;

// Half-step sequence (IN1, IN2, IN3, IN4)
constexpr uint8_t half_step_seq[8][4] = {
    {1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0}, {0, 1, 1, 0},
    {0, 0, 1, 0}, {0, 0, 1, 1}, {0, 0, 0, 1}, {1, 0, 0, 1}};

int8_t step_index = 0;
Command command_data;
char *const command_data_ptr = reinterpret_cast<char *>(&command_data);
constexpr size_t command_size = sizeof(Command);
unsigned long start_time_us = 0;
unsigned long expected_elapsed_time_us = 0;
unsigned long step_interval_us = 0;
unsigned long rest_duration_us = 0;
uint16_t abs_n_steps = 0;

inline void set_coils(const uint8_t a, const uint8_t b, const uint8_t c,
                      const uint8_t d) {
  digitalWrite(in1_pin, a);
  digitalWrite(in2_pin, b);
  digitalWrite(in3_pin, c);
  digitalWrite(in4_pin, d);
}

inline void release_motor() { set_coils(0, 0, 0, 0); }

inline void increment_half_step() {
  if (step_index == 7) {
    step_index = 0;
  } else {
    ++step_index;
  }
  set_coils(half_step_seq[step_index][0], half_step_seq[step_index][1],
            half_step_seq[step_index][2], half_step_seq[step_index][3]);
}

inline void decrement_half_step() {
  if (step_index == 0) {
    step_index = 7;
  } else {
    --step_index;
  }
  set_coils(half_step_seq[step_index][0], half_step_seq[step_index][1],
            half_step_seq[step_index][2], half_step_seq[step_index][3]);
}

inline void increment_n_half_steps() {
  for (uint16_t i_step = 0; i_step < abs_n_steps; ++i_step) {
    while (micros() - start_time_us < expected_elapsed_time_us) {
      // wait
    }
    increment_half_step();
    expected_elapsed_time_us += step_interval_us;
  }
}

inline void decrement_n_half_steps() {
  for (uint16_t i_step = 0; i_step < abs_n_steps; ++i_step) {
    while (micros() - start_time_us < expected_elapsed_time_us) {
      // wait
    }
    decrement_half_step();
    expected_elapsed_time_us += step_interval_us;
  }
}

inline void rest() {
  expected_elapsed_time_us += rest_duration_us;
  while (micros() - start_time_us < expected_elapsed_time_us) {
    // wait
  }
}

inline void execute_command(const Command &command) {
  int16_t n_steps = command.n_steps;

  if (n_steps == 0) {
    release_motor();
    return;
  }

  if (n_steps == 1) {
    increment_half_step();
    return;
  }

  if (n_steps == -1) {
    decrement_half_step();
    return;
  }

  expected_elapsed_time_us =
      static_cast<unsigned long>(command.init_wait_duration_s) * 1000000UL;
  step_interval_us = static_cast<unsigned long>(command.step_interval_us);
  rest_duration_us =
      static_cast<unsigned long>(command.rest_duration_ms) * 1000UL;

  if (n_steps > 0) {
    abs_n_steps = static_cast<uint16_t>(n_steps);
    for (uint8_t i_repeat = 0; i_repeat < command.n_repeats; ++i_repeat) {
      increment_n_half_steps();
      release_motor();
      rest();
      decrement_n_half_steps();
      release_motor();
      rest();
    }
  } else {
    abs_n_steps = static_cast<uint16_t>(-n_steps);
    for (uint8_t i_repeat = 0; i_repeat < command.n_repeats; ++i_repeat) {
      decrement_n_half_steps();
      release_motor();
      rest();
      increment_n_half_steps();
      release_motor();
      rest();
    }
  }
}

void setup() {
  pinMode(in1_pin, OUTPUT);
  pinMode(in2_pin, OUTPUT);
  pinMode(in3_pin, OUTPUT);
  pinMode(in4_pin, OUTPUT);

  release_motor();

  Serial.begin(115200);
  while (!Serial) {
    // wait
  }
}

void loop() {
  if (Serial.available() >= command_size) {
    start_time_us = micros();
    Serial.readBytes(command_data_ptr, command_size);
    execute_command(command_data);
  }
}
