import json
import urllib.request
import urllib.error

nasal_file = r"C:\Users\Harsimar Kaur\Downloads\FlightGear-2024.1.7-data\fgdata_2024_1\Nasal\wxradar_export.nas"

with open(nasal_file, "r", encoding="ascii") as f:
    code = f.read()

payload = {
    "name": "",
    "children": [
        {
            "name": "script",
            "index": 0,
            "value": code
        }
    ]
}

request = urllib.request.Request(
    "http://localhost:8080/run.cgi?value=nasal",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST"
)

try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print("HTTP status:", response.status)
        print("FlightGear response:", response.read().decode("utf-8", errors="replace"))
except urllib.error.HTTPError as e:
    print("HTTP error:", e.code)
    print("Details:", e.read().decode("utf-8", errors="replace"))
except Exception as e:
    print("Error:", e)