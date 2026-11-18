"""Photos: Wizard's front for Immich (https://immich.app, AGPL-3.0), run unmodified next to this front.

- The Wizard page lives at /photos/ (and its files under /photos/). Immich's own web app keeps every other path,
  including /photos (its timeline) and /photos/<asset-uuid> (its viewer), so the app's own port is plain Immich.
- No sign-in: reaching this box over Tailscale is the only access control. On first start this front creates Immich's
  one account itself (a placeholder email and a random password kept only in the private state volume, never shown),
  and every request it passes to Immich without its own credentials gets that account's session added. The Immich
  phone app's sign-in (password or the "Continue" button) is answered here with a real session for the same account.
- The agent's tools at /mcp talk to Immich's documented REST API (https://api.immich.app) with a narrow API key this
  front makes for itself on first start. Secrets never leave the private state volume and no route returns them.
- Google Takeout import uses immich-go (https://github.com/simulot/immich-go, AGPL-3.0), downloaded on the
  owner's first import and checked against its pinned release checksum.
"""
import hashlib
import hmac
import http.client as httpclient  # bound as httpclient only: the front's own http() helper keeps its name
import io
import calendar
import datetime
import json
import re
import secrets
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse

STATE = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or "/state"
IMPORT_DIR = os.environ.get("IMPORT_DIR", "/import")
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", "/library")
APP_PORT = os.environ.get("APP_PORT", "")
PAGE = "/photos/"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[0-9:.]+Z?)?\Z")
KEY_NAME = "Wizard agent (Photos app)"
# What the agent tools and immich-go need (immich-go docs/installation.md "API Permissions"), nothing more.
KEY_PERMISSIONS = [
    "asset.read", "asset.statistics", "asset.update", "asset.upload", "asset.copy", "asset.delete", "asset.download",
    "asset.view", "album.create", "album.read", "albumAsset.create", "server.about", "server.statistics",
    "server.storage", "stack.create", "tag.asset", "tag.create", "tag.read", "user.read", "job.create", "job.read",
    "person.read", "person.statistics", "timeline.read",
]
IMMICH_GO = {
    "version": "v0.32.0",
    "amd64": ("https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz",
              "6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97"),
    "arm64": ("https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_arm64.tar.gz",
              "2c35d9284baae407ef9540bdac5f488971b0bdc7be758a4d7c05ab270af09fdb"),
}
IMPORT_DEFAULTS = {"include_archived": True, "include_partner": True, "sync_albums": True, "include_trashed": False}
PRINT_REVIEW_FILE = "grandparent-print-reviews.json"
PRINT_REVIEW_SWITCH = "grandparent-print-review"
LOCK = threading.Lock()
RUN = {"proc": None}


# ---------------- private state (0700 dir, 0600 files) ----------------

def _state_path(name):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE, 0o700)
    except OSError:
        pass
    return os.path.join(STATE, name)


def _read_json(name, default=None):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    p = _state_path(name)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


# ---------------- Immich API ----------------

def _immich(method, path, payload=None, key=None, cookie=None, timeout=30, raw=False, token=None, extra=None):
    """Call Immich's REST API. Returns (status, parsed JSON or bytes). Never raises for HTTP errors."""
    headers = dict(extra or {}, Accept="application/json")
    if key:
        headers["x-api-key"] = key
    if token:
        headers["x-immich-user-token"] = token
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        code, body = http(method, "/api" + path, data, headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except OSError as e:
        return 0, {"error": "Immich is not reachable yet (%s)" % type(e).__name__}
    if raw:
        return code, body
    try:
        return code, json.loads(body) if body else {}
    except ValueError:
        return code, {"raw": body[:200].decode("utf-8", "replace")}


def _key():
    return (_read_json("agent-key.json") or {}).get("secret") or ""


def _api(method, path, payload=None, timeout=60):
    key = _key()
    if not key:
        raise RuntimeError("Photos is still setting itself up (Immich starting, or the agent key not made yet). "
                           "Try again in a minute; photos_status shows progress.")
    code, body = _immich(method, path, payload, key=key, timeout=timeout)
    if code == 401 and _ensure_agent_key(force=True):
        code, body = _immich(method, path, payload, key=_key(), timeout=timeout)
    if code == 401:
        raise RuntimeError("Immich does not accept the agent's key and Photos could not make a new one yet. "
                           "Try again shortly; photos_status shows progress.")
    if code == 0:
        raise RuntimeError(body.get("error"))
    if code >= 400:
        raise RuntimeError("Immich answered %d: %s" % (code, (body.get("message") if isinstance(body, dict) else body)))
    return body


def _own_url(headers=None):
    host = ""
    for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(","):
        name = h.strip().rsplit(":", 1)[0]
        if name.endswith(".ts.net") or ("." in name and not name.replace(".", "").isdigit()):
            host = name
            break
    if not host and headers:
        host = (headers.get("host") or "").rsplit(":", 1)[0]
    if not host or not APP_PORT:
        return ""
    return "https://%s:%s" % (host, APP_PORT)


# ---------------- status ----------------

def _du(path):
    try:
        st = os.statvfs(path)
        return {"free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1), "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)}
    except OSError:
        return None


def status(headers=None):
    out = {"app": APP, "version": VERSION, "immich": {"reachable": False}, "agent_connected": False}
    code, ping = _immich("GET", "/server/ping", timeout=5)
    if code == 200:
        out["immich"]["reachable"] = True
        _, ver = _immich("GET", "/server/version", timeout=5)
        if isinstance(ver, dict) and "major" in ver:
            out["immich"]["version"] = "%s.%s.%s" % (ver["major"], ver["minor"], ver["patch"])
        _, cfg = _immich("GET", "/server/config", timeout=5)
        out["immich"]["has_owner_account"] = bool(cfg.get("isInitialized"))
        out["immich"]["trash_days"] = cfg.get("trashDays")
    owner = _owner()
    out["sign_in"] = {"mode": "none: anyone who reaches this box over Tailscale (the owner's tailnet) is the owner",
                      "managed_account": bool(owner.get("password")), "ready": bool(AUTH.get("token")),
                      **({"problem": AUTH["error"]} if AUTH.get("error") else {})}
    key = _key()
    if key and out["immich"]["reachable"]:
        c, me = _immich("GET", "/users/me", key=key, timeout=10)
        out["agent_connected"] = c == 200
        if c == 200:
            out["account"] = {"name": me.get("name"), "email": me.get("email"), "is_admin": me.get("isAdmin")}
            c, stats = _immich("GET", "/server/statistics", key=key, timeout=20)
            if c == 200:
                out["library"] = {"photos": stats.get("photos"), "videos": stats.get("videos"),
                                  "size_gb": round((stats.get("usage") or 0) / 1e9, 2)}
            c, jobs = _immich("GET", "/jobs", key=key, timeout=10)
            if c == 200:
                out["background_work"] = {
                    n: {"waiting": (jobs.get(n) or {}).get("jobCounts", {}).get("waiting", 0),
                        "active": (jobs.get(n) or {}).get("jobCounts", {}).get("active", 0),
                        "paused": (jobs.get(n) or {}).get("queueStatus", {}).get("isPaused", False)}
                    for n in ("metadataExtraction", "thumbnailGeneration", "faceDetection", "facialRecognition",
                              "smartSearch", "duplicateDetection", "videoConversion")}
    out["disk"] = _du(LIBRARY_DIR) or _du(STATE)
    out["import"] = import_summary()
    out["features"] = {"grandparent_print_review": switch_on(PRINT_REVIEW_SWITCH)}
    own = _own_url(headers)
    out["links"] = {"wizard": "?open=photos", **({"immich": own + "/", "mobile_server_url": own} if own else {})}
    if not out["immich"]["reachable"]:
        out["next"] = "Immich is starting (the first start takes a minute or two). Try again shortly."
    elif not out["immich"]["has_owner_account"] or not out["agent_connected"]:
        out["next"] = ("Photos is setting itself up (it makes its own account; no sign-in for the owner). Check again "
                       "in a minute." if not AUTH.get("error") else "Setup problem: " + AUTH["error"])
    return out


# ---------------- people and search ----------------

def _person_ids(person):
    if not person:
        return []
    wanted = [p.strip() for p in str(person).split(",") if p.strip()]
    ids, everyone = [], None
    for w in wanted:
        if UUID_RE.match(w):
            ids.append(w)
            continue
        if everyone is None:
            everyone = _all_people()
        hits = [p for p in everyone if (p.get("name") or "").strip().lower() == w.lower()]
        if not hits:
            hits = [p for p in everyone if w.lower() in (p.get("name") or "").lower()]
        if len(hits) != 1:
            names = sorted({p.get("name") for p in hits if p.get("name")})[:10]
            raise ValueError("No single person named %r (%s). Use photos_people to get the exact id." %
                             (w, "matches: " + ", ".join(names) if names else "no match"))
        ids.append(hits[0]["id"])
    return ids


def _all_people(with_hidden=False):
    people, page = [], 1
    while page <= 50:
        q = urllib.parse.urlencode({"page": page, "size": 500, "withHidden": "true" if with_hidden else "false"})
        body = _api("GET", "/people?" + q)
        people += body.get("people") or []
        if not body.get("hasNextPage"):
            break
        page += 1
    return people


def people(name="", include_unnamed=False, limit=50):
    limit = max(1, min(int(limit or 50), 500))
    everyone = _all_people()
    rows = [p for p in everyone if include_unnamed or (p.get("name") or "").strip()]
    if name:
        rows = [p for p in rows if name.lower() in (p.get("name") or "").lower()]
    out = []
    for i, p in enumerate(rows[:limit]):
        row = {"id": p["id"], "name": p.get("name") or "(unnamed)", "favorite": p.get("isFavorite", False)}
        if i < 40:
            try:
                row["photos"] = _api("GET", "/people/%s/statistics" % p["id"]).get("assets")
            except RuntimeError:
                pass
        out.append(row)
    unnamed = sum(1 for p in everyone if not (p.get("name") or "").strip())
    return {"people": out, "shown": len(out), "named_total": len(everyone) - unnamed, "unnamed_faces_groups": unnamed,
            "note": "Immich groups faces automatically after import. Name a face group in Immich (People) and it "
                    "shows here by name."}


def _filters(person="", taken_after="", taken_before="", media_type="", city="", favorite=None, album_id=""):
    f = {}
    ids = _person_ids(person)
    if ids:
        f["personIds"] = ids
    for k, v in (("takenAfter", taken_after), ("takenBefore", taken_before)):
        if v:
            if not DATE_RE.match(str(v)):
                raise ValueError("%s must be a date like 2019-06-30" % k)
            f[k] = v if "T" in v else v + ("T00:00:00.000Z" if k == "takenAfter" else "T23:59:59.999Z")
    if media_type:
        t = str(media_type).upper()
        if t not in ("IMAGE", "VIDEO"):
            raise ValueError("media_type is IMAGE or VIDEO")
        f["type"] = t
    if city:
        f["city"] = city
    if favorite is not None:
        f["isFavorite"] = bool(favorite)
    if album_id:
        if not UUID_RE.match(album_id):
            raise ValueError("album_id must be an Immich album id")
        f["albumIds"] = [album_id]
    return f


def _asset_row(a, own):
    ex = a.get("exifInfo") or {}
    row = {"id": a["id"], "file": a.get("originalFileName"), "type": a.get("type"),
           "taken": a.get("localDateTime") or a.get("fileCreatedAt"),
           "people": [p.get("name") for p in (a.get("people") or []) if p.get("name")],
           "place": ", ".join(x for x in (ex.get("city"), ex.get("country")) if x) or None}
    if own:
        row["open"] = "%s/photos/%s" % (own, a["id"])
    return row


def search(query="", person="", taken_after="", taken_before="", media_type="", city="", favorite=None, limit=25):
    limit = max(1, min(int(limit or 25), 200))
    f = _filters(person, taken_after, taken_before, media_type, city, favorite)
    own = _own_url()
    if query:
        body = _api("POST", "/search/smart", dict(f, query=str(query)[:500], size=limit, withExif=True))
        items = (body.get("assets") or {}).get("items") or []
        return {"mode": "smart (meaning-based, best matches first)", "shown": len(items),
                "assets": [_asset_row(a, own) for a in items]}
    total = _api("POST", "/search/statistics", f).get("total")
    body = _api("POST", "/search/metadata", dict(f, size=limit, withExif=True, withPeople=True, order="desc"))
    items = (body.get("assets") or {}).get("items") or []
    return {"mode": "filters", "total_matching": total, "shown": len(items), "assets": [_asset_row(a, own) for a in items]}


# ---------------- monthly grandparent print review ----------------

def _review_month(month=""):
    if month is not None and not isinstance(month, str):
        raise ValueError("month must look like 2026-10")
    if not month:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        month = (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}\Z", str(month)):
        raise ValueError("month must look like 2026-10")
    year, number = (int(x) for x in str(month).split("-"))
    if year < 1970 or number < 1 or number > 12:
        raise ValueError("month must look like 2026-10")
    return "%04d-%02d" % (year, number)


def _review_dates(month):
    year, number = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, number)[1]
    return "%s-01" % month, "%s-%02d" % (month, last)


def _review_label(month):
    return datetime.date(int(month[:4]), int(month[5:]), 1).strftime("%B %Y")


def _review_store():
    data = _read_json(PRINT_REVIEW_FILE, {}) or {}
    return data if isinstance(data, dict) else {}


def _review_write(data):
    _write_json(PRINT_REVIEW_FILE, data)


def _asset_dimensions(asset):
    exif = asset.get("exifInfo") or {}
    width = asset.get("width") or exif.get("exifImageWidth") or exif.get("imageWidth") or 0
    height = asset.get("height") or exif.get("exifImageHeight") or exif.get("imageHeight") or 0
    try:
        return int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return 0, 0


def _review_asset_row(asset, month, selected=False):
    row = _asset_row(asset, _own_url())
    width, height = _asset_dimensions(asset)
    row.update(selected=bool(selected), favorite=bool(asset.get("isFavorite")), width=width or None,
               height=height or None, thumbnail="api/print-review/thumbnail/%s?month=%s" %
               (asset["id"], urllib.parse.quote(month)))
    return row


def _review_assets(ids):
    out = []
    for asset_id in ids:
        if not UUID_RE.match(str(asset_id)):
            raise ValueError("not an Immich asset id: %s" % asset_id)
        asset = _api("GET", "/assets/%s" % asset_id)
        if asset.get("type") != "IMAGE":
            raise ValueError("Grandparent print sets can contain photos, not videos.")
        out.append(asset)
    return out


def _review_candidates(month, cap=5000):
    start, end = _review_dates(month)
    filters = _filters(taken_after=start, taken_before=end, media_type="IMAGE")
    try:
        total = int(_api("POST", "/search/statistics", filters).get("total") or 0)
    except RuntimeError:
        total = 0
    items, page = [], 1
    while len(items) < cap:
        body = _api("POST", "/search/metadata", dict(filters, size=min(1000, cap - len(items)), page=page,
                                                       withExif=True, withPeople=True, order="desc"), timeout=120)
        assets = body.get("assets") or {}
        items += assets.get("items") or []
        if not assets.get("nextPage"):
            break
        page = int(assets["nextPage"])

    # Immich's semantic rank is useful for finding people and occasions. It is only a boost: favorites,
    # faces, resolution and date variety still work before smart search has finished indexing.
    smart_rank = {}
    if items:
        try:
            body = _api("POST", "/search/smart", dict(filters, query="family people portrait celebration pets",
                                                       size=min(300, len(items)), withExif=True), timeout=120)
            for i, asset in enumerate((body.get("assets") or {}).get("items") or []):
                smart_rank[asset["id"]] = max(1, 40 - i / 10)
        except RuntimeError:
            pass

    scored = []
    for asset in items:
        width, height = _asset_dimensions(asset)
        megapixels = width * height / 1000000 if width and height else 0
        people = asset.get("people") or []
        named = [p for p in people if (p.get("name") or "").strip()]
        score = smart_rank.get(asset["id"], 0)
        score += 100 if asset.get("isFavorite") else 0
        score += 22 if people else 0
        score += min(len(people), 4) * 3 + min(len(named), 4) * 2
        score += min(megapixels, 12)
        score += 2 if (asset.get("exifInfo") or {}).get("city") else 0
        scored.append((score, asset))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("localDateTime") or pair[1].get("fileCreatedAt") or "")))
    ranked = [asset for _, asset in scored]
    return ranked, max(total, len(ranked))


def _varied_ids(assets, count):
    chosen, seen, per_day = [], set(), {}
    # First pass gives different days a chance. The second fills from the strongest remaining moments.
    for limit_per_day in (1, 2, 1000):
        for asset in assets:
            asset_id = asset["id"]
            if asset_id in seen:
                continue
            day = str(asset.get("localDateTime") or asset.get("fileCreatedAt") or "unknown")[:10]
            if per_day.get(day, 0) >= limit_per_day:
                continue
            chosen.append(asset_id)
            seen.add(asset_id)
            per_day[day] = per_day.get(day, 0) + 1
            if len(chosen) >= count:
                return chosen
    return chosen


def _review_revision(record):
    reviewed = {"asset_ids": record.get("asset_ids") or [], "note": record.get("note") or "",
                "updated_at": record.get("updated_at")}
    return hashlib.sha256(json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def print_review_status(month=""):
    month = _review_month(month)
    record = (_review_store().get("reviews") or {}).get(month)
    base = {"month": month, "label": _review_label(month), "purchase": "No print order has been placed."}
    if not record:
        return dict(base, state="not_started", selected_count=0, candidates=[], asset_ids=[], note="",
                    next="Generate a suggested shortlist, then review it before approving a print set.")
    selected = set(record.get("asset_ids") or [])
    candidates, missing = [], []
    for asset_id in record.get("candidate_ids") or []:
        try:
            asset = _review_assets([asset_id])[0]
            candidates.append(_review_asset_row(asset, month, asset_id in selected))
        except RuntimeError:
            missing.append(asset_id)
    available_selected = selected & {x["id"] for x in candidates}
    state = "needs_refresh" if missing else record.get("state")
    if missing:
        next_step = "%d review photo(s) are no longer available. Save the remaining selection or generate a fresh review." % len(missing)
    else:
        next_step = ("This print set is approved, but nothing has been ordered." if state == "approved"
                     else "Adjust the photos and note, then explicitly approve the print set.")
    return {**base, **record, "state": state, "selected_count": len(available_selected), "candidates": candidates,
            "missing_asset_ids": missing, "revision": _review_revision(record), "next": next_step}


def generate_print_review(month="", limit=8):
    if not switch_on(PRINT_REVIEW_SWITCH):
        raise RuntimeError("Monthly print review is not turned on in Try new features.")
    month = _review_month(month)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer from 1 to 20")
    data = _review_store()
    existing = (data.get("reviews") or {}).get(month)
    if existing and existing.get("state") not in ("empty",):
        out = print_review_status(month)
        out["next"] = "A review already exists for this month. It was left unchanged."
        return out
    assets, total = _review_candidates(month)
    candidate_count = min(len(assets), max(12, min(limit * 2, 30)))
    candidate_ids = [a["id"] for a in assets[:candidate_count]]
    selected_ids = _varied_ids(assets[:candidate_count], min(limit, candidate_count))
    now = int(time.time())
    record = {"state": "draft" if selected_ids else "empty", "source_count": total,
              "considered_count": len(assets), "candidate_ids": candidate_ids, "asset_ids": selected_ids, "note": "",
              "generated_at": now, "updated_at": now}
    data.setdefault("reviews", {})[month] = record
    _review_write(data)
    out = print_review_status(month)
    if not selected_ids:
        out["next"] = "No photos were found for this month. Turn on phone backup or import a Google Photos archive first."
    return out


def save_print_review(month, asset_ids, note=""):
    if not switch_on(PRINT_REVIEW_SWITCH):
        raise RuntimeError("Monthly print review is not turned on in Try new features.")
    month = _review_month(month)
    data = _review_store()
    record = (data.get("reviews") or {}).get(month)
    if not record:
        raise ValueError("Generate this month's shortlist first.")
    if not isinstance(asset_ids, list) or any(not isinstance(x, str) for x in asset_ids):
        raise ValueError("asset_ids must be a list of photo ids")
    ids = list(dict.fromkeys(asset_ids))
    if not ids or len(ids) > 20:
        raise ValueError("Choose between 1 and 20 photos.")
    candidates = set(record.get("candidate_ids") or [])
    if any(not UUID_RE.match(x) or x not in candidates for x in ids):
        raise ValueError("Choose photos from this month's review candidates.")
    _review_assets(ids)
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError("The month note must be 500 characters or fewer.")
    record.update(state="draft", asset_ids=ids, note=note.strip(), updated_at=int(time.time()))
    for key in ("approved_at",):
        record.pop(key, None)
    _review_write(data)
    return print_review_status(month)


def approve_print_review(month, expected_count=None, expected_revision="", confirmed=False):
    if not switch_on(PRINT_REVIEW_SWITCH):
        raise RuntimeError("Monthly print review is not turned on in Try new features.")
    month = _review_month(month)
    data = _review_store()
    record = (data.get("reviews") or {}).get(month)
    if not record or not record.get("asset_ids"):
        raise ValueError("Choose and save at least one photo first.")
    if not isinstance(confirmed, bool):
        raise ValueError("confirmed must be true or false")
    count = len(record["asset_ids"])
    revision = _review_revision(record)
    _review_assets(record["asset_ids"])
    if not confirmed:
        return {"preview": True, "month": month, "would_approve": count,
                "note": record.get("note") or "", "revision": revision, "purchase": "No order will be placed.",
                "next": "Get the owner's explicit yes, then call again with confirmed=true, expected_count=%d and this revision." % count}
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count != count:
        raise ValueError("The print set now has %d photos, not %s. Review it again before approving." %
                         (count, expected_count))
    if not isinstance(expected_revision, str) or not hmac.compare_digest(expected_revision, revision):
        raise ValueError("The print set changed after the approval preview. Review it again before approving.")
    record.update(state="approved", approved_at=int(time.time()), updated_at=int(time.time()))
    _review_write(data)
    return print_review_status(month)


# ---------------- trash (never a permanent delete) ----------------

def _matching_ids(f, cap=50000):
    ids, page = [], 1
    while len(ids) < cap:
        body = _api("POST", "/search/metadata", dict(f, size=1000, page=page), timeout=120)
        a = body.get("assets") or {}
        ids += [x["id"] for x in a.get("items") or []]
        if not a.get("nextPage"):
            break
        page = int(a["nextPage"])
    return ids[:cap]


def move_to_trash(asset_ids=None, person="", taken_after="", taken_before="", media_type="", expected_count=None,
                  confirmed=False):
    asset_ids = [str(x) for x in (asset_ids or [])]
    bad = [x for x in asset_ids if not UUID_RE.match(x)]
    if bad:
        raise ValueError("not Immich asset ids: %s" % ", ".join(bad[:5]))
    if asset_ids and (person or taken_after or taken_before or media_type):
        raise ValueError("Give either asset_ids or filters, not both.")
    trash_days = (_immich("GET", "/server/config", timeout=5)[1] or {}).get("trashDays", 30)
    if asset_ids:
        count = len(set(asset_ids))
        ids = sorted(set(asset_ids))
        f = None
    else:
        if not (person or taken_after or taken_before):
            raise ValueError("Say which photos: a person and/or a date range (taken_after/taken_before), or asset_ids. "
                             "This tool never trashes the whole library.")
        f = _filters(person, taken_after, taken_before, media_type)
        count = _api("POST", "/search/statistics", f).get("total") or 0
        ids = None
    if not confirmed:
        sample = []
        if f is not None and count:
            body = _api("POST", "/search/metadata", dict(f, size=5, withPeople=True, withExif=True))
            sample = [_asset_row(a, _own_url()) for a in (body.get("assets") or {}).get("items") or []]
        return {"preview": True, "would_move_to_trash": count, "sample": sample,
                "next": "Tell the owner exactly how many photos/videos this is and get his yes. Then call again with "
                        "confirmed=true and expected_count=%d. They go to Immich's trash (kept %s days, restorable), "
                        "not deleted." % (count, trash_days)}
    if expected_count is None or int(expected_count) != count:
        raise ValueError("The matching set is now %d items, not %s. Preview again and re-confirm with the owner." %
                         (count, expected_count))
    if ids is None:
        ids = _matching_ids(f)
        if len(ids) != count:
            raise ValueError("Found %d ids for %d matches; nothing moved. Try again." % (len(ids), count))
    moved = 0
    for i in range(0, len(ids), 500):
        _api("DELETE", "/assets", {"ids": ids[i:i + 500], "force": False}, timeout=300)
        moved += len(ids[i:i + 500])
    return {"moved_to_trash": moved, "restorable_for_days": trash_days,
            "undo": "photos_restore_from_trash with the same asset ids, or Immich > Trash > Restore."}


def restore_from_trash(asset_ids=None, everything=False):
    asset_ids = [str(x) for x in (asset_ids or []) if UUID_RE.match(str(x))]
    if everything:
        body = _api("POST", "/trash/restore")
        return {"restored": body.get("count"), "scope": "everything in the trash"}
    if not asset_ids:
        raise ValueError("Give asset_ids, or everything=true to restore the whole trash.")
    n = 0
    for i in range(0, len(asset_ids), 500):
        body = _api("POST", "/trash/restore/assets", {"ids": asset_ids[i:i + 500]})
        n += body.get("count") or 0
    return {"restored": n}


# ---------------- Google Takeout import (immich-go) ----------------

def _zips():
    try:
        names = sorted(os.listdir(IMPORT_DIR))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(IMPORT_DIR, n)
        if n.lower().endswith((".zip", ".tgz", ".tar.gz")) and not n.startswith(".") and os.path.isfile(p):
            st = os.stat(p)
            out.append({"name": n, "size_gb": round(st.st_size / 1e9, 2), "modified": int(st.st_mtime)})
    return out


def import_options():
    saved = _read_json("import-options.json", {}) or {}
    return {k: saved.get(k, v) if isinstance(saved.get(k), bool) else v for k, v in IMPORT_DEFAULTS.items()}


def set_import_options(d):
    if not isinstance(d, dict) or set(d) - set(IMPORT_DEFAULTS) or not all(isinstance(v, bool) for v in d.values()):
        raise ValueError("options are booleans: " + ", ".join(IMPORT_DEFAULTS))
    _write_json("import-options.json", dict(import_options(), **d))
    return import_options()


LINE_RE = re.compile(r"(uploaded|server duplicate|local duplicate|error|discarded|already|skipped|album)", re.I)


def _log_tail(path, n=12):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    hide = [x for x in (_key(), _owner().get("password"), _owner().get("token"), AUTH.get("token")) if x]
    out = []
    for l in lines[-n:]:
        for x in hide:
            l = l.replace(x, "***")
        out.append(l[:300])
    return out


def import_summary():
    st = _read_json("import.json", {}) or {}
    running = bool(RUN["proc"] and RUN["proc"].poll() is None)
    if st.get("state") == "running" and not running:
        st["state"] = "interrupted"
    zips = _zips()
    out = {"state": st.get("state", "idle"), "drop_folder": "Files > Photos > Import",
           "waiting_files": len(zips), "waiting_gb": round(sum(z["size_gb"] for z in zips), 2)}
    for k in ("started_at", "finished_at", "exit_code", "files", "message"):
        if k in st:
            out[k] = st[k]
    return out


def import_status():
    st = _read_json("import.json", {}) or {}
    out = import_summary()
    out["files_in_drop_folder"] = _zips()
    out["options"] = import_options()
    if st.get("log"):
        out["log_tail"] = _log_tail(st["log"])
    out["how"] = ("Google Takeout: choose only Google Photos, .zip, 50 GB parts. Put every part in Files > Photos > "
                  "Import (or upload them there from a computer), then start the import. Running it again is safe: "
                  "photos already in Immich are skipped.")
    return out


def _arch():
    m = os.uname().machine
    return "arm64" if m in ("aarch64", "arm64") else "amd64"


def _immich_go():
    url, sha = IMMICH_GO[_arch()]
    bindir = _state_path("bin")
    os.makedirs(bindir, mode=0o700, exist_ok=True)
    exe = os.path.join(bindir, "immich-go-" + IMMICH_GO["version"])
    if os.path.isfile(exe):
        return exe
    with urllib.request.urlopen(url, timeout=300) as r:
        blob = r.read()
    if hashlib.sha256(blob).hexdigest() != sha:
        raise RuntimeError("immich-go download did not match its pinned checksum; not running it")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        member = t.getmember("immich-go")
        data = t.extractfile(member).read()
    tmp = exe + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o700)
    os.replace(tmp, exe)
    return exe


def _run_import(files, opts):
    st = {"state": "running", "started_at": int(time.time()), "files": [z["name"] for z in files]}
    logdir = _state_path("import-logs")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    st["log"] = os.path.join(logdir, time.strftime("%Y%m%d-%H%M%S") + ".log")
    _write_json("import.json", st)
    try:
        exe = _immich_go()
        server = "http://%s:%d" % (UP_HOST, UP_PORT)
        env = {"PATH": "/usr/bin:/bin", "HOME": STATE, "TMPDIR": _state_path("tmp"),
               "IMMICH_GO_UPLOAD_SERVER": server, "IMMICH_GO_UPLOAD_API_KEY": _key(),
               "IMMICH_GO_UPLOAD_ADMIN_API_KEY": _key()}
        os.makedirs(env["TMPDIR"], mode=0o700, exist_ok=True)
        argv = [exe, "upload", "from-google-photos", "--no-ui", "--on-errors=continue", "--concurrent-tasks=4",
                "--log-file=" + st["log"], "--client-timeout=60m",
                "--include-archived=%s" % str(opts["include_archived"]).lower(),
                "--include-partner=%s" % str(opts["include_partner"]).lower(),
                "--sync-albums=%s" % str(opts["sync_albums"]).lower(),
                "--include-trashed=%s" % str(opts["include_trashed"]).lower(),
                "--people-tag=true", "--takeout-tag=true"] + [os.path.join(IMPORT_DIR, z["name"]) for z in files]
        with open(st["log"] + ".out", "ab") as out:
            RUN["proc"] = subprocess.Popen(argv, env=env, cwd=STATE, stdout=out, stderr=subprocess.STDOUT)
            code = RUN["proc"].wait()
        st.update(state="done" if code == 0 else "failed", exit_code=code, finished_at=int(time.time()))
        if code:
            st["message"] = " | ".join(_log_tail(st["log"] + ".out", 3))[:600]
    except Exception as e:
        st.update(state="failed", finished_at=int(time.time()), message="%s: %s" % (type(e).__name__, e))
    finally:
        RUN["proc"] = None
        _write_json("import.json", st)


def import_start(confirmed=False):
    with LOCK:
        if RUN["proc"] and RUN["proc"].poll() is None:
            return {"started": False, "state": "running", "message": "An import is already running."}
        files = _zips()
        if not files:
            return {"started": False, "message": "No Takeout files yet. Put the .zip parts in Files > Photos > Import."}
        if not _key():
            raise RuntimeError("Photos is still setting itself up (no agent key yet). Try again in a minute.")
        if not confirmed:
            return {"started": False, "preview": True, "files": files,
                    "next": "Call again with confirmed=true to import these %d file(s) into Immich." % len(files)}
        opts = import_options()
        threading.Thread(target=_run_import, args=(files, opts), daemon=True).start()
        for _ in range(20):
            if (_read_json("import.json", {}) or {}).get("state") == "running":
                break
            time.sleep(0.1)
    return {"started": True, "files": [z["name"] for z in files], "options": opts,
            "note": "Large Takeouts take hours. Face grouping and search indexing run after the upload."}


def _prepare_import_dir():
    """Let the Files app (uid 1000) drop Takeout files here: group 1000, setgid, group-writable. Never recursive."""
    try:
        st = os.stat(IMPORT_DIR)
        if st.st_gid != 1000:
            os.chown(IMPORT_DIR, -1, 1000)
        if (st.st_mode & 0o7777) != 0o2775:
            os.chmod(IMPORT_DIR, 0o2775)
    except OSError as e:
        print("photos: could not prepare %s (%s)" % (IMPORT_DIR, e), flush=True)


STARTUP.append(_prepare_import_dir)


# ---------------- no sign-in: Photos' own Immich account, added to every request ----------------
# Miguel: "Tailscale will be my one and only access. I don't want to have to log into something. I'm the only user."
# Immich keeps its normal accounts; this front owns the single account and answers for it:
# - first start: admin sign-up with a placeholder email and a random password (owner.json, 0600, never shown),
#   then one login session for browsers and a narrow API key for the agent;
# - every request passed to Immich that brings no credentials of its own gets that session (x-immich-user-token),
#   and browser page loads get Immich's readable "immich_is_authenticated" cookie, so the web app opens signed in;
# - the phone app's sign-in (POST /api/auth/login with anything, or its OAuth "Continue" button, answered here with
#   PKCE checked) returns a real Immich session for that account, so the phone has its own device session.
# Requests from another tailnet login than the owner's, from a host name this box doesn't serve, or cross-site from
# a web page (writes) get nothing added and meet Immich's normal sign-in.

OWNER_EMAIL = "owner@photos.wizard"  # a placeholder, never a person's address; Immich only needs some email
OWNER_NAME = "Owner"
AUTH = {"token": "", "error": ""}
AUTH_LOCK = threading.Lock()
KEY_LOCK = threading.Lock()
PENDING = {}  # phone sign-in starts: nonce -> {"redirect", "state", "challenge", "exp"}
CODES = {}  # one-time phone sign-in codes: code -> {"state", "challenge", "exp"}
CODE_TTL = 300
OWN_CREDENTIALS = ("x-immich-user-token", "x-immich-session-token", "authorization", "x-api-key")
SIGN_IN_PATH = "/wizard-sign-in"
COOKIE_AGE = 400 * 86400


def _owner():
    return _read_json("owner.json", {}) or {}


def _login_upstream(user_agent=""):
    """A real Immich password login for Photos' own account: (status, Immich's LoginResponseDto)."""
    o = _owner()
    if not o.get("password"):
        return 503, {"message": "Photos is still setting itself up. Try again in a minute.", "statusCode": 503}
    return _immich("POST", "/auth/login", {"email": o["email"], "password": o["password"]},
                   extra={"User-Agent": user_agent or "Wizard Photos"}, timeout=30)


def _ensure_agent_key(force=False):
    with KEY_LOCK:
        old = _read_json("agent-key.json") or {}
        if old.get("secret") and not force:
            c, _ = _immich("GET", "/users/me", key=old["secret"], timeout=10)
            if c != 401:
                return c == 200
        tok = AUTH.get("token")
        if not tok:
            return False
        if old.get("id"):
            _immich("DELETE", "/api-keys/%s" % old["id"], token=tok)
        c, made = _immich("POST", "/api-keys", {"name": KEY_NAME, "permissions": KEY_PERMISSIONS}, token=tok)
        if c not in (200, 201) or not made.get("secret"):
            return False
        _write_json("agent-key.json", {"id": (made.get("apiKey") or {}).get("id"), "secret": made["secret"],
                                        "user": _owner().get("email"), "created_at": int(time.time())})
        return True


def _setup_once():
    """Make sure Photos' own account, its session and the agent key exist. True when everything is ready."""
    with AUTH_LOCK:
        code, _ = _immich("GET", "/server/ping", timeout=5)
        if code != 200:
            return False
        _, cfg = _immich("GET", "/server/config", timeout=10)
        o = _owner()
        if not cfg.get("isInitialized"):
            if not o.get("password"):
                o = {"email": OWNER_EMAIL, "password": secrets.token_urlsafe(32), "created_at": int(time.time())}
                _write_json("owner.json", o)  # saved before Immich hears it, so a crash never strands the account
            c, b = _immich("POST", "/auth/admin-sign-up",
                           {"email": o["email"], "password": o["password"], "name": OWNER_NAME}, timeout=30)
            if c not in (200, 201):
                AUTH["error"] = "Immich did not create Photos' account (%s: %s)" % (c, (b or {}).get("message"))
                return False
        elif not o.get("password"):
            AUTH["error"] = ("Immich already has an account that Photos did not make, so Photos cannot open it without "
                             "a sign-in. Reinstalling Photos with its data removed starts fresh.")
            return False
        tok = AUTH.get("token") or o.get("token") or ""
        me = {}
        if tok:
            c, me = _immich("GET", "/users/me", token=tok, timeout=10)
            if c == 401:
                tok = ""
        if not tok:
            c, b = _login_upstream()
            if c not in (200, 201) or not (b or {}).get("accessToken"):
                AUTH["error"] = "Photos could not open its own Immich account (%s)." % c
                return False
            tok = b["accessToken"]
            o["token"] = tok
            _write_json("owner.json", o)
            c, me = _immich("GET", "/users/me", token=tok, timeout=10)
        AUTH["token"] = tok
        if isinstance(me, dict) and me.get("shouldChangePassword") and me.get("id"):
            # Immich asks a new admin to change his password on the first phone sign-in; there is no password to change.
            _immich("PUT", "/admin/users/%s" % me["id"], {"shouldChangePassword": False}, token=tok)
        if not o.get("onboarded"):
            # Skip Immich's first-run tour for the one account; its settings stay in Immich > Administration.
            c1, _ = _immich("POST", "/system-metadata/admin-onboarding", {"isOnboarded": True}, token=tok)
            c2, _ = _immich("PUT", "/users/me/onboarding", {"isOnboarded": True}, token=tok)
            if c1 in (200, 201, 204) and c2 in (200, 201, 204):
                o["onboarded"] = True
                _write_json("owner.json", o)
        ok = _ensure_agent_key()
        AUTH["error"] = "" if ok else "Photos could not make the agent's key yet."
        return ok


def _auth_loop():
    while True:
        try:
            ok = _setup_once()
        except Exception as e:  # keep retrying; the status shows the problem
            ok, AUTH["error"] = False, "%s: %s" % (type(e).__name__, e)
        time.sleep(300 if ok else 10)


STARTUP.append(lambda: threading.Thread(target=_auth_loop, daemon=True).start())


def _hostname(netloc):
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _served_names():
    names = {_hostname(h) for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(",") if h.strip()}
    return names | {"127.0.0.1", "localhost"} if names else set()


def _owner_request(method, headers):
    """Is this request the box owner's own (his tailnet, this box's address, not a cross-site write)?"""
    names = _served_names()
    if names and _hostname(headers.get("host")) not in names:
        return False
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    if owners and login and login not in owners:
        return False
    read = method in ("GET", "HEAD")
    if headers.get("sec-fetch-site") == "cross-site" and not (read and headers.get("sec-fetch-mode") == "navigate"):
        return False
    origin = headers.get("origin")
    if origin and not read:
        oname = urllib.parse.urlsplit(origin).hostname if origin != "null" else None
        if not oname or (names and oname not in names) or (not names and oname != _hostname(headers.get("host"))):
            return False
    return True


def _has_own_credentials(target, headers):
    if any(headers.get(h) for h in OWN_CREDENTIALS) or "immich_access_token=" in (headers.get("cookie") or ""):
        return True
    q = target.partition("?")[2]
    return bool(q) and any(k in ("apiKey", "sessionKey") for k, _ in urllib.parse.parse_qsl(q))


def _add_session(method, target, headers, lines):
    tok = AUTH.get("token")
    if not tok or _has_own_credentials(target, headers) or not _owner_request(method, headers):
        return lines
    return lines[:1] + [l for l in lines[1:] if not l.lower().startswith("x-immich-user-token:")] + [
        "x-immich-user-token: " + tok]


HEAD_REWRITE.append(_add_session)


def _base_url(headers):
    host = (headers.get("host") or "").strip()
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https" if _hostname(host).endswith(".ts.net") else "http"
    return "%s://%s" % (proto, host)


def _b64url_sha256(text):
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).decode().rstrip("=")


def _expire():
    now = time.time()
    for d in (PENDING, CODES):
        for k in [k for k, v in d.items() if v["exp"] < now]:
            d.pop(k, None)


def _sign_in_page(code, text):
    html = ("<!doctype html><meta name=viewport content='width=device-width'><title>Photos</title>"
            "<body style='font:1rem system-ui;padding:1.5rem'><p>%s</p>" % text)
    return code, "text/html; charset=utf-8", html.encode()


def _login_answer(headers):
    c, b = _login_upstream(headers.get("user-agent", ""))
    if c in (200, 201) and (b or {}).get("accessToken"):
        return _json(201, b)
    return _json(503 if c in (0, 503) else 502, {"message": "Photos could not open its account right now. Try again "
                                                            "in a minute.", "statusCode": 503})


def auth_route(method, path, query, headers, body):
    """Immich's sign-in endpoints, answered for Photos' own account (see the section comment)."""
    _expire()
    if path == "/api/auth/login":
        return _login_answer(headers)
    if path == "/api/auth/logout":
        # Nothing to sign out of: the session stays, the web app goes back to the library.
        return _json(200, {"successful": True, "redirectUri": "/photos"})
    if path == "/api/oauth/authorize":
        d = json.loads(body or b"{}") if body else {}
        redirect = str(d.get("redirectUri") or "")
        rp = urllib.parse.urlsplit(redirect)
        if not (rp.scheme == "app.immich" or (rp.scheme in ("http", "https") and
                                              _hostname(rp.netloc) == _hostname(headers.get("host")))):
            return _json(400, {"message": "unsupported redirect", "statusCode": 400})
        if len(PENDING) > 200:
            return _json(429, {"message": "too many sign-ins in progress", "statusCode": 429})
        nonce = secrets.token_urlsafe(24)
        PENDING[nonce] = {"redirect": redirect, "state": str(d.get("state") or ""),
                          "challenge": str(d.get("codeChallenge") or ""), "exp": time.time() + CODE_TTL}
        return _json(201, {"url": _base_url(headers) + SIGN_IN_PATH + "?" + urllib.parse.urlencode({"r": nonce})})
    if path == SIGN_IN_PATH:
        nonce = dict(urllib.parse.parse_qsl(query)).get("r", "")
        p = PENDING.pop(nonce, None)
        if not p:
            return _sign_in_page(400, "This link has expired. Go back to the Immich app and tap Continue again.")
        code = secrets.token_urlsafe(32)
        CODES[code] = {"state": p["state"], "challenge": p["challenge"], "exp": time.time() + CODE_TTL}
        sep = "&" if "?" in p["redirect"] else "?"
        loc = p["redirect"] + sep + urllib.parse.urlencode({"code": code, "state": p["state"]})
        return 302, "text/html; charset=utf-8", b"", {"Location": loc}
    if path == "/api/oauth/callback":
        d = json.loads(body or b"{}") if body else {}
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(str(d.get("url") or "")).query))
        entry = CODES.pop(q.get("code", ""), None)
        if not entry:
            return _json(401, {"message": "This sign-in has expired. Tap Continue again.", "statusCode": 401})
        state = str(d.get("state") or q.get("state") or "")
        if entry["state"] and not hmac.compare_digest(entry["state"], state):
            return _json(401, {"message": "Sign-in state did not match. Tap Continue again.", "statusCode": 401})
        if entry["challenge"] and not hmac.compare_digest(entry["challenge"], _b64url_sha256(str(d.get("codeVerifier") or ""))):
            return _json(401, {"message": "Sign-in check failed. Tap Continue again.", "statusCode": 401})
        return _login_answer(headers)
    return _json(404, {"message": "not found", "statusCode": 404})


def phone_config_route(method, path, query, headers, body):
    """Immich's public features/config for the phone app: one "Continue" button instead of an email/password form."""
    c, b = _immich("GET", path[len("/api"):] + ("?" + query if query else ""), timeout=15)
    if c == 200 and isinstance(b, dict):
        if path.endswith("/features"):
            b.update(oauth=True, oauthAutoLaunch=False, passwordLogin=False)
        else:
            b["oauthButtonText"] = "Continue"
    return _json(c or 502, b)


HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-type", "cache-control",
       "x-content-type-options", "set-cookie", "upgrade", "proxy-connection", "te", "trailer"}


def page_with_cookie_route(method, path, query, headers, body):
    """A browser page load: Immich's own answer plus its readable sign-in marker cookie."""
    target = path + ("?" + query if query else "")
    fwd = {k: headers[k] for k in ("accept", "accept-language", "user-agent") if headers.get(k)}
    fwd["x-immich-user-token"] = AUTH.get("token", "")
    conn = httpclient.HTTPConnection(up_addr(), UP_PORT, timeout=30)
    try:
        conn.request(method, target, headers=fwd)
        r = conn.getresponse()
        data = r.read()
        extra = {k: v for k, v in r.getheaders() if k.lower() not in HOP}
        ctype = r.getheader("content-type") or "text/html; charset=utf-8"
        code = r.status
    finally:
        conn.close()
    secure = "; Secure" if _base_url(headers).startswith("https:") else ""
    extra["Set-Cookie"] = "immich_is_authenticated=true; Path=/; Max-Age=%d; SameSite=Lax%s" % (COOKIE_AGE, secure)
    return code, ctype, data, extra


AUTH_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/oauth/authorize", "/api/oauth/callback"}


def _intercept(method, target, headers):
    path = target.split("?", 1)[0]
    if not (path in AUTH_PATHS or path == SIGN_IN_PATH or path.startswith("/api/server/") or
            not path.startswith(("/api/", "/.well-known/"))):
        return None
    if not _owner_request(method, headers):
        return None
    if path in AUTH_PATHS and method == "POST":
        return auth_route
    if path == SIGN_IN_PATH and method in ("GET", "HEAD"):
        return auth_route
    if method == "GET" and path in ("/api/server/features", "/api/server/config") and not headers.get("sec-fetch-site"):
        return phone_config_route  # the phone app (browsers always send Sec-Fetch-Site)
    if (method in ("GET", "HEAD") and AUTH.get("token") and not path.startswith(("/api/", "/.well-known/"))
            and "immich_is_authenticated=" not in (headers.get("cookie") or "")
            and not _has_own_credentials(target, headers)
            and (headers.get("sec-fetch-dest") in ("document", "iframe") or "text/html" in (headers.get("accept") or ""))):
        return page_with_cookie_route
    return None


INTERCEPTS.append(_intercept)


# ---------------- MCP tools (box-only: the agent through the Wizard hub) ----------------

def _box_only():
    h = REQUEST.get()
    if any(h.get(k) for k in ("tailscale-user-login", "x-forwarded-for", "forwarded", "cf-connecting-ip",
                              "cf-access-jwt-assertion", "x-real-ip")):
        raise PermissionError("the Photos tools are only for apps on this box")


@tool("photos_status", "Photos (Immich) status: whether Immich is up and the owner's account exists, whether the agent "
      "is connected, library size (photos, videos, GB), background work still running (face detection, face "
      "grouping, search indexing), Google Takeout import state, free disk, links, and the owner's next step.")
def photos_status():
    _box_only()
    return status()


@tool("photos_people", "List the people Immich recognised by face (face groups), with how many photos each is in. "
      "Named people by default; include_unnamed=true also lists face groups the owner has not named yet.",
      {"name": {"type": "string", "description": "Only people whose name contains this"},
       "include_unnamed": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})
def photos_people(name="", include_unnamed=False, limit=50):
    _box_only()
    return people(name, include_unnamed, limit)


@tool("photos_search", "Find photos and videos. With `query` it is a meaning-based search (e.g. 'beach at sunset', "
      "'birthday cake'); without it, filter by person, dates, type, city or favourite and get the total count. "
      "Returns ids, file names, dates, people and an `open` link per item.",
      {"query": {"type": "string"}, "person": {"type": "string", "description": "Person name or id; several comma-separated means photos with all of them"},
       "taken_after": {"type": "string", "description": "YYYY-MM-DD"}, "taken_before": {"type": "string", "description": "YYYY-MM-DD"},
       "media_type": {"type": "string", "enum": ["IMAGE", "VIDEO"]}, "city": {"type": "string"},
       "favorite": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}})
def photos_search(query="", person="", taken_after="", taken_before="", media_type="", city="", favorite=None, limit=25):
    _box_only()
    return search(query, person, taken_after, taken_before, media_type, city, favorite, limit)


@tool("photos_print_review", "Prepare and review a suggested monthly set of family photos for grandparent prints. "
      "Actions: status reads a month (previous month by default); generate ranks that month's photos and saves a draft; "
      "save changes the selected candidate ids and note; approve first previews the exact count and revision, then requires "
      "confirmed=true with the same expected_count and expected_revision. Approval prepares the set only: this tool never orders, mails or pays for prints.",
      {"action": {"type": "string", "enum": ["status", "generate", "save", "approve"]},
       "month": {"type": "string", "description": "YYYY-MM; previous calendar month when omitted"},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20},
       "asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
       "note": {"type": "string", "maxLength": 500}, "expected_count": {"type": "integer"},
       "expected_revision": {"type": "string"}, "confirmed": {"type": "boolean"}}, required=("action",))
def photos_print_review(action, month="", limit=8, asset_ids=None, note="", expected_count=None,
                        expected_revision="", confirmed=False):
    _box_only()
    if not switch_on(PRINT_REVIEW_SWITCH):
        raise RuntimeError("Monthly print review is not turned on in Try new features.")
    if action == "status":
        return print_review_status(month)
    if action == "generate":
        return generate_print_review(month, limit)
    if action == "save":
        return save_print_review(month, asset_ids, note)
    if action == "approve":
        return approve_print_review(month, expected_count, expected_revision, confirmed)
    raise ValueError("action is status, generate, save or approve")


@tool("photos_move_to_trash", "Move photos/videos to Immich's TRASH (restorable for the trash period, 30 days by "
      "default; never a permanent delete). Pick them by asset_ids, or by person and/or date range. First call without "
      "confirmed: it returns the exact count and a sample. Tell the owner the count, get his explicit yes, then call "
      "again with confirmed=true and expected_count equal to that count.",
      {"asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5000},
       "person": {"type": "string"}, "taken_after": {"type": "string"}, "taken_before": {"type": "string"},
       "media_type": {"type": "string", "enum": ["IMAGE", "VIDEO"]},
       "expected_count": {"type": "integer"}, "confirmed": {"type": "boolean"}})
def photos_move_to_trash(asset_ids=None, person="", taken_after="", taken_before="", media_type="", expected_count=None,
                         confirmed=False):
    _box_only()
    return move_to_trash(asset_ids, person, taken_after, taken_before, media_type, expected_count, confirmed)


@tool("photos_restore_from_trash", "Undo: bring photos back from Immich's trash, by asset_ids or everything=true.",
      {"asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5000}, "everything": {"type": "boolean"}})
def photos_restore_from_trash(asset_ids=None, everything=False):
    _box_only()
    return restore_from_trash(asset_ids, everything)


@tool("photos_import_status", "Google Takeout import: Takeout files waiting in Files > Photos > Import, whether an "
      "import is running, done or failed, its options and the last log lines.")
def photos_import_status():
    _box_only()
    return import_status()


@tool("photos_import_start", "Import every Google Takeout file in Files > Photos > Import into Immich with immich-go "
      "(keeps dates, places, descriptions and albums from Google's JSON files; skips photos already imported). "
      "Without confirmed it only lists what would be imported.",
      {"confirmed": {"type": "boolean"}})
def photos_import_start(confirmed=False):
    _box_only()
    return import_start(confirmed)


# ---------------- owner page and its API ----------------

def _json(code, obj, extra=None):
    return (code, "application/json", json.dumps(obj).encode()) + ((extra,) if extra else ())


_LOCAL = ("127.0.0.1", "localhost")


def _trusted(headers, write):
    host = (headers.get("host") or "").strip().lower()
    hosts = {h.strip().lower() for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(",") if h.strip()}
    if hosts and host not in hosts and host.rsplit(":", 1)[0] not in _LOCAL:
        return False
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    if owners and login and login not in owners:
        return False
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if write and origin and urllib.parse.urlsplit(origin).netloc.lower() != host:
        return False
    return True


def _body(headers, body):
    if (headers.get("content-type") or "").split(";")[0].strip() != "application/json" or len(body) > 8192:
        raise ValueError("send JSON")
    d = json.loads(body or b"{}")
    if not isinstance(d, dict):
        raise ValueError("send a JSON object")
    return d


def _review_body(headers, body, allowed, required=()):
    d = _body(headers, body)
    unknown = set(d) - set(allowed)
    missing = set(required) - set(d)
    if unknown or missing:
        raise ValueError("invalid print-review fields")
    if "month" in d and not isinstance(d["month"], str):
        raise ValueError("month must be a string like 2026-10")
    if "limit" in d and (isinstance(d["limit"], bool) or not isinstance(d["limit"], int) or not 1 <= d["limit"] <= 20):
        raise ValueError("limit must be an integer from 1 to 20")
    if "asset_ids" in d and (not isinstance(d["asset_ids"], list) or len(d["asset_ids"]) > 20 or
                              any(not isinstance(x, str) for x in d["asset_ids"])):
        raise ValueError("asset_ids must be a list of up to 20 ids")
    if "note" in d and (not isinstance(d["note"], str) or len(d["note"]) > 500):
        raise ValueError("note must be a string of 500 characters or fewer")
    if "confirmed" in d and not isinstance(d["confirmed"], bool):
        raise ValueError("confirmed must be true or false")
    if "expected_count" in d and (isinstance(d["expected_count"], bool) or not isinstance(d["expected_count"], int)):
        raise ValueError("expected_count must be an integer")
    if "expected_revision" in d and not isinstance(d["expected_revision"], str):
        raise ValueError("expected_revision must be a string")
    return d


# ---- wizard.app-switches/1 (wizard-os docs/APP_SWITCHES.md) ----
# Owner feature switches. New features ship behind one, off by default: append it to SWITCHES
# ({"id", "name", "description", "default": False, "issues": ["oscarhenrycollins/photos#N"]}) and gate it on
# switch_on(id). Kept on the box (STATE_DIR/switches.json). Owner only: the box's Tailscale Serve login with
# X-Wizard-Switch: 1; never a bearer or a box-local host.
SWITCHES = [
    {"id": PRINT_REVIEW_SWITCH, "name": "Monthly grandparent print review",
     "description": "Suggest last month's best family photos, then let you adjust and approve a print set. Nothing is ordered.",
     "default": False, "issues": ["humanitylabs-org/photos#2"]},
]
_SW_LOCAL = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def switches_doc():
    saved = _read_json("switches.json", {}) or {}
    return {"contract": "wizard.app-switches/1", "switches": [
        dict(x, on=saved[x["id"]] if isinstance(saved.get(x["id"]), bool) else bool(x.get("default"))) for x in SWITCHES]}


def switch_on(switch_id):
    return next((x["on"] for x in switches_doc()["switches"] if x["id"] == switch_id), False)


def _sw_owner(headers, write):
    if headers.get("authorization") or headers.get("x-wizard-switch") != "1":
        return False
    host = (headers.get("host") or "").strip().lower()
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if not name or name in _SW_LOCAL or "." not in name:
        return False
    origin = headers.get("origin")
    if origin and urllib.parse.urlsplit(origin).netloc.lower() != host:
        return False
    if write and not origin:
        return False
    login = headers.get("tailscale-user-login") or ""
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    return "@" in login and "," not in login and (not owners or login.strip().lower() in owners)


def switches_route(method, headers, body):
    if method in ("GET", "HEAD"):
        if not _sw_owner(headers, False):
            return _json(403, {"error": "Only this Wizard's owner can see these switches.", "code": "owner_only"})
        return _json(200, switches_doc())
    if method != "POST":
        return _json(405, {"error": "method"})
    if not _sw_owner(headers, True):
        return _json(403, {"error": "Only this Wizard's owner can change these switches.", "code": "owner_only"})
    try:
        d = _body(headers, body)
        if not isinstance(d.get("id"), str) or not isinstance(d.get("on"), bool) or set(d) - {"id", "on"}:
            raise ValueError
    except (ValueError, TypeError):
        return _json(400, {"error": 'Send JSON {"id": "...", "on": true|false}', "code": "invalid_input"})
    if d["id"] not in {x["id"] for x in SWITCHES}:
        return _json(404, {"error": "No such switch", "code": "unknown_switch"})
    ids = {x["id"] for x in SWITCHES}
    saved = {k: v for k, v in (_read_json("switches.json", {}) or {}).items() if k in ids and isinstance(v, bool)}
    saved[d["id"]] = d["on"]
    _write_json("switches.json", saved)
    return _json(200, switches_doc())


@route("/api/switches", match=lambda p: p.rstrip("/") == "/api/switches")
def switches_bare_route(method, path, query, headers, body):
    return switches_route(method, headers, body)
# ---- end wizard.app-switches/1 ----


def _health():
    code, _ = _immich("GET", "/server/ping", timeout=3)
    return {"ok": True, "app": APP, "version": VERSION, "immich": code == 200}


@route("/healthz", match=lambda p: p == "/healthz")
def health_route(method, path, query, headers, body):
    return _json(200, _health())


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-src https: http:; connect-src 'self'; frame-ancestors 'self'")


def _page_path(p):
    """Wizard owns /photos/ and its own files; Immich keeps /photos and /photos/<asset-uuid>[/...]."""
    if not p.startswith(PAGE):
        return False
    first = p[len(PAGE):].split("/", 1)[0]
    return not UUID_RE.match(first)


@route(PAGE, match=_page_path)
def page_route(method, path, query, headers, body):
    sub = path[len(PAGE):]
    if sub in ("healthz",):
        return _json(200, _health())
    if sub in ("api/switches", "api/switches/"):
        return switches_route(method, headers, body)
    if sub.startswith("api/"):
        write = method not in ("GET", "HEAD")
        if not _trusted(headers, write):
            return _json(403, {"error": "Open Photos from your Wizard.", "code": "owner_only"})
        try:
            if method == "GET" and sub == "api/status":
                return _json(200, status(headers))
            if method == "GET" and sub == "api/import":
                return _json(200, import_status())
            if method == "GET" and sub == "api/import/options":
                return _json(200, {"options": import_options()})
            if method == "POST" and sub == "api/import/options":
                return _json(200, {"options": set_import_options(_body(headers, body))})
            if method == "POST" and sub == "api/import/start":
                _body(headers, body)
                return _json(200, import_start(True))
            if sub.startswith("api/print-review") and not switch_on(PRINT_REVIEW_SWITCH):
                return _json(404, {"error": "Monthly print review is not turned on.", "code": "feature_off"})
            if method == "GET" and sub == "api/print-review":
                month = dict(urllib.parse.parse_qsl(query)).get("month", "")
                return _json(200, print_review_status(month))
            if method == "GET" and sub.startswith("api/print-review/thumbnail/"):
                asset_id = sub.rsplit("/", 1)[-1]
                params = dict(urllib.parse.parse_qsl(query))
                if not params.get("month"):
                    raise ValueError("month is required for review thumbnails")
                month = _review_month(params["month"])
                record = (_review_store().get("reviews") or {}).get(month) or {}
                if (not UUID_RE.match(asset_id) or not _key() or
                        asset_id not in set(record.get("candidate_ids") or [])):
                    return _json(404, {"error": "not found"})
                code, data = _immich("GET", "/assets/%s/thumbnail?size=preview" % asset_id,
                                     key=_key(), timeout=30, raw=True)
                return ((code, "image/jpeg", data, {"Cache-Control": "private, no-store"})
                        if code == 200 else _json(404, {"error": "not found"}))
            if method == "POST" and sub == "api/print-review/generate":
                d = _review_body(headers, body, {"month", "limit"})
                return _json(200, generate_print_review(d.get("month", ""), d.get("limit", 8)))
            if method == "POST" and sub == "api/print-review/save":
                d = _review_body(headers, body, {"month", "asset_ids", "note"}, required=("asset_ids",))
                return _json(200, save_print_review(d.get("month", ""), d["asset_ids"], d.get("note", "")))
            if method == "POST" and sub == "api/print-review/approve":
                d = _review_body(headers, body, {"month", "expected_count", "expected_revision", "confirmed"},
                                 required=("confirmed",))
                return _json(200, approve_print_review(d.get("month", ""), d.get("expected_count"),
                                                        d.get("expected_revision", ""), d["confirmed"]))
        except (ValueError, RuntimeError) as e:
            return _json(400, {"error": str(e)})
        return _json(404, {"error": "not found"})
    if method not in ("GET", "HEAD"):
        return _json(405, {"error": "method"})
    name = sub or "index.html"
    if name in VENDOR:
        ext = "." + name.rsplit(".", 1)[-1]
        extra = {"Content-Security-Policy": CSP} if ext == ".html" else None
        out = (200, MIME.get(ext, "application/octet-stream"), base64.b64decode(VENDOR[name]))
        return out + ((extra,) if extra else ())
    return _json(404, {"error": "not found"})
