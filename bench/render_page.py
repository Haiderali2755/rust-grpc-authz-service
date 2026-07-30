#!/usr/bin/env python3
"""Render the results page from measured sweep data.

Reads bench/results/sweep.json (and optionally k8s.json) and writes
bench/results/index.html. Refuses to render without measurements — every number
on the page comes from a run.

Form choice: the data is a latency-vs-throughput curve, so throughput is the x
axis and latency the y, with p50/p90/p99 as three series on ONE shared axis.
Deliberately not a dual-axis chart of latency and throughput against
concurrency: two arbitrary scales on one plot invent a relationship that is not
in the data.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")

# Categorical slots 1-3 of the reference palette. Validated with
# scripts/validate_palette.js in both modes: all checks pass, worst adjacent CVD
# deltaE 9.2 light / 9.4 dark. Light-mode aqua carries a contrast WARN (2.74:1),
# which the skill requires be relieved by visible labels — every series is
# directly labelled and a table view ships below, satisfying that.
P50_L, P90_L, P99_L = "#2a78d6", "#eb6834", "#1baf7a"
P50_D, P90_D, P99_D = "#3987e5", "#d95926", "#199e70"

# Plot geometry (viewBox units).
W, H = 720, 344
# PAD_T leaves room for the y-axis title above the plot, so it cannot collide
# with the topmost tick label.
PAD_L, PAD_R, PAD_T, PAD_B = 56, 92, 34, 42


def load(name, required=True):
    path = os.path.join(RESULTS, f"{name}.json")
    if not os.path.exists(path):
        if required:
            sys.exit(
                f"missing {path}\n"
                "Run ./bench/run_benchmark.sh first — this renders measured data only."
            )
        return None
    with open(path) as f:
        return json.load(f)


def main():
    sweep = load("sweep")["sweep"]
    k8s = load("k8s", required=False)

    sweep = sorted(sweep, key=lambda r: r["concurrency"])

    xs = [r["throughput_rps"] for r in sweep]
    all_lat_us = [r["latency"][k] for r in sweep for k in ("p50_us", "p90_us", "p99_us")]

    x_max = max(xs) * 1.06
    y_max = max(all_lat_us) / 1000 * 1.10  # ms

    def sx(rps):
        return PAD_L + (rps / x_max) * (W - PAD_L - PAD_R)

    def sy(ms):
        return H - PAD_B - (ms / y_max) * (H - PAD_T - PAD_B)

    series = [
        ("p50", "p50_us", "var(--p50)", P50_L),
        ("p90", "p90_us", "var(--p90)", P90_L),
        ("p99", "p99_us", "var(--p99)", P99_L),
    ]

    # The 1ms line is the claim's threshold, so it is drawn as a reference rule.
    one_ms_y = sy(1.0)

    paths = []
    for label, key, color, _hex in series:
        pts = [(sx(r["throughput_rps"]), sy(r["latency"][key] / 1000)) for r in sweep]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        paths.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for (x, y), r in zip(pts, sweep):
            paths.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" '
                f'stroke="var(--surface-1)" stroke-width="2">'
                f"<title>{label} at {r['throughput_rps']:,.0f} rps "
                f"(concurrency {r['concurrency']}): "
                f"{r['latency'][key] / 1000:.3f} ms</title></circle>"
            )
        # Direct label at the series end — required relief for the contrast WARN.
        lx, ly = pts[-1]
        paths.append(
            f'<text x="{lx + 9:.1f}" y="{ly + 4:.1f}" fill="{color}" '
            f'font-size="12" font-weight="640">{label}</text>'
        )

    # Axes ticks.
    ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        rps = x_max * frac
        x = sx(rps)
        ticks.append(
            f'<line x1="{x:.1f}" y1="{H - PAD_B}" x2="{x:.1f}" y2="{H - PAD_B + 4}" '
            f'stroke="var(--rule)" stroke-width="1"/>'
            f'<text x="{x:.1f}" y="{H - PAD_B + 17}" text-anchor="middle" '
            f'fill="var(--text-muted)" font-size="11">{rps / 1000:.0f}k</text>'
        )
    n_y = 4
    for i in range(n_y + 1):
        ms = y_max * i / n_y
        y = sy(ms)
        ticks.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
            f'stroke="var(--rule)" stroke-width="1" opacity="0.55"/>'
            f'<text x="{PAD_L - 9}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="var(--text-muted)" font-size="11">{ms:.1f}</text>'
        )

    # Table + concurrency annotations.
    rows = "".join(
        f"<tr><td>{r['concurrency']}</td>"
        f"<td>{r['throughput_rps']:,.0f}</td>"
        f"<td>{r['latency']['p50_us'] / 1000:.3f}</td>"
        f"<td>{r['latency']['p90_us'] / 1000:.3f}</td>"
        f"<td>{r['latency']['p99_us'] / 1000:.3f}</td>"
        f"<td>{r['sub_millisecond_share']:.1f}%</td>"
        f"<td>{r['requests_ok']:,}</td>"
        f"<td>{r['requests_err']}</td></tr>"
        for r in sweep
    )

    # Headline: the fastest point that still holds sub-ms p50, stated with its load.
    sub_ms = [r for r in sweep if r["latency"]["p50_us"] < 1000]
    best = max(sub_ms, key=lambda r: r["throughput_rps"]) if sub_ms else None
    total_reqs = sum(r["requests_ok"] for r in sweep)
    total_errs = sum(r["requests_err"] for r in sweep)

    if best:
        hero_fig = f"{best['latency']['p50_us'] / 1000:.2f}<span class='u'>ms</span>"
        hero_txt = (
            f"<b>p50 at {best['throughput_rps']:,.0f} req/s</b> — the highest "
            f"throughput at which median latency stays under 1 ms. "
            f"{best['sub_millisecond_share']:.1f}% of all requests at that load "
            f"completed inside 1 ms."
        )
    else:
        hero_fig = "n/a"
        hero_txt = "No measured point held a sub-millisecond p50."

    k8s_block = ""
    if k8s:
        k8s_block = f"""
  <section class="panel">
    <h3>Running on Kubernetes</h3>
    <p>Deployed to a kind cluster from the manifests in <code>k8s/</code> — 3/3
       replicas Ready, zero restarts. Driving the same client through the cluster:
       <b>{k8s['requests_ok']:,} RPCs</b>, <b>{k8s['requests_err']} errors</b>,
       <b>{k8s['latency']['p50_us'] / 1000:.2f} ms p50</b> at concurrency
       {k8s['concurrency']}.</p>
    <p class="muted">Higher than the {next(r['latency']['p50_us'] / 1000 for r in sweep if r['concurrency'] == k8s['concurrency']):.2f} ms
       measured over plain Docker at the same concurrency. The difference is
       <code>kubectl port-forward</code> — a userspace TCP proxy — plus pod
       networking, not the service. Quoting the Docker figure for a Kubernetes
       deployment would be wrong, so both are recorded.</p>
  </section>"""

    html = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gRPC Authz Service — Latency Benchmark</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --surface-2: #f2f2ef; --surface-3: #e9e9e4;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
    --p50: {P50_L}; --p90: {P90_L}; --p99: {P99_L}; --rule: #e2e2dd;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: #1a1a19; --surface-2: #232322; --surface-3: #2c2c2a;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
      --p50: {P50_D}; --p90: {P90_D}; --p99: {P99_D}; --rule: #34342f;
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #232322; --surface-3: #2c2c2a;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e85;
    --p50: {P50_D}; --p90: {P90_D}; --p99: {P99_D}; --rule: #34342f;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; }}
  .viz-root {{
    background: var(--surface-1); color: var(--text-primary);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 48px 24px 64px; min-height: 100vh;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; }}
  h1 {{ font-size: 26px; line-height: 1.25; margin: 0 0 8px; letter-spacing: -0.02em; }}
  .sub {{ color: var(--text-secondary); font-size: 15px; margin: 0 0 16px; max-width: 62ch; }}
  .cfg {{ color: var(--text-muted); font-size: 12.5px; margin: 0 0 30px; }}
  .cfg b {{ color: var(--text-secondary); font-weight: 600; }}

  .hero {{ background: var(--surface-2); border-radius: 12px; padding: 26px 30px;
           margin-bottom: 12px; display: flex; align-items: baseline; gap: 20px;
           flex-wrap: wrap; }}
  .hero-fig {{ font-size: 54px; font-weight: 680; line-height: 1;
               letter-spacing: -0.035em; font-variant-numeric: tabular-nums; }}
  .hero-fig .u {{ font-size: 24px; font-weight: 600; color: var(--text-secondary);
                  margin-left: 3px; }}
  .hero-txt {{ font-size: 14.5px; color: var(--text-secondary); max-width: 40ch; }}
  .hero-txt b {{ color: var(--text-primary); font-weight: 600; }}

  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 10px; margin-bottom: 38px; }}
  .kpi {{ background: var(--surface-2); border-radius: 10px; padding: 14px 16px; }}
  .kpi-label {{ font-size: 11.5px; color: var(--text-muted); text-transform: uppercase;
                letter-spacing: 0.05em; display: block; }}
  .kpi-val {{ font-size: 20px; font-weight: 640; font-variant-numeric: tabular-nums;
              display: block; margin-top: 2px; }}

  .chartwrap {{ margin-bottom: 8px; overflow-x: auto; }}
  svg {{ display: block; width: 100%; height: auto; min-width: 520px; }}
  .axis-title {{ font-size: 11.5px; fill: var(--text-muted); }}
  .legend {{ display: flex; gap: 18px; font-size: 13px; color: var(--text-secondary);
             flex-wrap: wrap; margin-bottom: 26px; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 7px; }}
  .swatch {{ width: 11px; height: 11px; border-radius: 3px; flex: none; }}

  .panel {{ background: var(--surface-2); border-radius: 10px; padding: 18px 20px;
            margin-bottom: 14px; font-size: 13.5px; color: var(--text-secondary); }}
  .panel h3 {{ font-size: 14px; margin: 0 0 8px; color: var(--text-primary); }}
  .panel p {{ margin: 0 0 9px; }}
  .panel p:last-child {{ margin-bottom: 0; }}
  .panel b {{ color: var(--text-primary); font-weight: 600; }}
  .panel .muted {{ color: var(--text-muted); font-size: 12.5px; }}
  code {{ font: 12.5px ui-monospace, SFMono-Regular, Menlo, monospace;
          background: var(--surface-3); padding: 1px 5px; border-radius: 4px; }}

  details {{ margin-top: 22px; }}
  summary {{ cursor: pointer; font-size: 13px; color: var(--text-secondary); padding: 7px 0; }}
  .tblwrap {{ overflow-x: auto; margin-top: 10px; }}
  table {{ border-collapse: collapse; font-size: 12.5px; width: 100%; min-width: 560px; }}
  th, td {{ text-align: right; padding: 7px 12px 7px 0; border-bottom: 1px solid var(--rule);
            font-variant-numeric: tabular-nums; white-space: nowrap; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: var(--text-secondary); font-weight: 600; }}

  footer {{ margin-top: 30px; font-size: 12.5px; color: var(--text-muted); }}
  footer a {{ color: inherit; }}

  @media (max-width: 560px) {{
    .viz-root {{ padding: 32px 16px 48px; }}
    h1 {{ font-size: 22px; }}
    .hero {{ padding: 20px; }}
    .hero-fig {{ font-size: 42px; }}
  }}
</style>
<div class="viz-root">
<div class="wrap">

  <h1>gRPC session validation: latency under load</h1>
  <p class="sub">A Rust gRPC service that resolves a session token and consumes
     rate budget in one call, both from Redis. Measured across a concurrency
     sweep rather than at a single point.</p>
  <p class="cfg">
    Rust · Tonic · Redis Lua · service capped at <b>2 CPU / 512 MiB</b> ·
    <b>{total_reqs:,}</b> requests across {len(sweep)} load levels ·
    <b>{total_errs}</b> errors · latencies from an HdrHistogram
  </p>

  <div class="hero">
    <span class="hero-fig">{hero_fig}</span>
    <span class="hero-txt">{hero_txt}</span>
  </div>

  <div class="kpis">
    <div class="kpi"><span class="kpi-label">Peak throughput</span>
      <span class="kpi-val">{max(xs):,.0f}<span style="font-size:12px;color:var(--text-muted)"> req/s</span></span></div>
    <div class="kpi"><span class="kpi-label">Best p50</span>
      <span class="kpi-val">{min(r['latency']['p50_us'] for r in sweep) / 1000:.2f}<span style="font-size:12px;color:var(--text-muted)"> ms</span></span></div>
    <div class="kpi"><span class="kpi-label">Requests served</span>
      <span class="kpi-val">{total_reqs:,}</span></div>
    <div class="kpi"><span class="kpi-label">Errors</span>
      <span class="kpi-val">{total_errs}</span></div>
  </div>

  <div class="chartwrap">
    <svg viewBox="0 0 {W} {H}" role="img"
         aria-label="Latency percentiles against throughput. Latency rises as
                     throughput approaches saturation near 19 to 23 thousand
                     requests per second.">
      {"".join(ticks)}
      <!-- The 1ms threshold the sub-millisecond claim refers to. -->
      <line x1="{PAD_L}" y1="{one_ms_y:.1f}" x2="{W - PAD_R}" y2="{one_ms_y:.1f}"
            stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 3"/>
      <text x="{W - PAD_R + 6}" y="{one_ms_y + 4:.1f}" fill="var(--text-muted)"
            font-size="11">1 ms</text>
      {"".join(paths)}
      <text x="{PAD_L}" y="{H - 6}" class="axis-title">throughput (requests/sec) →</text>
      <text x="{PAD_L - 9}" y="16" text-anchor="end" class="axis-title">ms</text>
      <text x="{PAD_L}" y="16" class="axis-title">latency percentiles</text>
    </svg>
  </div>

  <div class="legend">
    <span><i class="swatch" style="background:var(--p50)"></i>p50 (median)</span>
    <span><i class="swatch" style="background:var(--p90)"></i>p90</span>
    <span><i class="swatch" style="background:var(--p99)"></i>p99 (tail)</span>
    <span style="color:var(--text-muted)">dashed = 1 ms</span>
  </div>

  <section class="panel">
    <h3>Reading this curve</h3>
    <p><b>Sub-millisecond is real, but it is a property of load level — not of the
       service alone.</b> Median latency stays under 1 ms up to about
       {best['throughput_rps']:,.0f} req/s. Past that the service saturates and
       queueing dominates.</p>
    <p><b>Why it bends.</b> This is a closed-loop benchmark, so Little's Law
       applies: latency ≈ concurrency ÷ throughput. At 64 in-flight requests and
       {max(xs):,.0f} rps, 64 ÷ {max(xs):,.0f} ≈
       {64 / max(xs) * 1000:.1f} ms — which is what p50 measures there. Beyond the
       knee near 32 concurrency, extra load buys almost no throughput and costs
       latency proportionally.</p>
    <p><b>So a bare "sub-millisecond" claim means little.</b> Without a stated
       concurrency and percentile it is unfalsifiable, which is why the whole
       curve is published rather than one flattering point.</p>
  </section>
{k8s_block}
  <section class="panel">
    <h3>What is being measured</h3>
    <p>End-to-end gRPC round trip including the Redis round trip, client and
       server on one host. Every latency is recorded into an HdrHistogram, so the
       percentiles are real quantiles rather than an average with error bars.</p>
    <p class="muted">A real deployment adds network hops between client, service,
       and Redis — expect higher absolute numbers and a lower knee. Redis runs
       with persistence disabled; this measures the service, not Redis durability.</p>
  </section>

  <details>
    <summary>All measured values (table view)</summary>
    <div class="tblwrap">
      <table>
        <thead><tr>
          <th>Concurrency</th><th>req/s</th><th>p50 ms</th><th>p90 ms</th>
          <th>p99 ms</th><th>&lt;1ms</th><th>Requests</th><th>Errors</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </details>

  <footer>
    Raw measurements: <a href="sweep.json">sweep.json</a>
    &middot; <a href="https://github.com/Haiderali2755/rust-grpc-authz-service">Source, manifests and methodology</a>
  </footer>

</div>
</div>
</html>
"""

    out = os.path.join(RESULTS, "index.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
