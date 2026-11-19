import json, subprocess, sys, time
prefix = sys.argv[1]; out = sys.argv[2]; dur = float(sys.argv[3]); t0 = float(sys.argv[4]) if len(sys.argv) > 4 else time.time()
last = None
with open(out, "a") as f:
    end = time.time() + dur
    while time.time() < end:
        try:
            d = json.loads(subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=20).stdout)
        except Exception as e:
            d = []
        rows = sorted([{k: r.get(k) for k in ("pid", "kind", "sessionId", "status", "startedAt")} | {"keys": sorted(r)} for r in d if str(r.get("cwd", "")).startswith(prefix)], key=lambda r: r["pid"] or 0)
        if rows != last:
            f.write(json.dumps({"t": round(time.time() - t0, 2), "side": "claude agents --json", "rows": rows}) + "\n"); f.flush()
            last = rows
        time.sleep(0.5)
