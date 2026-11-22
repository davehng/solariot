# Based on the original Sungrow Modbus TCP encryption wrapper by
# Roberto Panerai Velloso (https://github.com/rpvelloso/Sungrow-Modbus).

"""Self-contained Sungrow encrypted Modbus TCP client (AES-128-ECB).

Implements Sungrow's Modbus TCP AES-128-ECB framing and daily key exchange
directly over Python's stdlib socket module, supporting Modbus function codes
0x03 (Read Holding Registers) and 0x04 (Read Input Registers), with automatic
fallback to unencrypted Modbus TCP when the inverter does not enable crypto.
"""

from datetime import date
import logging
import socket
import struct

try:
    from Cryptodome.Cipher import AES
except ImportError:  # pragma: no cover - support pycryptodome package name too
    from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

PRIV_KEY = b"Grow#0*2Sun68CbE"
NO_CRYPTO1 = b"\x00" * 16
NO_CRYPTO2 = b"\xff" * 16
GET_KEY = b"\x68\x68\x00\x00\x00\x06\xf7\x04\x0a\xe7\x00\x08"
HEADER = b"\x68\x68"
FC_READ_HOLDING_REGISTERS = 0x03
FC_READ_INPUT_REGISTERS = 0x04


class ModbusRegisterResponse:
    """Minimal response object matching pymodbus's read_*_registers interface."""

    def __init__(self, registers=None, error=False, exception_code=None, message=""):
        self._error = error
        self.exception_code = exception_code
        self.message = message
        if registers is not None:
            self.registers = registers

    def isError(self):
        return self._error

    def __repr__(self):
        if self._error:
            parts = ["ModbusRegisterResponse(error=True"]
            if self.exception_code is not None:
                parts.append(f", exception_code={self.exception_code:#x}")
            if self.message:
                parts.append(f", message={self.message!r}")
            parts.append(")")
            return "".join(parts)
        return f"ModbusRegisterResponse(registers={len(self.registers)} items)"


class SungrowModbusTcpClient:
    """Modbus TCP client for Sungrow inverters with AES-128-ECB encryption."""

    def __init__(
        self,
        host,
        port=502,
        timeout=3,
        retries=3,
        priv_key=PRIV_KEY,
        **_ignored,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self._priv_key = priv_key
        self._sock = None
        self._key = None
        self._aes_ecb = None
        self._key_date = None
        self._tx_id = 0

    def _open_socket(self):
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            self._sock = sock
            return True
        except OSError as err:
            logger.debug("TCP connect to %s:%s failed: %s", self.host, self.port, err)
            self._sock = None
            return False

    def _recv_exact(self, num_bytes):
        if self._sock is None:
            raise ConnectionError("Socket is not connected")
        buf = bytearray()
        while len(buf) < num_bytes:
            chunk = self._sock.recv(num_bytes - len(buf))
            if not chunk:
                raise ConnectionError(
                    f"Connection closed after {len(buf)}/{num_bytes} bytes"
                )
            buf.extend(chunk)
        return bytes(buf)

    def _setup_crypto(self, pub_key):
        self._key = bytes(a ^ b for a, b in zip(pub_key, self._priv_key))
        self._aes_ecb = AES.new(self._key, AES.MODE_ECB)
        self._key_date = date.today()
        logger.debug("Sungrow AES-128-ECB encryption enabled for %s", self.host)

    def _invalidate_key(self):
        self._key = None
        self._aes_ecb = None
        self._key_date = None

    def _needs_key(self):
        return self._key is None or self._key_date != date.today()

    def _negotiate_key(self):
        self._invalidate_key()
        self._sock.sendall(GET_KEY)
        key_packet = self._recv_exact(25)
        pub_key = key_packet[9:]
        if (
            len(pub_key) == 16
            and pub_key != NO_CRYPTO1
            and pub_key != NO_CRYPTO2
        ):
            self._setup_crypto(pub_key)
        else:
            self._key = b"no encryption"
            self._aes_ecb = None
            self._key_date = date.today()
            logger.debug("Sungrow inverter %s responded without encryption", self.host)

    def connect(self):
        if not self._open_socket():
            self._invalidate_key()
            return False

        if self._needs_key():
            try:
                self._negotiate_key()
            except (OSError, ConnectionError) as err:
                logger.warning("Sungrow key exchange failed: %s", err)
                self._invalidate_key()
                self.close()
                return False

            # After the plaintext key exchange, Sungrow inverters expect a fresh
            # TCP connection before encrypted Modbus requests are sent.
            if not self._open_socket():
                return False

        return True

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _next_tx_id(self):
        self._tx_id = (self._tx_id % 0xFFFF) + 1
        return self._tx_id

    def _send_and_receive_encrypted(self, request, tx_id):
        length = len(request)
        padding = 16 - (length % 16)
        padded = HEADER + request[2:] + (b"\xff" * padding)
        crypto_header = bytes([1, 0, length, padding])
        self._sock.sendall(crypto_header + self._aes_ecb.encrypt(padded))

        header = self._recv_exact(4)
        packet_len = header[2]
        resp_padding = header[3]
        total_len = packet_len + resp_padding
        if total_len == 0 or (total_len % 16) != 0:
            raise ValueError(
                f"Invalid Sungrow crypto header {header.hex()} "
                f"(packet_len={packet_len}, padding={resp_padding})"
            )

        encrypted_packet = self._recv_exact(total_len)
        decrypted = self._aes_ecb.decrypt(encrypted_packet)
        return struct.pack(">H", tx_id) + decrypted[2:packet_len]

    def _send_and_receive_plain(self, request):
        self._sock.sendall(request)
        mbap = self._recv_exact(7)
        _, _, mbap_len, _ = struct.unpack(">HHHB", mbap)
        if mbap_len < 2:
            raise ValueError(f"Invalid MBAP length: {mbap_len}")
        pdu = self._recv_exact(mbap_len - 1)
        return mbap + pdu

    def _decode_read_response(self, packet, expected_tx_id, expected_fc, count):
        if len(packet) < 9:
            raise ValueError(f"Modbus response too short ({len(packet)} bytes)")

        resp_tx_id, proto_id, _, _, resp_fc = struct.unpack(">HHHBB", packet[:8])
        if proto_id != 0:
            raise ValueError(f"Invalid Modbus protocol ID: {proto_id:#x}")
        if resp_tx_id != expected_tx_id:
            raise ValueError(
                f"Transaction ID mismatch: got {resp_tx_id}, expected {expected_tx_id}"
            )

        if resp_fc == (expected_fc | 0x80):
            return ModbusRegisterResponse(error=True, exception_code=packet[8])

        if resp_fc != expected_fc:
            raise ValueError(
                f"Unexpected function code: got {resp_fc:#x}, expected {expected_fc:#x}"
            )

        byte_count = packet[8]
        expected_bytes = count * 2
        if byte_count != expected_bytes or len(packet) < 9 + byte_count:
            raise ValueError(
                f"Register byte count mismatch: header={byte_count}, "
                f"expected={expected_bytes}, packet_len={len(packet)}"
            )

        registers = list(struct.unpack(f">{count}H", packet[9 : 9 + byte_count]))
        return ModbusRegisterResponse(registers=registers, error=False)

    def _execute_read(self, func_code, address, count, unit_id):
        last_error = None
        for attempt in range(1, self.retries + 1):
            if self._sock is None and not self.connect():
                last_error = ConnectionError(
                    f"Could not connect to {self.host}:{self.port}"
                )
                continue

            tx_id = self._next_tx_id()
            request = struct.pack(
                ">HHHBBHH",
                tx_id,
                0,
                6,
                int(unit_id),
                func_code,
                int(address),
                int(count),
            )

            try:
                if self._aes_ecb is not None:
                    packet = self._send_and_receive_encrypted(request, tx_id)
                else:
                    packet = self._send_and_receive_plain(request)
                return self._decode_read_response(packet, tx_id, func_code, int(count))
            except (OSError, ConnectionError, ValueError) as err:
                last_error = err
                logger.debug(
                    "Sungrow Modbus read attempt %d/%d failed (fc=%#x, addr=%s, count=%s): %s",
                    attempt,
                    self.retries,
                    func_code,
                    address,
                    count,
                    err,
                )
                # Close socket and invalidate cached key so a rebooted inverter
                # (which may have rotated its daily key) re-negotiates on retry.
                self.close()
                if self._aes_ecb is not None:
                    self._invalidate_key()

        return ModbusRegisterResponse(error=True, message=str(last_error))

    @staticmethod
    def _resolve_unit(device_id=None, slave=None, unit=None):
        for val in (device_id, slave, unit):
            if val is not None:
                return val
        return 1

    def read_input_registers(
        self, address, count=1, device_id=None, slave=None, unit=None
    ):
        unit_id = self._resolve_unit(device_id=device_id, slave=slave, unit=unit)
        return self._execute_read(FC_READ_INPUT_REGISTERS, address, count, unit_id)

    def read_holding_registers(
        self, address, count=1, device_id=None, slave=None, unit=None
    ):
        unit_id = self._resolve_unit(device_id=device_id, slave=slave, unit=unit)
        return self._execute_read(FC_READ_HOLDING_REGISTERS, address, count, unit_id)
