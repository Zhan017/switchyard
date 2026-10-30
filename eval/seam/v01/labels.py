"""Stage 2: the human check. 50 stitched answers, 10 per strategy, spread over the pairs and
drawn with a fixed seed, are labelled blind on the local view before any judge runs."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from eval.seam.records import Prompt
from eval.seam.v01.judge import local_view
from eval.seam.v01.plan import Plan
from eval.seam.v01.records import Reference, Stitched

CHOICES = ("A", "B", "cant_tell")


def labels_dir(out: Path) -> Path:
    return out / "labels"


def sample(plan: Plan, items: list[Stitched]) -> list[Stitched]:
    """``per_strategy`` items for each strategy, round robin over the pairs that have it,
    preferring prompts not drawn yet. Only continued items can be labelled."""
    rng = random.Random(plan.label_seed)
    drawn: list[Stitched] = []
    used_prompts: set[str] = set()
    for strategy in plan.strategies:
        pools: dict[str, list[Stitched]] = {}
        for item in sorted(items, key=lambda i: i.item_id):
            if item.strategy == strategy and item.outcome == "ok":
                pools.setdefault(item.pair, []).append(item)
        for pool in pools.values():
            rng.shuffle(pool)
        pairs = sorted(pools)
        rng.shuffle(pairs)
        taken = 0
        while taken < plan.label_per_strategy and any(pools.values()):
            for pair in pairs:
                if taken == plan.label_per_strategy or not pools[pair]:
                    continue
                pool = pools[pair]
                pick = next((i for i in pool if i.prompt_id not in used_prompts), pool[0])
                pool.remove(pick)
                drawn.append(pick)
                used_prompts.add(pick.prompt_id)
                taken += 1
    return drawn


def export(
    plan: Plan,
    out: Path,
    drawn: list[Stitched],
    refs: dict[str, Reference],
    prompts: list[Prompt],
) -> Path:
    """Write the blind items, the key, and a page to label them on. Returns the page."""
    rng = random.Random(plan.label_seed + 1)
    by_prompt = {p.id: p for p in prompts}
    order = list(drawn)
    rng.shuffle(order)
    items, key = [], []
    for n, item in enumerate(order, start=1):
        ref = refs[f"{item.prompt_id}-{item.primary}"]
        stitched = ref.text[: item.cut] + item.continuation
        ref_view, stitched_view = local_view(ref.text, stitched, item.cut, plan.local_chars)
        stitched_is = rng.choice("AB")
        a, b = (stitched_view, ref_view) if stitched_is == "A" else (ref_view, stitched_view)
        label_id = f"L{n:02d}"
        items.append({"id": label_id, "request": by_prompt[item.prompt_id].text, "a": a, "b": b})
        key.append({"id": label_id, "item_id": item.item_id, "stitched_is": stitched_is})
    folder = labels_dir(out)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "items.json").write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    (folder / "key.json").write_text(json.dumps(key, indent=1) + "\n")
    page = folder / "label.html"
    page.write_text(_page(items))
    return page


def load_key(out: Path) -> list[dict[str, str]]:
    path = labels_dir(out) / "key.json"
    if not path.exists():
        raise SystemExit("No labelling key yet: run the sample stage first.")
    key: list[dict[str, str]] = json.loads(path.read_text())
    return key


def load_labels(out: Path) -> dict[str, str]:
    """The human labels, label id -> A, B or cant_tell. Refuses an incomplete set."""
    key = load_key(out)
    path = labels_dir(out) / "labels.json"
    if not path.exists():
        raise SystemExit(f"No labels yet. Label the items in {labels_dir(out) / 'label.html'}.")
    raw: dict[str, Any] = json.loads(path.read_text())
    labels = {str(k): str(v) for k, v in (raw.get("labels") or raw).items()}
    missing = [k["id"] for k in key if labels.get(k["id"]) not in CHOICES]
    if missing:
        raise SystemExit(f"Labels missing or invalid for {', '.join(missing)}.")
    return labels


def human_verdicts(out: Path) -> dict[str, bool]:
    """Item id -> detected: the labeller picked the stitched answer. Can't tell is not."""
    labels = load_labels(out)
    return {k["item_id"]: labels[k["id"]] == k["stitched_is"] for k in load_key(out)}


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    """Cohen's kappa for two raters' yes/no verdicts on the same items. None when chance
    agreement is total (both raters gave one answer to everything)."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    if chance >= 1:
        return None
    return (observed - chance) / (1 - chance)


def _page(items: list[dict[str, str]]) -> str:
    # Inside a script tag only "</" can end the data early.
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.replace("__ITEMS__", data)


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seam labels</title>
<style>
:root { --bg: #fbfaf7; --fg: #1d1d1b; --muted: #6b6a66; --line: #dcd9d0; --card: #ffffff;
  --accent: #2f5d50; --picked: #e3eee9; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171716; --fg: #ecebe6; --muted: #a3a29c; --line: #34332f; --card: #1f1f1d;
    --accent: #8cc2ad; --picked: #24352f; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.help { color: var(--muted); margin: 0 0 16px; max-width: 70ch; }
.bar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.request { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; margin-bottom: 12px; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 760px) { .pair { grid-template-columns: 1fr; } }
.text { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; white-space: pre-wrap; word-break: break-word;
  font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
.text.picked { background: var(--picked); border-color: var(--accent); }
.label { font-weight: 600; margin: 0 0 6px; }
button { font: inherit; padding: 8px 14px; border-radius: 6px; border: 1px solid var(--line);
  background: var(--card); color: var(--fg); cursor: pointer; }
button.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.choices { display: flex; gap: 8px; margin: 14px 0; flex-wrap: wrap; }
.muted { color: var(--muted); }
</style>
</head>
<body>
<main>
<h1>Seam labels</h1>
<p class="help">Each item shows a request and two excerpts from two answers, taken from the
same point. One answer was written in one go. The other was cut off partway and finished by a
different writer. Pick the one that was continued by a different writer, or Can't tell.
Keys: a, b, c to choose; left and right arrows to move. Progress is kept in this browser.</p>
<div class="bar">
  <button id="prev">Back</button><button id="next">Next</button>
  <span id="where" class="muted"></span>
  <button id="save">Download labels.json</button>
</div>
<div class="request"><div class="label">Request</div><div id="request"></div></div>
<div class="choices">
  <button data-c="A">A was continued</button><button data-c="B">B was continued</button>
  <button data-c="cant_tell">Can't tell</button>
</div>
<div class="pair">
  <div><div class="label">A</div><div id="a" class="text"></div></div>
  <div><div class="label">B</div><div id="b" class="text"></div></div>
</div>
</main>
<script type="application/json" id="items">__ITEMS__</script>
<script>
const items = JSON.parse(document.getElementById("items").textContent);
const STORE = "seam-labels-v0.1";
let labels = {};
try { labels = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { labels = {}; }
let at = Math.max(0, items.findIndex(i => !labels[i.id]));
if (at < 0) at = 0;
function keep() { try { localStorage.setItem(STORE, JSON.stringify(labels)); } catch (e) {} }
function show() {
  const item = items[at];
  document.getElementById("request").textContent = item.request;
  document.getElementById("a").textContent = item.a;
  document.getElementById("b").textContent = item.b;
  const done = items.filter(i => labels[i.id]).length;
  document.getElementById("where").textContent =
    `${item.id}, ${at + 1} of ${items.length}; ${done} labelled`;
  for (const b of document.querySelectorAll("[data-c]")) {
    b.classList.toggle("on", labels[item.id] === b.dataset.c);
  }
  document.getElementById("a").classList.toggle("picked", labels[item.id] === "A");
  document.getElementById("b").classList.toggle("picked", labels[item.id] === "B");
}
function choose(c) {
  labels[items[at].id] = c; keep();
  if (at < items.length - 1) at += 1;
  show();
}
for (const b of document.querySelectorAll("[data-c]")) b.onclick = () => choose(b.dataset.c);
document.getElementById("prev").onclick = () => { at = Math.max(0, at - 1); show(); };
document.getElementById("next").onclick = () => {
  at = Math.min(items.length - 1, at + 1); show();
};
document.getElementById("save").onclick = () => {
  const blob = new Blob([JSON.stringify({labels, saved_at: new Date().toISOString()}, null, 1)],
    {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "labels.json"; a.click();
};
document.addEventListener("keydown", e => {
  if (e.key === "a") choose("A"); else if (e.key === "b") choose("B");
  else if (e.key === "c") choose("cant_tell");
  else if (e.key === "ArrowLeft") document.getElementById("prev").click();
  else if (e.key === "ArrowRight") document.getElementById("next").click();
});
show();
</script>
</body>
</html>
"""
