#include <stddef.h>
#include <stdint.h>

#pragma pack(push, 1)
struct Command {
  int16_t n_steps;
  uint16_t step_interval_us;
  uint16_t rest_duration_ms;
  uint8_t n_repeats;
  uint8_t init_wait_duration_s;
};
#pragma pack(pop)

static_assert(sizeof(Command) == 8, "Command must stay packed to 8 bytes");

constexpr uint8_t in1_pin = 8;
constexpr uint8_t in2_pin = 9;
constexpr uint8_t in3_pin = 10;
constexpr uint8_t in4_pin = 11;

// Half-step sequence encoded as IN4..IN1 bit mask.
constexpr uint8_t half_step_seq[8] = {0b0001, 0b0011, 0b0010, 0b0110,
                                      0b0100, 0b1100, 0b1000, 0b1001};

uint8_t step_index = 0;
uint8_t coil_mask = 0;
Command command_data;
char *const command_data_ptr = reinterpret_cast<char *>(&command_data);
constexpr size_t command_size = sizeof(Command);

inline void set_coils(const uint8_t next_mask) {
  const uint8_t changed_mask = static_cast<uint8_t>(next_mask ^ coil_mask);

  if (changed_mask & 0b0001) {
    digitalWrite(in1_pin, (next_mask & 0b0001) != 0);
  }
  if (changed_mask & 0b0010) {
    digitalWrite(in2_pin, (next_mask & 0b0010) != 0);
  }
  if (changed_mask & 0b0100) {
    digitalWrite(in3_pin, (next_mask & 0b0100) != 0);
  }
  if (changed_mask & 0b1000) {
    digitalWrite(in4_pin, (next_mask & 0b1000) != 0);
  }

  coil_mask = next_mask;
}

inline void wait_until(const unsigned long deadline_us) {
  while (static_cast<long>(micros() - deadline_us) < 0) {
    // wait
  }
}

inline void release_motor() { set_coils(0); }

inline void increment_half_step() {
  step_index = static_cast<uint8_t>((step_index + 1) & 0x07);
  set_coils(half_step_seq[step_index]);
}

inline void decrement_half_step() {
  step_index = static_cast<uint8_t>((step_index - 1) & 0x07);
  set_coils(half_step_seq[step_index]);
}

inline void inc_n_half_steps(const uint16_t n_half_steps,
                             const unsigned long step_interval_us,
                             unsigned long &next_deadline_us) {
  for (uint16_t i_step = 0; i_step < n_half_steps; ++i_step) {
    wait_until(next_deadline_us);
    increment_half_step();
    next_deadline_us += step_interval_us;
  }
}

inline void dec_n_half_steps(const uint16_t n_half_steps,
                             const unsigned long step_interval_us,
                             unsigned long &next_deadline_us) {
  for (uint16_t i_step = 0; i_step < n_half_steps; ++i_step) {
    wait_until(next_deadline_us);
    decrement_half_step();
    next_deadline_us += step_interval_us;
  }
}

inline void rest(const unsigned long rest_duration_us,
                 unsigned long &next_deadline_us) {
  next_deadline_us += rest_duration_us;
  wait_until(next_deadline_us);
}

inline void execute_command(const Command &command) {
  const int16_t n_steps = command.n_steps;

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

  const uint16_t n_half_steps =
      (n_steps > 0) ? static_cast<uint16_t>(n_steps)
                    : static_cast<uint16_t>(-static_cast<int32_t>(n_steps));
  const unsigned long step_interval_us =
      static_cast<unsigned long>(command.step_interval_us);
  const unsigned long rest_duration_us =
      static_cast<unsigned long>(command.rest_duration_ms) * 1000UL;
  const uint8_t n_repeats = command.n_repeats;

  if (n_repeats == 0) {
    release_motor();
    return;
  }

  unsigned long next_deadline_us =
      micros() +
      static_cast<unsigned long>(command.init_wait_duration_s) * 1000000UL;

  if (n_steps > 0) {
    for (uint8_t i_repeat = 0; i_repeat < n_repeats; ++i_repeat) {
      inc_n_half_steps(n_half_steps, step_interval_us, next_deadline_us);
      release_motor();
      rest(rest_duration_us, next_deadline_us);
      dec_n_half_steps(n_half_steps, step_interval_us, next_deadline_us);
      release_motor();
      if (i_repeat < n_repeats - 1) {
        rest(rest_duration_us, next_deadline_us);
      }
    }
  } else {
    for (uint8_t i_repeat = 0; i_repeat < n_repeats; ++i_repeat) {
      dec_n_half_steps(n_half_steps, step_interval_us, next_deadline_us);
      release_motor();
      rest(rest_duration_us, next_deadline_us);
      inc_n_half_steps(n_half_steps, step_interval_us, next_deadline_us);
      release_motor();
      if (i_repeat < n_repeats - 1) {
        rest(rest_duration_us, next_deadline_us);
      }
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
    const size_t bytes_read = Serial.readBytes(command_data_ptr, command_size);
    if (bytes_read == command_size) {
      execute_command(command_data);
    }
  }
}
