# Greedy pass@1 on a fixed HumanEval subset, using the model's own chat template via llama-server /v1/chat/completions.
import json, os, re, subprocess, sys, tempfile, time, urllib.request, resource
label, model, step = sys.argv[1], sys.argv[2], int(sys.argv[3])
S = os.environ.get('QLAB') or os.path.dirname(os.path.abspath(__file__)); port = 8101
probs = [json.loads(l) for l in open(f"{S}/data/HumanEval.jsonl")][::step]
srv = subprocess.Popen([f"{os.environ.get('LLAMA_BIN', S + '/build-avx512/bin')}/llama-server", "-m", model, "-c", "4096", "-t", "4", "--port", str(port),
                        "--parallel", "1", "--no-webui", "--jinja"], stdout=open(f"{S}/out/{label}.chat.srv.log", "w"), stderr=subprocess.STDOUT)
def post(body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=900))
for _ in range(240):
    try: urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2); break
    except Exception: time.sleep(2)
def limits(): resource.setrlimit(resource.RLIMIT_CPU, (10, 10)); resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
def extract(text):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m: return m.group(1)
    m = re.search(r"```(?:python|py)?\s*\n(.*)", text, re.S)   # unterminated block
    return m.group(1) if m else text
passed, rows, tps = 0, [], []
try:
    for p in probs:
        ask = ("Complete the following Python function. Reply with the complete function implementation "
               "in a single ```python code block and nothing else.\n\n```python\n" + p["prompt"] + "```")
        d = post({"messages": [{"role": "user", "content": ask}], "max_tokens": 768, "temperature": 0})
        txt = d["choices"][0]["message"]["content"] or ""
        tps.append(d.get("timings", {}).get("predicted_per_second", 0.0))
        code = extract(txt)
        imports = "".join(l + "\n" for l in p["prompt"].splitlines() if re.match(r"(from|import) ", l))
        body = code if re.search(rf"def\s+{re.escape(p['entry_point'])}\s*\(", code) else p["prompt"] + code
        prog = imports + body + "\n\n" + p["test"] + f"\n\ncheck({p['entry_point']})\n"
        with tempfile.TemporaryDirectory() as td:
            try: ok = subprocess.run([sys.executable, "-I", "-c", prog], cwd=td, timeout=15, capture_output=True, preexec_fn=limits).returncode == 0
            except subprocess.TimeoutExpired: ok = False
        passed += ok; rows.append({"task": p["task_id"], "ok": ok})
        print(f"{label} {p['task_id']} {'PASS' if ok else 'fail'} ({passed}/{len(rows)})", flush=True)
finally:
    srv.terminate(); srv.wait()
json.dump({"label": label, "mode": "chat", "passed": passed, "n": len(probs), "pass_at_1": passed / len(probs),
           "gen_tok_s_mean": sum(tps) / max(1, len(tps)), "rows": rows}, open(f"{S}/out/{label}.chat.humaneval.json", "w"))
print("RESULT", label, passed, len(probs), "gen tok/s", round(sum(tps) / max(1, len(tps)), 2))
