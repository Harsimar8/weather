"""Connection to FlightGear through its telnet property interface.

Start FlightGear with:  --telnet=5401
"""
import socket
import threading
import time

# Property that takes a manual METAR string. Check it in FlightGear under
# Debug > Browse Internal Properties > environment, as names can differ
# between versions and weather systems.
METAR_PROPERTY = "/environment/metar/data"

POSITION_PROPS = {
    "lat": "/position/latitude-deg",
    "lon": "/position/longitude-deg",
    "alt_ft": "/position/altitude-ft",
    "heading": "/orientation/heading-deg",
}


class FGLink:
    def __init__(self, host="localhost", port=5401, poll_hz=5.0):
        self.host, self.port = host, port
        self.poll_interval = 1.0 / poll_hz
        self.sock = None
        self.reader = None
        self.lock = threading.Lock()
        self.state = {}
        self.connected = False
        self.error = ""
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ connect
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=3)
        self.sock.settimeout(2)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._send("data")  # raw mode: replies are just the value
        self.connected = True
        self.error = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.connected = False
        try:
            if self.sock:
                self._send("quit")
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    # ------------------------------------------------------- low level I/O
    def _send(self, line):
        self.sock.sendall((line + "\r\n").encode("utf-8"))

    def _drain(self):
        """Throw away any unexpected leftover replies."""
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(2)

    def get(self, path):
        with self.lock:
            self._send(f"get {path}")
            return self.reader.readline().strip()

    def set(self, path, value):
        with self.lock:
            self._send(f"set {path} {value}")
            time.sleep(0.05)
            self._drain()

    # ------------------------------------------------------------ polling
    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                new = {k: float(self.get(p)) for k, p in POSITION_PROPS.items()}
                self.state = new
            except (OSError, ValueError) as exc:
                self.error = str(exc) or exc.__class__.__name__
                self.connected = False
                return
            time.sleep(self.poll_interval)

    def send_metar(self, metar):
        self.set(METAR_PROPERTY, metar)
