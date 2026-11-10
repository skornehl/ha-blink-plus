# Offline tests

These run the live view engine (`immis.py`) against a fake Blink IMMIS relay
(TLS) and real ffmpeg. You don't need a Blink account or Home Assistant. CI runs
them on every push (`.github/workflows/validate.yml`).

```bash
cd tests
ffmpeg -f lavfi -i testsrc=size=640x360:rate=15 -f lavfi -i sine -t 6 \
  -c:v libx264 -g 15 -pix_fmt yuv420p -c:a aac -f mpegts sample.ts
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 2 -subj /CN=immis.test

python3 test_immis.py   . ../custom_components/blink_plus/immis.py token_ok   # stream + extended_liveview hand-off
python3 test_immis.py   . ../custom_components/blink_plus/immis.py null_only  # relay rejects the token -> zero-token fallback
python3 test_preempt.py . ../custom_components/blink_plus/immis.py            # one live view per sync module, STOP on hand-off
python3 test_failure.py . ../custom_components/blink_plus/immis.py            # silent camera: 1 attempt, STOP, cool-down
python3 test_archive.py ../custom_components/blink_plus/archive.py            # clip archive: USB (no plan) + cloud (plan), retries, resume,
                                                                               # clips skipped after 3 failed runs, failed cloud clips, unload, no source
```
