from octacam.serial_link import Command


def test_command_wire_format_matches_cpp_packed_struct():
    # Hand-computed little-endian layout of the packed C++ struct:
    # int16 n_steps, uint16 step_interval_us, uint16 rest_duration_ms,
    # uint8 n_repeats, uint8 init_wait_duration_s -> 8 bytes total.
    command = Command(
        n_steps=-4096,  # 0xF000
        step_interval_us=1465,  # 0x05B9
        rest_duration_ms=1000,  # 0x03E8
        n_repeats=3,
        init_wait_duration_s=10,
    )
    assert command.to_bytes() == b"\x00\xf0\xb9\x05\xe8\x03\x03\x0a"
    assert len(Command().to_bytes()) == 8


def test_single_step_commands():
    assert Command(n_steps=1).to_bytes() == b"\x01\x00\x00\x00\x00\x00\x00\x00"
    assert Command(n_steps=-1).to_bytes() == b"\xff\xff\x00\x00\x00\x00\x00\x00"
    assert Command(n_steps=0).to_bytes() == b"\x00" * 8
