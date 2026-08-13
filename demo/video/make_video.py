"""Render the groundgraph demo video (silent, captioned, 1280x720).

Deterministic pipeline: HTML frames (terminal aesthetic + title cards) ->
Chrome headless screenshots -> ffmpeg concat. Rerunnable by anyone:

    python demo/video/make_video.py --capture /tmp/gg-video-capture.txt \
        --out /tmp/groundgraph-demo.mp4

The capture file is the output of the demo commands, delimited by
`### SECTION` markers (see demo/video/capture.sh).
"""
from __future__ import annotations

import argparse
import html
import shutil
import subprocess
import sys
from pathlib import Path

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
]

W, H = 1280, 720

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { width: 1280px; height: 720px; overflow: hidden;
       background: #0d1117; color: #c9d1d9;
       font-family: -apple-system, 'Segoe UI', sans-serif; }
.card { height: 100%; display: flex; flex-direction: column;
        justify-content: center; align-items: center; text-align: center;
        padding: 0 90px; background:
        radial-gradient(ellipse at 30% 20%, #16202e 0%, #0d1117 60%); }
.card h1 { font-size: 64px; color: #e6edf3; letter-spacing: -1px; }
.card h1 .accent { color: #3fb950; }
.card .sub { font-size: 27px; color: #8b949e; margin-top: 26px; line-height: 1.45; }
.card .small { font-size: 20px; color: #58a6ff; margin-top: 34px;
               font-family: 'SF Mono', Menlo, monospace; }
.card .zh { font-size: 19px; color: #6e7681; margin-top: 14px; }
.term { height: 100%; padding: 34px 44px; }
.win { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
       height: 100%; box-shadow: 0 16px 48px rgba(0,0,0,.5); overflow: hidden; }
.bar { background: #21262d; padding: 11px 16px; display: flex; gap: 8px;
       align-items: center; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.bar .t { color: #8b949e; font-size: 13px; margin-left: 12px;
          font-family: Menlo, monospace; }
.body { padding: 20px 26px; font-family: 'SF Mono', Menlo, monospace;
        font-size: 16.5px; line-height: 1.52; white-space: pre-wrap;
        word-break: break-all; }
.p { color: #3fb950; font-weight: 600; }
.c { color: #e6edf3; font-weight: 600; }
.o { color: #9da7b3; }
.hl { color: #58a6ff; }
.dim { color: #6e7681; }
.cap { position: absolute; bottom: 22px; left: 0; right: 0; text-align: center;
       color: #8b949e; font-size: 19px; }
.cursor { display: inline-block; width: 9px; height: 19px; background: #c9d1d9;
          vertical-align: text-bottom; }
"""


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def card(title_html: str, sub: str, small: str = "", zh: str = "") -> str:
    return (f"<style>{CSS}</style><div class='card'><h1>{title_html}</h1>"
            f"<div class='sub'>{sub}</div>"
            + (f"<div class='small'>{esc(small)}</div>" if small else "")
            + (f"<div class='zh'>{esc(zh)}</div>" if zh else "")
            + "</div>")


def term(cmd_shown: str, out_html: str, caption: str, cursor: bool) -> str:
    cur = "<span class='cursor'></span>" if cursor else ""
    return (f"<style>{CSS}</style><div class='term'><div class='win'>"
            "<div class='bar'>"
            "<div class='dot' style='background:#ff5f57'></div>"
            "<div class='dot' style='background:#febc2e'></div>"
            "<div class='dot' style='background:#28c840'></div>"
            "<div class='t'>groundgraph — demo</div></div>"
            f"<div class='body'><span class='p'>$ </span>"
            f"<span class='c'>{esc(cmd_shown)}</span>{cur}\n{out_html}</div>"
            f"</div></div><div class='cap'>{esc(caption)}</div>")


def colorize(lines: list[str]) -> str:
    out = []
    for ln in lines:
        e = esc(ln)
        if ln.startswith(("[graph]", "-- lever")):
            out.append(f"<span class='hl'>{e}</span>")
        elif ln.startswith("- `"):
            out.append(f"<span class='hl'>{e}</span>")
        elif "(conf" in ln or ln.startswith(("{", " ", "}")):
            out.append(f"<span class='o'>{e}</span>")
        elif ln.startswith(("==", "--", "###")):
            out.append(f"<span class='dim'>{e}</span>")
        else:
            out.append(f"<span class='o'>{e}</span>")
    return "\n".join(out)


def load_capture(path: Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    key = None
    for ln in path.read_text().splitlines():
        if ln.startswith("### "):
            key = ln[4:].strip()
            sections[key] = []
        elif key:
            sections[key].append(ln.rstrip())
    return {k: [ln for ln in v if ln.strip() or v.index(ln) < len(v)] for k, v in sections.items()}


def typed_frames(cmd: str, out_lines: list[str], caption: str,
                 out_secs: float) -> list[tuple[str, float]]:
    """Two typing frames, then the full output frame."""
    part = cmd[: max(3, int(len(cmd) * 0.45))]
    return [
        (term(part, "", caption, cursor=True), 0.45),
        (term(cmd, "", caption, cursor=True), 0.6),
        (term(cmd, colorize(out_lines), caption, cursor=False), out_secs),
    ]


def build_frames(cap: dict[str, list[str]]) -> list[tuple[str, float]]:
    frames: list[tuple[str, float]] = []
    frames.append((card(
        "ground<span class='accent'>graph</span>",
        "A strictly-local, deterministic code-memory graph for coding agents",
        "zero runtime dependencies · one SQLite file · MIT",
        "零依赖 · 严格本地化 · 全流程无大模型 · 每条事实可证明"), 3.2))
    frames.append((card(
        "Every fact is <span class='accent'>provable</span>",
        "AST nodes · git commits · doc lines · proof-carrying derivations —<br>"
        "no LLM anywhere in the pipeline",
        "a smaller graph you can prove beats a bigger graph you can vibe",
        "生成能力用于回答问题，而不是用于记忆"), 3.8))
    frames += typed_frames(
        "pip install -e . && bash demo/demo.sh",
        cap.get("BUILD", []),
        "Index all of Flask: ~3 seconds, ~12,500 facts, stdlib only", 5.5)
    frames += typed_frames(
        "groundgraph query --predicate called-by --subject url_for",
        cap.get("CALLED_BY", []),
        "Who calls url_for? — materialized inverse edges", 4.5)
    frames += typed_frames(
        "groundgraph query --predicate tests --object flask.Flask",
        cap.get("TESTS", []),
        "What covers Flask? — fail-closed tests relation (import AND reference)", 4.5)
    frames += typed_frames(
        'groundgraph assist "url_for builds the wrong external URL scheme..."',
        cap.get("ASSIST", []),
        "Serve-time recall: file:line + relations + co-change — with a fired-signal", 8.5)
    frames += typed_frames(
        "groundgraph tool query_facts '{\"predicate\": \"may-raise\", ...}'",
        cap.get("MAYRAISE", []),
        "Agentic tools: exception flow derived with proof paths", 4.5)
    if cap.get("MCP"):
        frames += typed_frames(
            "claude mcp add groundgraph -- python -m groundgraph mcp --db graph.db",
            cap.get("MCP", []),
            "MCP server: plug the graph into Claude Code or any MCP client", 6.0)
    frames += typed_frames(
        "groundgraph status",
        cap.get("STATUS", []),
        "Anti-rot dashboard: tiers, duplicates, contradictions, freshness", 5.0)
    frames.append((card(
        "Measured, not <span class='accent'>hyped</span>",
        "Built-in experiment instrumentation records whether the lever fired.<br>"
        "We publish our methodology — and our null result.",
        "docs/honest-eval.md",
        "内置实验插桩：阴性结果可归因，我们连同方法论一起公开"), 4.2))
    frames.append((card(
        "github.com/alleboudy/<span class='accent'>groundgraph</span>",
        "MIT · Python ≥ 3.10 · zero runtime dependencies",
        "pip install -e . && bash demo/demo.sh",
        "你的代码和图谱永不离开你的机器"), 4.5))
    return frames


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, type=Path)
    ap.add_argument("--out", default="/tmp/groundgraph-demo.mp4")
    ap.add_argument("--workdir", default="/tmp/gg-video-frames", type=Path)
    args = ap.parse_args()

    chrome = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if not chrome or not shutil.which("ffmpeg"):
        print("need Chrome + ffmpeg", file=sys.stderr)
        return 1

    wd = args.workdir
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    frames = build_frames(load_capture(args.capture))

    concat = ["ffconcat version 1.0"]
    for i, (html_body, secs) in enumerate(frames):
        hf = wd / f"f{i:03d}.html"
        pf = wd / f"f{i:03d}.png"
        hf.write_text(f"<!doctype html><html><body>{html_body}</body></html>")
        subprocess.run([chrome, "--headless=new", f"--screenshot={pf}",
                        f"--window-size={W},{H}", "--hide-scrollbars",
                        "--disable-gpu", f"file://{hf}"],
                       check=True, capture_output=True, timeout=30)
        concat.append(f"file '{pf}'")
        concat.append(f"duration {secs}")
    # concat demuxer needs the last file repeated
    concat.append(f"file '{wd / f'f{len(frames)-1:03d}.png'}'")
    (wd / "list.txt").write_text("\n".join(concat) + "\n")

    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(wd / "list.txt"),
                    "-vf", "fps=30", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20",
                    args.out],
                   check=True, capture_output=True, timeout=300)
    total = sum(s for _, s in frames)
    print(f"wrote {args.out} ({len(frames)} frames, ~{total:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
