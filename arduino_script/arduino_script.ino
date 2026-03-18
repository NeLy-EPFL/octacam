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

inline void move_n_steps(int16_t n_steps, const uint16_t step_interval_us) {
  if (n_steps > 0) {
    for (int i = 0; i < n_steps; ++i) {
      increment_half_step();
      delayMicroseconds(step_interval_us);
    }
  } else {
    n_steps = -n_steps;
    for (int i = 0; i < n_steps; ++i) {
      decrement_half_step();
      delayMicroseconds(step_interval_us);
    }
  }
  release_motor();
}

inline void execute_command(const Command &command) {
  if (command.n_steps == 0) {
    release_motor();
    return;
  }

  if (command.n_steps == 1) {
    increment_half_step();
    return;
  }

  if (command.n_steps == -1) {
    decrement_half_step();
    return;
  }

  if (command.init_wait_duration_s > 0) {
    delay(static_cast<unsigned long>(command.init_wait_duration_s) * 1000UL);
  }

  int16_t current_steps = command.n_steps;
  for (uint8_t i = 0; i + 1 < command.n_repeats; ++i) {
    move_n_steps(current_steps, command.step_interval_us);
    if (command.rest_duration_ms > 0) {
      delay(command.rest_duration_ms);
    }
    current_steps = -current_steps;
  }
  move_n_steps(current_steps, command.step_interval_us);
}

void setup() {
  pinMode(in1_pin, OUTPUT);
  pinMode(in2_pin, OUTPUT);
  pinMode(in3_pin, OUTPUT);
  pinMode(in4_pin, OUTPUT);

  release_motor();

  Serial.begin(115200);
  while (!Serial) {
    ;
  }
}

Command command_data;
char *const command_data_ptr = reinterpret_cast<char *>(&command_data);
constexpr size_t command_size = sizeof(Command);

void loop() {
  if (Serial.available() >= command_size) {
    const size_t bytes_read = Serial.readBytes(command_data_ptr, command_size);
    if (bytes_read == command_size) {
      execute_command(command_data);
    }
  }
}
