"""Safety checks for outgoing APRS messages."""

import re
from kamxl import KAMTimeoutError

class APRSTransmitError(ValueError):
    """APRS transmission cannot proceed safely."""

def validate_disconnected_status(response: str) -> None:
    """
    Require a recognized, disconnected KAM-XL STATUS response.

    Fail closed on missing, connected, or unrecognized stream states.
    Require explicit confirmation that VHF stream A/2 is disconnected.
    """
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid STATUS response")

    lines = [
        line.strip()
        for line in response.splitlines()
        if line.strip()
    ]

    stream_lines = [
        line for line in lines
        if re.search(r"\bstream\b", line, re.IGNORECASE)
    ]

    if not stream_lines:
        raise APRSTransmitError(
            "STATUS did not report any AX.25 streams"
        )

    # Every reported AX.25 stream must be disconnected.
    for line in stream_lines:
        if not re.fullmatch(
            r"[A-Z]/[12]\s+stream\s+-\s+IO DISCONNECTED",
            line,
            re.IGNORECASE,
        ):
            raise APRSTransmitError(
                f"Unsafe or unrecognized stream state: {line}"
            )

    # Port 2 is our APRS port. Require explicit confirmation
    # that its A/2 stream is disconnected.
    if not any(
        re.fullmatch(
            r"A/2\s+stream\s+-\s+IO DISCONNECTED",
            line,
            re.IGNORECASE,
        )
        for line in stream_lines
    ):
        raise APRSTransmitError(
            "STATUS does not confirm disconnected A/2 stream"
        )

    # Reject unexpected non-stream lines. The KAM-XL normally
    # includes a FREE BYTES line in its STATUS response.
    non_stream_lines = [
        line for line in lines
        if line not in stream_lines
    ]

    if len(non_stream_lines) != 1 or not re.fullmatch(
        r"FREE BYTES \d+",
        non_stream_lines[0],
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            "STATUS contains unexpected or missing information"
        )

def validate_aprs_port(response: str) -> None:
    """
    Require the KAM-XL to report Port 2 for APRS transmission.

    Do not automatically change ports.
    """
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid PORT response")

    if not re.fullmatch(r"\s*PORT\s+2\s*", response):
        raise APRSTransmitError(
            f"APRS requires KAM-XL Port 2; received: {response!r}"
        )

def validate_aprs_paclen(response: str) -> None:
    """Require Port 2 PACLEN of 128 bytes."""
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid PACLEN response")

    if not re.fullmatch(
        r"\s*PACLEN\s+\d+/128\s*",
        response,
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            f"APRS requires Port 2 PACLEN 128; received: {response!r}"
        )


def validate_aprs_cpactime(response: str) -> None:
    """Require character-packet timing to be disabled."""
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid CPACTIME response")

    if not re.fullmatch(
        r"\s*CPACTIME\s+OFF\s*",
        response,
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            f"APRS requires CPACTIME OFF; received: {response!r}"
        )


def validate_aprs_sendpac(response: str) -> None:
    """Require carriage return as the SENDPAC character."""
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid SENDPAC response")

    if not re.fullmatch(
        r"\s*SENDPAC\s+\$0D\s+\(CTRL-M\)\s*",
        response,
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            f"APRS requires SENDPAC $0D; received: {response!r}"
        )


def validate_aprs_command_character(response: str) -> None:
    """Require Ctrl-C as the command-mode escape character."""
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid COMMAND response")

    if not re.fullmatch(
        r"\s*COMMAND\s+\$03\s+\(CTRL-C\)\s*",
        response,
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            f"APRS requires COMMAND $03; received: {response!r}"
        )

def validate_unproto(response: str) -> str:
    """
    Validate an existing KAM-XL UNPROTO setting for restoration.

    Accept the value returned by kam.get("UNPROTO").
    """
    if not isinstance(response, str):
        raise APRSTransmitError("Invalid UNPROTO response")

    value = response.strip().upper()

    if not re.fullmatch(
        r"[A-Z0-9]{1,6}(?:-[0-9]{1,2})?"
        r"(?:/[A-Z0-9]{1,6}(?:-[0-9]{1,2})?)?"
        r"(?: VIA [A-Z0-9-]+(?:,[A-Z0-9-]+)*)?",
        value,
    ):
        raise APRSTransmitError(
            f"Unrecognized UNPROTO configuration: {response!r}"
        )

    return value

def validate_aprs_active_stream(response: str) -> None:
    """
    Require disconnected stream A on active radio Port 2.

    This checks the current I/O stream, not the PORT default.
    """
    validate_disconnected_status(response)

    lines = [
        line.strip()
        for line in response.splitlines()
        if line.strip()
    ]

    stream_lines = [
        line for line in lines
        if re.search(r"\bstream\b", line, re.IGNORECASE)
    ]

    if len(stream_lines) != 1 or not re.fullmatch(
        r"A/2\s+stream\s+-\s+IO DISCONNECTED",
        stream_lines[0],
        re.IGNORECASE,
    ):
        raise APRSTransmitError(
            "APRS transmission requires disconnected stream A/2"
        )

def validate_aprs_recovery_status(response: str) -> dict:
    """
    Validate STATUS after an explicit command-mode recovery.

    This does not perform recovery or authorize RF transmission.
    """
    validate_aprs_active_stream(response)

    return {
        "command_mode_response_valid": True,
        "active_stream": "A/2",
        "ax25_connected": False,
        "rf_enabled": False,
    }

def recover_aprs_command_mode(kam) -> dict:
    """
    Explicit KAM-XL command-mode recovery.

    Caller must hold exclusive serial access and must have
    authorized a Ctrl-C mode transition.

    Does not enable or submit RF transmission.
    """
    try:
        kam.enter_command_mode()
        status = kam.send_command("STATUS")
        result = validate_aprs_recovery_status(status)
    except Exception as exc:
        raise APRSTransmitError(
            "APRS command-mode recovery failed. "
            "TNC state remains uncertain; manual verification required."
        ) from exc

    return {
        **result,
        "recovered": True,
        "transmitted": False,
    }

def check_aprs_tx_preflight(kam) -> dict:
    """
    Read-only KAM-XL APRS transmit preflight.

    Caller must provide exclusive serial access and ensure the
    TNC is already in command mode. Passing these checks does
    not authorize RF transmission.
    """
    checks = (
        ("STATUS", validate_aprs_active_stream),
        ("PORT", validate_aprs_port),
        ("PACLEN", validate_aprs_paclen),
        ("CPACTIME", validate_aprs_cpactime),
        ("SENDPAC", validate_aprs_sendpac),
        ("COMMAND", validate_aprs_command_character),
    )

    for command, validator in checks:
        response = kam.send_command(command)
        validator(response)

    return {
        "passed": True,
        "rf_enabled": False,
        "transmitted": False,
        "checks": [command for command, _ in checks],
    }

def build_aprs_tx_plan(
    payload: str,
    original_unproto: str,
    path: str = "",
) -> tuple[str, ...]:
    """
    Build a KAM-XL APRS transmission plan without performing I/O.

    The caller must separately verify the active stream, operating
    mode, and any automatic transmissions before executing it.
    """
    original = validate_unproto(original_unproto)

    if not isinstance(payload, str):
        raise APRSTransmitError("Invalid APRS payload")

    if not re.fullmatch(r":[A-Z0-9 -]{9}:[\x20-\x7E]+", payload):
        raise APRSTransmitError("Invalid APRS message format")

    if "\r" in payload or "\n" in payload:
        raise APRSTransmitError("APRS payload contains line breaks")

    if len(payload.encode("ascii")) > 128:
        raise APRSTransmitError("APRS payload exceeds Port 2 PACLEN")

    if path:
        if not re.fullmatch(
            r"[A-Z0-9]{1,6}-[0-9]{1,2}"
            r"(?:,[A-Z0-9]{1,6}-[0-9]{1,2})*",
            path,
        ):
            raise APRSTransmitError("Invalid APRS digipeater path")

    unproto = f"APRS VIA {path}" if path else "APRS"

    return (
        f"UNPROTO {unproto}",
        "CONVERS",
        payload + "\r",
        "\x03",
        f"UNPROTO {original}",
    )

def transmit_aprs_message(
    kam,
    payload: str,
    original_unproto: str,
    path: str = "",
    *,
    dry_run: bool = True,
) -> dict:
    """
    Prepare an APRS transmission.

    This initial implementation is deliberately offline-only.
    It does not perform hardware I/O, regardless of dry_run.
    """
    if dry_run is not True:
        raise APRSTransmitError(
            "Live APRS transmission is not implemented"
        )

    plan = build_aprs_tx_plan(
        payload=payload,
        original_unproto=original_unproto,
        path=path,
    )

    return {
        "dry_run": True,
        "transmitted": False,
        "serial_writes": 0,
        "commands": list(plan),
    }

def execute_aprs_tx_transaction(kam, payload: str, path: str = "") -> dict:
    """
    Execute one APRS UI-message transaction on the supplied interface.

    FOR OFFLINE FAKE-SERIAL TESTING ONLY.

    This function performs real writes on whatever kam object it receives.
    It must not be exposed through the daemon or called with live hardware
    until exclusive ownership and operating-mode safety are implemented.
    """
    if getattr(kam.serial, "_aprs_offline_simulation", False) is not True:
        raise APRSTransmitError(
            "APRS transaction requires an offline simulation interface"
        )
    from aprs import format_aprs_message

    if not isinstance(payload, str) or len(payload) < 11:
        raise APRSTransmitError("Invalid APRS payload")

    # Validate the complete payload through the existing formatter.
    if payload[0] != ":" or payload[10] != ":":
        raise APRSTransmitError("Invalid APRS message structure")

    addressee = payload[1:10].strip()
    message = payload[11:]

    if format_aprs_message(addressee, message) != payload:
        raise APRSTransmitError("Noncanonical APRS message")

    # Validate path before touching hardware.
    build_aprs_tx_plan(payload, "CQ/CQ", path)

    original = None
    changed = False
    submitted = False
    in_convers = False

    try:
        # Verify all six APRS prerequisites immediately before
        # changing configuration or entering conversational mode.
        check_aprs_tx_preflight(kam)

        original = validate_unproto(kam.get("UNPROTO"))

        plan = build_aprs_tx_plan(payload, original, path)
        new_unproto = plan[0].removeprefix("UNPROTO ")

        # The TNC may apply the setting even if its acknowledgment
        # is lost. Treat the configuration as potentially changed
        # before sending the command.
        changed = True

        try:
            kam.set("UNPROTO", new_unproto)
        except (KAMTimeoutError, TimeoutError) as exc:
            # We cannot safely assume the TNC is still
            # interpreting input as commands.
            changed = False

            raise APRSTransmitError(
                "UNPROTO command timed out. TNC state is uncertain; "
                "automatic restoration was not attempted. "
                "Manual verification required."
            ) from exc
        if validate_unproto(kam.get("UNPROTO")) != new_unproto:
            raise APRSTransmitError("UNPROTO change verification failed")

        # CONVERS does not return a command prompt.
        # Once CONVERS is attempted, the TNC may have switched modes
        # even if the serial write or flush subsequently fails.
        in_convers = True
        convers_bytes = b"CONVERS\r"
        written = kam.serial.write(convers_bytes)

        if written != len(convers_bytes):
            raise APRSTransmitError(
                "Short CONVERS command write: "
                f"{written!r} of {len(convers_bytes)} bytes. "
                "TNC operating mode is uncertain."
            )

        kam.serial.flush()
        try:
            payload_bytes = payload.encode("ascii") + b"\r"
            written = kam.serial.write(payload_bytes)

            if written != len(payload_bytes):
                raise APRSTransmitError(
                    "Short APRS payload write: "
                    f"{written!r} of {len(payload_bytes)} bytes"
                )

            kam.serial.flush()
        except Exception as exc:
            raise APRSTransmitError(
                "APRS payload submission is uncertain; the TNC may "
                "already have accepted the packet. "
                "Do not retry automatically."
            ) from exc

        submitted = True

    finally:
        command_mode_confirmed = not in_convers

        if in_convers:
            try:
                recover_aprs_command_mode(kam)
                command_mode_confirmed = True
            except Exception as exc:
                raise APRSTransmitError(
                    "Unable to confirm KAM-XL command mode after "
                    "APRS transmission attempt. TNC state is uncertain; "
                    "manual intervention required. UNPROTO restoration "
                    "was not attempted. APRS payload may already have "
                    "been transmitted; do not retry automatically."
                ) from exc

        if changed and original is not None:
            if not command_mode_confirmed:
                raise APRSTransmitError(
                    "Cannot safely restore UNPROTO outside command mode"
                )

            try:
                kam.set("UNPROTO", original)

                restored = validate_unproto(kam.get("UNPROTO"))

                if restored != original:
                    raise APRSTransmitError(
                        "UNPROTO restoration verification failed"
                    )

            except Exception as exc:
                transmission_warning = (
                    " APRS payload may already have been transmitted; "
                    "do not retry automatically."
                    if in_convers else ""
                )

                raise APRSTransmitError(
                    "Unable to verify original UNPROTO configuration. "
                    "TNC state is uncertain; manual intervention required."
                    + transmission_warning
                ) from exc

    return {
        "submitted_to_tnc": submitted,
        "rf_confirmed": False,
        "delivered": False,
        "unproto_restored": True,
    }
