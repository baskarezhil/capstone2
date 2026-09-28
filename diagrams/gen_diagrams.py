import json, random, os, sys, unicodedata

OUT = sys.argv[1]
random.seed(42)

PAL = {
    # kind: (stroke, fill, zone_fill)
    "gray":   ("#495057", "#e9ecef", "#f8f9fa"),
    "red":    ("#c92a2a", "#ffc9c9", "#fff5f5"),
    "blue":   ("#1971c2", "#a5d8ff", "#e7f5ff"),
    "violet": ("#6741d9", "#d0bfff", "#f3f0ff"),
    "green":  ("#2f9e44", "#b2f2bb", "#ebfbee"),
    "yellow": ("#e67700", "#ffec99", "#fff9db"),
    "cyan":   ("#0c8599", "#99e9f2", "#e3fafc"),
    "orange": ("#d9480f", "#ffd8a8", "#fff4e6"),
    "teal":   ("#087f5b", "#96f2d7", "#e6fcf5"),
    "hitl":   ("#e03131", "#ffa8a8", "#fff5f5"),
}


def char_w(c, fs):
    if ord(c) > 0x2000 and unicodedata.category(c) in ("So", "No", "Sk"):
        return fs * 1.0
    if c == " ":
        return fs * 0.28
    if c.isupper():
        return fs * 0.66
    return fs * 0.53


def text_w(s, fs):
    return max(sum(char_w(c, fs) for c in line) for line in s.split("\n"))


class Scene:
    def __init__(self):
        self.back, self.els, self.n = [], [], 0
        self.byid = {}

    def _id(self, p):
        self.n += 1
        return f"{p}{self.n}"

    def _base(self, t, x, y, w, h, stroke="#1e1e1e", fill="transparent", **kw):
        el = dict(id=kw.pop("id", None) or self._id(t[:3]), type=t, x=x, y=y, width=w, height=h,
                  angle=0, strokeColor=stroke, backgroundColor=fill, fillStyle="solid",
                  strokeWidth=2, strokeStyle="solid", roughness=1, opacity=100, groupIds=[],
                  frameId=None, roundness=None, seed=random.randint(1, 2**31),
                  version=1, versionNonce=random.randint(1, 2**31), isDeleted=False,
                  boundElements=[], updated=1, link=None, locked=False)
        el.update(kw)
        self.byid[el["id"]] = el
        return el

    def text(self, x, y, s, fs=16, color="#1e1e1e", align="left", container=None, back=False):
        w, h = text_w(s, fs), s.count("\n") * fs * 1.25 + fs * 1.25
        el = self._base("text", x, y, w, h, stroke=color, text=s, originalText=s, fontSize=fs,
                        fontFamily=2, textAlign=align, verticalAlign="middle" if container else "top",
                        containerId=container, autoResize=True, lineHeight=1.25)
        (self.back if back else self.els).append(el)
        return el

    def box(self, x, y, w, h, s, kind="blue", fs=14, bold=False):
        stroke, fill, _ = PAL[kind]
        r = self._base("rectangle", x, y, w, h, stroke=stroke, fill=fill, roundness={"type": 3})
        self.els.append(r)
        tw = text_w(s, fs)
        if tw > w - 12:
            print(f"WARN text too wide ({tw:.0f} > {w-12}): {s!r}")
        th = (s.count("\n") + 1) * fs * 1.25
        if th > h - 8:
            print(f"WARN text too tall ({th:.0f} > {h-8}): {s!r}")
        t = self.text(x + w / 2 - tw / 2, y + h / 2 - th / 2, s, fs=fs, align="center", container=r["id"])
        r["boundElements"].append({"type": "text", "id": t["id"]})
        return r

    def zone(self, x, y, w, h, title, kind="gray", fs=18):
        stroke, _, zfill = PAL[kind]
        z = self._base("rectangle", x, y, w, h, stroke=stroke, fill=zfill, strokeStyle="dashed",
                       roundness={"type": 3}, strokeWidth=2)
        self.back.append(z)
        self.text(x + 14, y + 10, title, fs=fs, color=stroke, back=True)
        return z

    def arrow(self, pts, start=None, end=None, dashed=False, color="#343a40", both=False, label=None,
              label_at=None, lfs=13, kind="arrow", head=True, width=2):
        x0, y0 = pts[0]
        rel = [[px - x0, py - y0] for px, py in pts]
        xs, ys = [p[0] for p in rel], [p[1] for p in rel]
        a = self._base(kind, x0, y0, max(xs) - min(xs), max(ys) - min(ys), stroke=color, points=rel,
                       strokeStyle="dashed" if dashed else "solid", strokeWidth=width,
                       lastCommittedPoint=None, startBinding=None, endBinding=None,
                       startArrowhead="arrow" if both else None,
                       endArrowhead="arrow" if (head and kind == "arrow") else None)
        if kind == "arrow":
            a["elbowed"] = False
        for which, tgt in (("startBinding", start), ("endBinding", end)):
            if tgt:
                a[which] = {"elementId": tgt["id"], "focus": 0, "gap": 4}
                tgt["boundElements"].append({"type": "arrow", "id": a["id"]})
        self.els.append(a)
        if label:
            lx, ly = label_at
            self.text(lx, ly, label, fs=lfs, color=color)
        return a

    def save(self, name):
        doc = {"type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
               "elements": self.back + self.els,
               "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20}, "files": {}}
        p = os.path.join(OUT, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print("wrote", p, len(doc["elements"]), "elements")


def c(el, side):
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    return {"l": (x, y + h / 2), "r": (x + w, y + h / 2), "t": (x + w / 2, y), "b": (x + w / 2, y + h)}[side]


# =====================================================================
# Diagram 1: Top-level cloud architecture
# =====================================================================
s = Scene()
s.text(40, 24, "CloudScale Global Networks: AIOps Incident Remediation & Auto-Healing Platform", fs=28)
s.text(40, 64, "Pod 2 · Top-level cloud architecture (C4 Level 2: Containers) · Primary: AWS EKS · Targets: AWS EKS + Azure AKS", fs=16, color="#495057")

s.zone(40, 100, 240, 560, "Signal Sources", "gray")
s.zone(320, 100, 280, 560, "Edge & Input Guardrails", "red")
s.zone(640, 100, 500, 560, "Agent Runtime: Amazon EKS (ns: aiops-agents)", "blue")
s.zone(1180, 100, 320, 380, "Tool Plane: MCP (Zero-Trust)", "cyan")
s.zone(1540, 100, 260, 380, "Managed Infrastructure", "orange")
s.zone(1180, 500, 620, 160, "Human-in-the-Loop Boundary", "hitl")
s.zone(640, 700, 860, 200, "LLM & State", "green")
s.zone(1540, 700, 260, 200, "Governance", "violet")
s.zone(40, 940, 1760, 170, "Observability: OpenTelemetry", "teal")

# sources
prom = s.box(60, 150, 200, 80, "Prometheus\n(metrics · alert rules)", "gray")
dd = s.box(60, 260, 200, 80, "Datadog\n(APM · logs)", "gray")
am = s.box(60, 370, 200, 80, "Alertmanager /\nPagerDuty", "gray")
cw = s.box(60, 480, 200, 80, "CloudWatch /\nAzure Monitor", "gray")

# edge
gw = s.box(340, 150, 240, 100, "API Gateway\nOIDC · mTLS · rate limit\n(webhook ingress)", "red")
guard = s.box(340, 290, 240, 130, "Input Guardrails (L1–L2)\nLlamaGuard 3 · Guardrails AI\ninjection scan (logs/alerts)\nsecret & PII redaction", "red")
norm = s.box(340, 460, 240, 90, "Event Normalizer\nalert → Incident schema\n(dedupe · correlate)", "red")

# runtime
sup = s.box(700, 140, 380, 90, "Supervisor / Orchestrator\nLangGraph StateGraph (hierarchical)\nrouting · step limits · token budget", "violet")
tri = s.box(700, 270, 110, 110, "Triage &\nDiagnosis\nAgent\n(RCA)", "blue")
pla = s.box(835, 270, 110, 110, "Remediation\nPlanner\nAgent", "blue")
exe = s.box(970, 270, 110, 110, "Execution &\nVerification\nAgent", "blue")
cls = s.box(700, 420, 180, 100, "Risk Classifier + L3\nrm -rf · sudo · priv-esc\ncurl/nc exfil blocked", "red")
hitl = s.box(900, 420, 180, 100, "HITL Gate Node\nsafe & C≥0.85 → auto\nelse → pause for SRE", "hitl")
st = s.box(700, 550, 180, 80, "Incident State\n(typed graph state)", "green")
res = s.box(900, 550, 180, 80, "Resilience Layer\ncircuit breaker · retry\ntimeouts · fallback", "yellow")

# MCP
mcp = s.box(1200, 140, 280, 70, "MCP Server (FastMCP)\nStreamable HTTP · OAuth 2.1", "cyan")
t1 = s.box(1200, 225, 280, 42, "fetch_k8s_logs · READ", "green")
t2 = s.box(1200, 277, 280, 42, "restart_service · NON-DESTRUCTIVE", "yellow")
t3 = s.box(1200, 329, 280, 42, "apply_hotfix · DESTRUCTIVE (HITL)", "red")
opa = s.box(1200, 390, 280, 75, "OPA Policy Engine\nallowlist · RBAC · dry-run first\nper-call short-lived creds", "cyan")

# infra
aws = s.box(1560, 150, 220, 80, "AWS EKS clusters\n(us-east-1 · eu-west-1)", "orange")
az = s.box(1560, 260, 220, 80, "Azure AKS clusters\n(workload identity)", "orange")
vault = s.box(1560, 370, 220, 80, "HashiCorp Vault\n(dynamic secrets)", "orange")

# HITL
appr = s.box(1200, 545, 280, 95, "HITL Approval Service\nRCA · diff · blast radius · rollback\ncard scrubbed of secrets (L4)\napprove / reject / modify", "hitl", fs=13)
sre = s.box(1560, 545, 220, 95, "SRE On-Call 👤\nSlack / Teams / Console", "orange")

# LLM & state
ckp = s.box(695, 745, 190, 125, "State Checkpointer\nRedis / Postgres\nthread_id per incident\n(pause · resume)", "green")
rtr = s.box(895, 745, 190, 125, "Model Router\n(LiteLLM gateway)\nsmall: triage/classify\nlarge: RCA/planning\n→ Azure OpenAI / Bedrock", "yellow", fs=13)
cache = s.box(1105, 745, 180, 125, "Semantic Cache\nRedis + embeddings\n(similar alerts / RCA)", "green")
vdb = s.box(1300, 745, 180, 125, "Vector DB\n(pgvector)\npast incidents ·\nrunbooks · postmortems", "green", fs=13)

# governance
aud = s.box(1560, 745, 220, 130, "Immutable Audit Log\nS3 Object Lock (WORM)\nhash-chained entries:\nwho · what · args · result", "violet")

# observability
otel = s.box(80, 985, 320, 100, "OpenTelemetry Collector\nspans: request → agent → LLM → tool\nGenAI semantic conventions", "teal")
trc = s.box(460, 985, 300, 100, "Grafana Tempo / LangSmith\ndistributed traces · run replays", "teal")
met = s.box(820, 985, 380, 100, "Prometheus + Grafana dashboards\nMTTR · latency/task · tokens/min\ncache hit % · tool failure % · HITL wait", "teal")
fin = s.box(1260, 985, 240, 100, "FinOps Dashboard\ntoken $ per incident\nrouting & cache savings", "teal")
siem = s.box(1560, 985, 220, 100, "SIEM\n(Microsoft Sentinel)\nsecurity alerts", "teal")

A = s.arrow
# ingest
A([c(prom, "r"), (340, 190)], prom, gw)
A([c(dd, "r"), (340, 212)], dd, gw)
A([c(am, "r"), (340, 228)], am, gw)
A([c(cw, "r"), (340, 242)], cw, gw)
A([c(gw, "b"), c(guard, "t")], gw, guard)
A([c(guard, "b"), c(norm, "t")], guard, norm)
A([c(norm, "r"), (620, 505), (620, 185), c(sup, "l")], norm, sup)
# supervisor -> agents
for ag in (tri, pla, exe):
    A([(c(ag, "t")[0], 230), c(ag, "t")], sup, ag)
A([c(tri, "r"), c(pla, "l")], tri, pla)
A([c(pla, "b"), (850, 420)], pla, cls)
A([c(cls, "r"), c(hitl, "l")], cls, hitl)
A([(1030, 420), (1030, 380)], hitl, exe)
A([(930, 520), (860, 550)], hitl, st)
# executor -> MCP (through resilience wrapper)
A([c(exe, "r"), (1155, 325), (1155, 175), c(mcp, "l")], exe, mcp, color="#0c8599")
s.text(1084, 331, "via circuit\nbreaker", fs=11, color="#0c8599")
A([(1480, 180), (1560, 180)], mcp, aws, color="#d9480f")
A([(1480, 200), (1520, 200), (1520, 300), (1560, 300)], mcp, az, color="#d9480f")
A([(1480, 425), (1560, 425)], opa, vault, color="#d9480f")
# HITL
A([(1080, 470), (1130, 470), (1130, 592), c(appr, "l")], hitl, appr, color="#e03131")
A([c(appr, "r"), c(sre, "l")], appr, sre, both=True, color="#e03131")
A([(1480, 625), (1515, 625), (1515, 810), c(aud, "l")], appr, aud, color="#6741d9")
# runtime -> LLM/state
A([c(st, "b"), c(ckp, "t")], st, ckp, color="#2f9e44")
A([c(res, "b"), c(rtr, "t")], res, rtr, color="#e67700")
A([c(rtr, "r"), c(cache, "l")], rtr, cache, both=True, color="#e67700")
A([c(res, "r"), (1110, 590), (1110, 685), (1390, 685), c(vdb, "t")], res, vdb, color="#2f9e44",
  label="RAG: similar incidents & runbooks", label_at=(1160, 664), lfs=12)
# telemetry
A([(360, 665), (360, 985)], None, otel, dashed=True, color="#087f5b",
  label="OTel spans from every layer\n(gateway · agents · LLM · MCP)\ntrace_id = incident_id", label_at=(372, 760), lfs=13)
A([c(otel, "r"), c(trc, "l")], otel, trc, color="#087f5b")
A([c(trc, "r"), c(met, "l")], trc, met, color="#087f5b")
A([c(met, "r"), c(fin, "l")], met, fin, color="#087f5b")
A([c(aud, "b"), c(siem, "t")], aud, siem, color="#6741d9")

# legend
lx, ly = 40, 1135
s.text(lx, ly + 2, "Legend:", fs=16)
items = [("blue", "Agent"), ("violet", "Orchestrator / Governance"), ("red", "Guardrail / Security"),
         ("hitl", "HITL gate"), ("green", "State / READ tool"), ("yellow", "LLM / NON-DESTRUCTIVE"),
         ("cyan", "MCP tool plane"), ("teal", "Observability")]
xx = lx + 90
for k, lab in items:
    st_, fi, _ = PAL[k]
    s.els.append(s._base("rectangle", xx, ly, 22, 22, stroke=st_, fill=fi))
    s.text(xx + 30, ly + 2, lab, fs=14)
    xx += 40 + text_w(lab, 14) + 24
s.arrow([(xx, ly + 11), (xx + 50, ly + 11)], dashed=True, color="#087f5b")
s.text(xx + 58, ly + 2, "telemetry", fs=14)
s.save("01-cloud-architecture.excalidraw")

# =====================================================================
# Diagram 2: Agent interaction workflow (sequence)
# =====================================================================
s = Scene()
s.text(40, 24, "Pod 2: Agent Interaction Workflow (Hierarchical Supervisor · MCP · HITL Gate)", fs=28)
s.text(40, 64, "P1 incident lifecycle: triage → plan → guardrail → HITL gate → execute → verify.   💾 = state persistence point   🔒 = human approval boundary", fs=16, color="#495057")

lanes = [("Alertmanager\n(Prometheus)", "gray"), ("Supervisor\n(LangGraph)", "violet"),
         ("Triage &\nDiagnosis Agent", "blue"), ("Remediation\nPlanner Agent", "blue"),
         ("Guardrails +\nRisk Classifier", "red"), ("HITL Gate\nNode", "hitl"), ("SRE On-Call 👤", "orange"),
         ("Execution &\nVerification Agent", "blue"), ("MCP Server\n+ OPA", "cyan"),
         ("Checkpointer\n+ Audit Log", "green")]
X = [120 + i * 180 for i in range(len(lanes))]
TOP = 110
for (name, k), x in zip(lanes, X):
    s.box(x - 80, TOP, 160, 60, name, k)

steps = [
    ("msg", 0, 1, "① P1 alert · HTTPS webhook"),
    ("msg", 1, 9, "② open incident thread_id · initial checkpoint 💾"),
    ("msg", 1, 2, "③ delegate: diagnose"),
    ("msg", 2, 8, "④ fetch_k8s_logs() · MCP · READ → auto"),
    ("ret", 8, 2, "logs · events · metrics"),
    ("msg", 2, 1, "⑤ RCA + confidence"),
    ("msg", 1, 3, "⑥ delegate: plan"),
    ("msg", 3, 4, "⑦ proposed action"),
    ("note", 4, "L3 guardrail: block rm -rf, sudo, chmod 777, curl | sh,\nexfil tools (curl/nc/scp), privilege escalation → re-plan", "red"),
    ("fstart", 3, 9, "ALT [A]: read-only / non-destructive (e.g., clear pod cache) AND RCA confidence C ≥ 0.85: autonomous", "green"),
    ("msg", 4, 7, "⑧a auto-approved (no human in loop)"),
    ("msg", 7, 8, "⑨a restart_service() · MCP"),
    ("fend",),
    ("fstart", 3, 9, "ALT [B] 🔒 destructive write · failover · config patch, OR C < 0.85: SRE approval REQUIRED", "hitl"),
    ("msg", 4, 5, "⑧b approval required"),
    ("msg", 5, 9, "⑨b checkpoint + interrupt(): graph PAUSED 💾"),
    ("msg", 5, 6, "⑩ approval card (scrubbed, L4)"),
    ("ret", 6, 5, "⑪ APPROVE / REJECT / MODIFY"),
    ("msg", 5, 9, "⑫ audit: approver · decision · ts · hash 💾"),
    ("msg", 5, 7, "⑬ resume(Command) from checkpoint"),
    ("msg", 7, 8, "⑭ apply_hotfix() · DESTRUCTIVE"),
    ("fend",),
    ("note", 8, "OPA policy check + Vault short-lived creds\n→ execute on EKS / AKS (dry-run first)", "cyan"),
    ("ret", 8, 7, "execution result"),
    ("msg", 7, 8, "⑮ verify: health probes / logs"),
    ("msg", 7, 1, "⑯ verification: healthy ✅ / degraded ❌"),
    ("note", 1, "❌ → circuit breaker: rollback, retry ≤ 2,\nthen escalate to on-call (PagerDuty)", "yellow"),
    ("msg", 1, 9, "⑰ close incident · audit trail · MTTR & token metrics 💾"),
]

y = TOP + 60 + 50
frame = None
for stp in steps:
    kind = stp[0]
    if kind in ("msg", "ret"):
        _, a, b, lab = stp
        dashed = kind == "ret"
        col = "#343a40" if not dashed else "#868e96"
        if 8 in (a, b) and kind == "msg":
            col = "#0c8599"
        if 5 in (a, b) or 6 in (a, b):
            col = "#e03131"
        s.arrow([(X[a], y), (X[b], y)], dashed=dashed, color=col)
        lw = text_w(lab, 13)
        lx = min(X[a], X[b]) + 10 if abs(X[a] - X[b]) < lw + 20 else (X[a] + X[b]) / 2 - lw / 2
        s.text(lx, y - 20, lab, fs=13, color=col)
        y += 50
    elif kind == "note":
        _, ln, lab, k = stp
        w = text_w(lab, 13) + 24
        s.box(X[ln] - w / 2, y - 18, w, 50, lab, k, fs=13)
        y += 66
    elif kind == "fstart":
        _, l1, l2, lab, k = stp
        frame = (X[l1] - 100, y - 30, X[l2] + 100, lab, k)
        y += 30
    elif kind == "fend":
        x1, y1, x2, lab, k = frame
        stroke, _, zfill = PAL[k]
        z = s._base("rectangle", x1, y1, x2 - x1, y - y1 - 20, stroke=stroke, fill=zfill,
                    strokeStyle="dashed", roundness={"type": 3}, strokeWidth=3 if k == "hitl" else 2)
        s.back.append(z)
        s.text(x1 + 12, y1 + 6, lab, fs=15, color=stroke, back=True)
        y += 20

END = y + 10
for x in X:  # lifelines behind everything
    ln = s._base("line", x, TOP + 60, 0, END - TOP - 60, stroke="#adb5bd", strokeStyle="dashed",
                 points=[[0, 0], [0, END - TOP - 60]], lastCommittedPoint=None, startBinding=None,
                 endBinding=None, startArrowhead=None, endArrowhead=None, strokeWidth=1)
    s.back.insert(0, ln)

s.text(40, END + 20, "Protocols:  HTTPS webhook (ingest)  ·  in-process LangGraph edges (agent ↔ agent, hierarchical)  ·  "
       "MCP JSON-RPC over Streamable HTTP + OAuth (agent ↔ tools)  ·  W3C traceparent propagated on every hop (OTel)", fs=14, color="#495057")
s.text(40, END + 44, "Topology: hierarchical. Supervisor owns routing and budgets; workers never call each other directly, "
       "and only the Execution agent holds write-capable MCP tools (least privilege).", fs=14, color="#495057")
s.save("02-agent-workflow-sequence.excalidraw")
