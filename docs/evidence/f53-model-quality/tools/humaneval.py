# Greedy pass@1 on a fixed HumanEval subset (every STEPth problem), via llama-server /completion.
import json, os, subprocess, sys, tempfile, time, urllib.request, resource
label, model, step = sys.argv[1], sys.argv[2], int(sys.argv[3])
S = os.environ.get('QLAB') or os.path.dirname(os.path.abspath(__file__)); port = 8099
probs = [json.loads(l) for l in open(f"{S}/data/HumanEval.jsonl")][::step]
srv = subprocess.Popen([f"{os.environ.get('LLAMA_BIN', S + '/build-avx512/bin')}/llama-server", "-m", model, "-c", "2048", "-t", "4", "--port", str(port),
                        "--parallel", "1", "--no-webui"], stdout=open(f"{S}/out/{label}.srv.log", "w"), stderr=subprocess.STDOUT)
def post(body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}/completion", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=900))
for _ in range(240):
    try: urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2); break
    except Exception: time.sleep(2)
STOP = ["\ndef ", "\nclass ", "\nif __name__", "\nprint(", "\n#", "\nassert", "\n```"]
def limits(): resource.setrlimit(resource.RLIMIT_CPU, (10, 10)); resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
passed, rows, tps = 0, [], []
try:
    for p in probs:
        d = post({"prompt": p["prompt"], "n_predict": 384, "temperature": 0, "stop": STOP, "cache_prompt": False})
        comp = d["content"]; tps.append(d["timings"]["predicted_per_second"])
        prog = p["prompt"] + comp + "\n\n" + p["test"] + f"\n\ncheck({p['entry_point']})\n"
        with tempfile.TemporaryDirectory() as td:
            try: ok = subprocess.run([sys.executable, "-I", "-c", prog], cwd=td, timeout=15, capture_output=True, preexec_fn=limits).returncode == 0
            except subprocess.TimeoutExpired: ok = False
        passed += ok; rows.append({"task": p["task_id"], "ok": ok})
        print(f"{label} {p['task_id']} {'PASS' if ok else 'fail'} ({passed}/{len(rows)})", flush=True)
finally:
    srv.terminate(); srv.wait()
json.dump({"label": label, "passed": passed, "n": len(probs), "pass_at_1": passed / len(probs),
           "gen_tok_s_mean": sum(tps) / len(tps), "rows": rows}, open(f"{S}/out/{label}.humaneval.json", "w"))
print("RESULT", label, passed, len(probs), "gen tok/s", round(sum(tps) / len(tps), 2))
