"""Shared, explicit byte budgets. Never compact or truncate review evidence."""

import json

from common import EvalError, json_bytes

DEFAULT_PACKET_BYTES = 512 * 1024
MAX_PACKET_BYTES = 1024 * 1024
LEGACY_PACKET_BYTES = 256 * 1024
ENVELOPE_HEADROOM = 64 * 1024


def input_limits(max_packet_bytes=DEFAULT_PACKET_BYTES):
    if type(max_packet_bytes) is not int or not 1 <= max_packet_bytes <= MAX_PACKET_BYTES:
        raise EvalError("review packet budget must be between 1 and 1048576 bytes")
    # Reserve bounded space for rubric/schema and JSON string escaping on the wire.
    # Every layer is measured independently; these bounds are not token estimates.
    return {"packet_bytes": max_packet_bytes,
            "envelope_bytes": max_packet_bytes + ENVELOPE_HEADROOM,
            "request_bytes": 2 * (max_packet_bytes + ENVELOPE_HEADROOM),
            "truncation": "disabled"}


def check_size(data, limit, layer):
    if len(data) > limit:
        raise EvalError(f"review {layer} too large: {len(data)} > {limit} bytes; evidence must not be truncated")
    return data


def check_packet(packet, max_packet_bytes=DEFAULT_PACKET_BYTES):
    return check_size(json_bytes(packet), input_limits(max_packet_bytes)["packet_bytes"], "packet")


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def encode_envelope(payload, max_packet_bytes=DEFAULT_PACKET_BYTES):
    check_packet(payload["packet"], max_packet_bytes)
    return check_size(encode_json(payload), input_limits(max_packet_bytes)["envelope_bytes"], "envelope")


def read_envelope(stream, max_packet_bytes=DEFAULT_PACKET_BYTES):
    limit = input_limits(max_packet_bytes)["envelope_bytes"]
    return check_size(stream.read(limit + 1), limit, "envelope")


def encode_request(body, max_packet_bytes=DEFAULT_PACKET_BYTES):
    return check_size(encode_json(body), input_limits(max_packet_bytes)["request_bytes"], "HTTP request")
