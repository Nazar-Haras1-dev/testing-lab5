"""Невеликий клієнт адмін-API WireMock (лише стандартна бібліотека).
Налаштовує відповіді (stubs), чистить стан і читає журнал запитів."""
import json
import time
import urllib.request

POLICY_PATH = "/rental-policy"


class WireMock:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url
        self.timeout = timeout
        # без системного проксі: адмін-запити йдуть напряму до контейнера
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------------------------------------------------------------
    # низькорівневий виклик адмін-API
    # ---------------------------------------------------------------
    def _call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with self._opener.open(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    # ---------------------------------------------------------------
    # стан: готовність, очищення
    # ---------------------------------------------------------------
    def is_ready(self):
        try:
            self._call("GET", "/__admin/mappings")
            return True
        except OSError:
            return False

    def reset(self):
        """Прибирає всі налаштовані відповіді та очищає журнал запитів."""
        self._call("DELETE", "/__admin/mappings")
        self._call("DELETE", "/__admin/requests")

    def mappings(self):
        return self._call("GET", "/__admin/mappings")["mappings"]

    # ---------------------------------------------------------------
    # налаштування відповідей WireMock
    # ---------------------------------------------------------------
    def stub_policy(self, status=200, body=None, headers=None, delay_ms=0):
        """POST /rental-policy -> відповідь із заданим статусом, тілом і затримкою."""
        response = {"status": status, "headers": {"Content-Type": "application/json", **(headers or {})}}
        if body is not None:
            response["body"] = body if isinstance(body, str) else json.dumps(body)
        if delay_ms:
            response["fixedDelayMilliseconds"] = delay_ms
        self._call("POST", "/__admin/mappings", {
            "request": {"method": "POST", "url": POLICY_PATH},
            "response": response,
        })

    def allow(self):
        """Успішна відповідь: HTTP 200 {"allowed": true}."""
        self.stub_policy(200, {"allowed": True})

    def deny(self):
        """Бізнес-відмова: HTTP 200 {"allowed": false}."""
        self.stub_policy(200, {"allowed": False})

    # ---------------------------------------------------------------
    # журнал запитів, які реально дійшли до WireMock
    # ---------------------------------------------------------------
    def requests(self, expected=0, wait=5.0):
        """Повертає запити з журналу. Якщо очікуємо хоча б `expected`, трохи чекає, поки вони з'являться."""
        deadline = time.monotonic() + (wait if expected else 0)
        while True:
            found = self._journal()
            if len(found) >= expected or time.monotonic() >= deadline:
                return found
            time.sleep(0.1)

    def _journal(self):
        entries = self._call("GET", "/__admin/requests")["requests"]
        return [{
            "method": entry["request"]["method"],
            "url": entry["request"]["url"],
            "headers": {name.lower(): value for name, value in entry["request"].get("headers", {}).items()},
            "body": entry["request"].get("body", ""),
        } for entry in entries]
