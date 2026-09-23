"""End-to-end demo run against a RUNNING server (real LLM), exactly as the dashboard uses it.

    uvicorn app.main:app --port 8000 &          # DEMO_MODE=true, SEND_MODE=simulated
    python scripts/e2e_demo.py http://127.0.0.1:8000

Loads the demo emails, processes all of them with the real model, then as admin "Demo
Admin": approves + sends two replies, tries to send one unapproved email (must fail),
rejects the spam, and prints the final state. Nothing leaves the machine (simulated).
"""

from __future__ import annotations

import sys
import time

import httpx

H = {"X-Admin-Name": "Demo Admin"}


def main(base: str) -> int:
    c = httpx.Client(base_url=base, headers=H, timeout=600)
    health = c.get("/health").json()
    print(f"server: demo={health['demo_mode']} send={health['send_mode']} "
          f"model={health['llm_model']}")
    if health["send_mode"] != "simulated":
        print("refusing: this demo only runs with SEND_MODE=simulated")
        return 2
    items = c.post("/demo/emails", json={}).json()
    ids = {i["sample_id"]: i["email_id"] for i in items}
    print(f"\n1) loaded {len(ids)} demo emails")

    print("\n2) processing each email with the real model")
    print(f"   {'sample':26} {'status':13} {'category':17} {'draft':6} {'sec':>5}  review reasons")
    for sid, eid in ids.items():
        t0 = time.monotonic()
        r = c.post(f"/emails/{eid}/process")
        d = r.json() if r.status_code == 200 else c.get(f"/emails/{eid}").json()
        err = "" if r.status_code == 200 else f" [HTTP {r.status_code} {r.json().get('error')}]"
        print(f"   {sid:26} {d['status']:13} {str(d['category']):17} "
              f"{'v' + str(d['draft']['version']) if d['draft'] else '-':6} "
              f"{time.monotonic() - t0:5.0f}  {len(d['review_reasons'])}{err}")

    print("\n3) sending WITHOUT approval (must be refused)")
    r = c.post(f"/emails/{ids['quotation_pdf']}/send")
    print(f"   quotation_pdf -> HTTP {r.status_code} {r.json().get('error')}")

    print("\n4) admin approves + sends two replies")
    for sid in ("requirement_lab_pcs", "invoice_network"):
        d = c.get(f"/emails/{ids[sid]}").json()
        if not d["draft"]:
            print(f"   {sid}: no draft ({d['status']}), skipped")
            continue
        print(f"   --- {sid} draft v{d['draft']['version']} "
              f"(warnings: {len(d['draft']['warnings'])}) ---")
        for line in d["draft"]["body"].splitlines():
            print(f"   | {line}")
        a = c.post(f"/emails/{ids[sid]}/approve",
                   json={"draft_id": d["draft"]["id"], "acknowledge_warnings": True})
        s = c.post(f"/emails/{ids[sid]}/send")
        print(f"   approve -> {a.status_code}, send -> {s.status_code} {s.json()}")

    print("\n5) admin rejects the spam newsletter")
    r = c.post(f"/emails/{ids['spam_newsletter']}/reject", json={"reason": "marketing"})
    print(f"   -> {r.status_code} {r.json()['status']}")

    print("\n6) injection email: what the admin sees")
    d = c.get(f"/emails/{ids['prompt_injection_vendor']}").json()
    print(f"   status={d['status']} category={d['category']} draft={bool(d['draft'])}")
    for reason in d["review_reasons"]:
        print(f"   - {reason}")

    stats = c.get("/dashboard/stats").json()
    print(f"\n7) final: { {k: v for k, v in stats['by_status'].items() if v} }  "
          f"sent={stats['sent']}")
    audit = c.get(f"/emails/{ids['invoice_network']}/audit").json()
    print("   invoice audit trail: " + " > ".join(a["event_type"] for a in audit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
