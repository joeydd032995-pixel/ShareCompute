import re,sys,glob,os,math
O=sys.argv[1]
def rd(p): return open(p,errors='ignore').read().replace('\r','\n') if os.path.exists(p) else ''
labels=sorted({os.path.basename(f).split('.')[0] for f in glob.glob(O+'/*.ppl.log')})
print(f"{'model':16} {'PPL':>7} {'bits/byte':>9} {'KLD':>7} {'top1%':>6} {'MMLU%':>6} {'HS%':>5}")
for l in labels:
    pp=rd(f'{O}/{l}.ppl.log'); m=re.search(r'Final estimate: PPL = ([\d.]+)',pp)
    m2=re.search(r'Mean PPL\(Q\)\s+:\s+([\d.]+)',pp)
    ppl=float(m.group(1)) if m else (float(m2.group(1)) if m2 else float('nan'))
    tok=re.search(r'Total number of tokens: (\d+)',rd(f'{O}/{l}.tok')); nt=int(tok.group(1)) if tok else 0
    bpb=math.log2(ppl)*nt/200000 if nt and ppl==ppl else float('nan')
    kl=re.search(r'Mean    KLD:\s+([\d.]+)',pp); t1=re.search(r'Same top p:\s+([\d.]+)',pp)
    def fin(n):
        m=re.findall(r'Final result: ([\d.]+)',rd(f'{O}/{l}.{n}.log')); return float(m[-1]) if m else float('nan')
    hs=re.findall(r'^\s*(?:200)\s+([\d.]+)%',rd(f'{O}/{l}.hs.log'),re.M)
    print(f"{l:16} {ppl:7.3f} {bpb:9.3f} {float(kl.group(1)) if kl else float('nan'):7.4f} {float(t1.group(1)) if t1 else float('nan'):6.1f} {fin('mmlu'):6.1f} {float(hs[-1]) if hs else float('nan'):5.1f}")
