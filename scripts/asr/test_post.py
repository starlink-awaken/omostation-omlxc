import base64
import json
import time
import urllib.request

b64 = base64.b64encode(open(r"C:\Users\xia\asr_input.wav", "rb").read()).decode()
req = urllib.request.Request("http://127.0.0.1:8390/asr",
    data=json.dumps({"audio_b64": b64, "language": "en"}).encode(),
    headers={"Content-Type": "application/json"})
t0 = time.time()
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read())
    print(f"{time.time()-t0:.1f}s ->", json.dumps(d, ensure_ascii=False))
