#!/usr/bin/env python3
"""Fetch Sleep Tech iOS metrics via the App Store Connect Analytics Reports API.

Runs inside GitHub Actions. Maintains an ONGOING analytics report request,
downloads daily instances of the Downloads / Discovery & Engagement reports,
and writes ios_metrics.json. On any failure writes last_error.txt (no secrets)
and exits 0 so the workflow can commit the error for remote debugging.

Env: ASC_PRIVATE_KEY, ASC_KEY_ID, ASC_ISSUER_ID, APP_ID
"""
import csv
import gzip
import io
import json
import os
import time
import traceback
import urllib.request

API = "https://api.appstoreconnect.apple.com"
APP_ID = os.environ.get("APP_ID", "6757299435")
NOTES = []

def make_token():
    import jwt
    now = int(time.time())
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 900,
         "aud": "appstoreconnect-v1"},
        os.environ["ASC_PRIVATE_KEY"],
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"},
    )

def api(path, method="GET", body=None):
    url = path if path.startswith("http") else API + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + make_token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}") from None

def get_all(path):
    items, url = [], path
    while url:
        j = api(url)
        items += j.get("data", [])
        url = j.get("links", {}).get("next")
    return items

def download_segment(url):
    """Segment URLs are pre-signed; no auth header."""
    with urllib.request.urlopen(url, timeout=120) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")

def parse_csv(text):
    """Apple analytics CSVs may be comma or tab delimited; sniff the header."""
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") >= first.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))

def ensure_request():
    reqs = get_all(f"/v1/apps/{APP_ID}/analyticsReportRequests")
    ongoing = [r for r in reqs
               if r["attributes"].get("accessType") == "ONGOING"
               and not r["attributes"].get("stoppedDueToInactivity")]
    if ongoing:
        return ongoing[0]["id"], False
    body = {"data": {"type": "analyticsReportRequests",
                     "attributes": {"accessType": "ONGOING"},
                     "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}}
    j = api("/v1/analyticsReportRequests", "POST", body)
    return j["data"]["id"], True

def collect(report_id, name, daily, keymap, raw_dir):
    """Download recent daily instances of one report and fold into `daily`."""
    instances = get_all(
        f"/v1/analyticsReports/{report_id}/instances?filter[granularity]=DAILY")
    instances.sort(key=lambda i: i["attributes"].get("processingDate", ""), reverse=True)
    saved_raw = False
    for inst in instances[:60]:
        segs = get_all(f"/v1/analyticsReportInstances/{inst['id']}/segments")
        for seg in segs:
            text = download_segment(seg["attributes"]["url"])
            rows = parse_csv(text)
            if not saved_raw and rows:
                os.makedirs(raw_dir, exist_ok=True)
                safe = "".join(c if c.isalnum() else "_" for c in name)[:60]
                with open(f"{raw_dir}/{safe}.sample.csv", "w") as f:
                    f.write(text[:20000])
                saved_raw = True
            for row in rows:
                date = row.get("Date") or row.get("date")
                if not date:
                    continue
                try:
                    n = int(float(row.get("Counts") or row.get("counts") or 0))
                except ValueError:
                    continue
                bucket = daily.setdefault(date, {})
                for metric, match in keymap.items():
                    if match(row):
                        bucket[metric] = bucket.get(metric, 0) + n

def main():
    out = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "status": "ok", "notes": NOTES, "reports": [], "daily": {}}

    req_id, created = ensure_request()
    if created:
        NOTES.append("ONGOING analytics report request created; Apple may take up to "
                     "48h to generate the first instances.")

    reports = get_all(f"/v1/analyticsReportRequests/{req_id}/reports")
    by_name = {r["attributes"]["name"]: r["id"] for r in reports}
    out["reports"] = sorted(by_name)

    def pick(*subs):
        # prefer Standard over Detailed to keep files small
        cands = [n for n in by_name if all(s.lower() in n.lower() for s in subs)]
        cands.sort(key=lambda n: ("standard" not in n.lower(), len(n)))
        return cands[0] if cands else None

    daily = {}
    dl = pick("download")
    if dl:
        collect(by_name[dl], dl, daily,
                {"totalDownloads": lambda r: True}, "data/raw")
    else:
        NOTES.append("No Downloads report available yet.")

    eng = pick("discovery", "engagement")
    if eng:
        def is_event(*names):
            names = [n.lower() for n in names]
            return lambda r: any((r.get("Event") or r.get("event") or "").lower() == n
                                 for n in names)
        collect(by_name[eng], eng, daily,
                {"impressionsTotal": is_event("impression", "impressions"),
                 "pageViewCount": is_event("page view", "pageview", "page views")},
                "data/raw")
    else:
        NOTES.append("No Discovery and Engagement report available yet.")

    if not daily:
        out["status"] = "pending"
    out["daily"] = dict(sorted(daily.items()))
    with open("ios_metrics.json", "w") as f:
        json.dump(out, f, indent=1)
    if os.path.exists("last_error.txt"):
        os.remove("last_error.txt")
    print(f"status={out['status']} reports={len(out['reports'])} days={len(daily)}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open("last_error.txt", "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ\n", time.gmtime()))
            f.write(traceback.format_exc())
        print("FAILED — wrote last_error.txt")
