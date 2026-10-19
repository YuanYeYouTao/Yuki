"""Actual wire bytes and fragmented streams, independent of WebSocket frames."""

import struct

import pytest

from yuki.client_api.rfb import (
    CAPS,
    NOTIFY,
    PEEK,
    PROVIDE,
    REQUEST,
    RfbInputFilter,
    RfbProtocolError,
)

HANDSHAKE = b"RFB 003.008\n\x01\x01"
UPDATE = struct.pack(">BBHHHH", 3, 0, 0, 0, 1440, 900)
KEY_DOWN = struct.pack(">BBHI", 4, 1, 0, 0xFFE1)
KEY_UP = struct.pack(">BBHI", 4, 0, 0, 0xFFE1)
EXT_DOWN = struct.pack(">BBHII", 255, 0, 1, 0x61, 30)
EXT_UP = struct.pack(">BBHII", 255, 0, 0, 0x61, 30)
POINTER = struct.pack(">BBHH", 5, 0x81, 321, 456)
POINTER_UP = struct.pack(">BBHH", 5, 0, 321, 456)


def ready(holder=False):
    parser = RfbInputFilter()
    assert parser.feed(HANDSHAKE) == HANDSHAKE
    assert parser.set_holder(holder) == b""
    return parser


def clipboard(data, *, extended=False):
    return b"\x06\0\0\0" + struct.pack(">i", -len(data) if extended else len(data)) + data


@pytest.mark.parametrize("cut", range(1, 14))
def test_handshake_fragments_and_coalesced_update(cut):
    parser = RfbInputFilter()
    wire = HANDSHAKE + UPDATE
    assert parser.feed(wire[:cut]) + parser.feed(wire[cut:]) == wire


@pytest.mark.parametrize("wire", [b"RFB 003.007\n", b"RFB 003.008\n\x02", HANDSHAKE[:-1] + b"\x02"])
def test_unsupported_handshake_closes_before_input_is_accepted(wire):
    with pytest.raises(RfbProtocolError):
        RfbInputFilter().feed(wire)


@pytest.mark.parametrize("kind", [1, 7, 149, 250, 254])
def test_unknown_message_closes_instead_of_guessing_length(kind):
    with pytest.raises(RfbProtocolError):
        ready().feed(bytes([kind]))


def test_unknown_qemu_subtype_closes_even_when_its_header_is_split():
    parser = ready(True)
    assert parser.feed(b"\xff") == b""
    with pytest.raises(RfbProtocolError):
        parser.feed(b"\x01")


@pytest.mark.parametrize("holder", [False, True])
def test_set_encodings_removes_only_unsupported_client_format_changes(holder):
    encodings = [0, 7, -316, -309, -258, -308, -312, -313, -1063131698]
    wire = struct.pack(">BBH", 2, 0, len(encodings)) + struct.pack(">9i", *encodings)
    keep = [value for value in encodings if value not in {-316, -309}]
    expected = struct.pack(">BBH", 2, 0, len(keep)) + struct.pack(">7i", *keep)
    for cut in range(1, len(wire)):
        parser = ready(holder)
        assert parser.feed(wire[:cut]) + parser.feed(wire[cut:] + UPDATE) == expected + UPDATE


@pytest.mark.parametrize(
    "flags,forward",
    [
        (0x1F000001, True),  # The real noVNC Caps plus all supported action bits.
        (CAPS | NOTIFY | PROVIDE | 1, True),
        (REQUEST | 1, True),
        (PEEK | 1, True),
        (REQUEST | PEEK | 1, True),
        (NOTIFY | 1, False),
        (PROVIDE | 1, False),
        (REQUEST | NOTIFY | 1, False),
        (1, False),
    ],
)
def test_viewer_clipboard_caps_wins_over_content_bits_and_next_message_stays_aligned(
    flags, forward
):
    wire = clipboard(struct.pack(">I", flags) + b"clipboard-bytes!", extended=True)
    assert len(wire) == 28  # signed length -20: four flags and sixteen body bytes.
    for cut in range(1, len(wire)):
        parser = ready()
        output = parser.feed(wire[:cut]) + parser.feed(wire[cut:] + UPDATE)
        assert output == (wire if forward else b"") + UPDATE


@pytest.mark.parametrize("data", [b"", b"hello", b"x" * 65536])
def test_traditional_clipboard_requires_holder_and_streams_without_reframing(data):
    wire = clipboard(data)
    viewer, holder = ready(), ready(True)
    pieces = [wire[:7], wire[7:14], wire[14:] + UPDATE]
    assert b"".join(viewer.feed(piece) for piece in pieces) == UPDATE
    assert b"".join(holder.feed(piece) for piece in pieces) == wire + UPDATE


def test_negative_one_byte_body_is_not_mistaken_for_four_flag_bytes():
    wire = b"\x06\0\0\0" + struct.pack(">i", -1) + b"x"
    assert ready().feed(wire + UPDATE) == UPDATE
    assert ready(True).feed(wire + UPDATE) == wire + UPDATE


def test_negative_signed_int_min_is_streamed_to_its_real_next_message_boundary():
    parser = ready()
    header = b"\x06\0\0\0" + struct.pack(">i", -(2**31))
    assert parser.feed(header + struct.pack(">I", PROVIDE | 1)) == b""
    remaining = 2**31 - 4
    # Reuse one real input chunk. Never allocate an entire two-GiB clipboard.
    chunk = b"x" * (1024 * 1024)
    while remaining >= len(chunk):
        assert parser.feed(chunk) == b""
        remaining -= len(chunk)
    assert parser.feed(chunk[:remaining] + UPDATE) == UPDATE


def test_pointer_high_bit_is_still_six_bytes_and_three_messages_share_one_frame():
    assert ready().feed(KEY_DOWN + POINTER + UPDATE) == UPDATE
    assert ready(True).feed(KEY_DOWN + POINTER + UPDATE) == KEY_DOWN + POINTER + UPDATE


def test_loss_releases_exact_normal_extended_and_pointer_keys_and_never_repeats():
    parser = ready(True)
    assert parser.feed(KEY_DOWN + EXT_DOWN + POINTER) == KEY_DOWN + EXT_DOWN + POINTER
    assert parser.set_holder(False) == KEY_UP + EXT_UP + POINTER_UP
    assert parser.set_holder(False) == b""
    assert parser.feed(KEY_DOWN + EXT_DOWN + POINTER + UPDATE) == UPDATE
    assert parser.close() == b""


def test_keys_released_by_client_are_not_released_a_second_time():
    parser = ready(True)
    assert (
        parser.feed(KEY_DOWN + EXT_DOWN + KEY_UP + EXT_UP) == KEY_DOWN + EXT_DOWN + KEY_UP + EXT_UP
    )
    assert parser.close() == b""


def test_long_forwarded_clipboard_finishes_before_loss_release_and_next_viewer_message():
    parser = ready(True)
    content = struct.pack(">I", PROVIDE | 1) + b"real clipboard body"
    wire = clipboard(content, extended=True)
    prefix = wire[:17]
    assert (
        parser.feed(KEY_DOWN + EXT_DOWN + POINTER + prefix)
        == KEY_DOWN + EXT_DOWN + POINTER + prefix
    )
    assert parser.set_holder(False) == b""
    assert (
        parser.feed(wire[17:] + KEY_DOWN + UPDATE)
        == wire[17:] + KEY_UP + EXT_UP + POINTER_UP + UPDATE
    )


def test_viewer_body_keeps_being_skipped_after_take_then_following_input_is_allowed():
    parser = ready()
    wire = clipboard(b"body continues after take")
    assert parser.feed(wire[:12]) == b""
    parser.set_holder(True)
    assert parser.feed(wire[12:] + KEY_DOWN) == KEY_DOWN
    assert parser.close() == KEY_UP


def test_truncated_forwarded_body_closes_without_injecting_keys_inside_clipboard():
    parser = ready(True)
    assert parser.feed(KEY_DOWN) == KEY_DOWN
    assert parser.feed(clipboard(b"unfinished")[:10]) == clipboard(b"unfinished")[:10]
    assert parser.close() == b""


def test_screen_resize_is_input_but_fence_pixel_format_and_updates_are_readable():
    resize = struct.pack(">BBHHBB", 251, 0, 1440, 900, 1, 0) + b"\0" * 16
    fence = b"\xf8" + b"\0" * 7 + b"\x03abc"
    pixel = b"\x00" + b"\0" * 19
    continuous = b"\x96" + b"\0" * 9
    wire = resize + fence + pixel + continuous + UPDATE
    assert ready().feed(wire) == fence + pixel + continuous + UPDATE
    assert ready(True).feed(wire) == wire
