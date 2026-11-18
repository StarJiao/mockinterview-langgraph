"""Read-only server status page at /server.

Every value comes from the running process, psutil, the two SQLite stores,
or the environment file — rendered server-side into one static HTML page.
Nothing here is writable: no POST, no state change. Secrets (API keys, pass
tokens) are masked before they reach the template; pass rows never carry
their plaintext or hash into this module.

The page is behind the same pass cookie as /api: the route handler validates
it directly because the gate middleware only guards /api/*.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import html
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import psutil

from .config import Config

REFRESH_SECONDS = 30
LOG_LINES = 20
ACTIVITY_HOURS = 48
"""Hourly buckets for the status-page charts; Beijing time (UTC+8) labels."""

BJ_TZ = timezone(timedelta(hours=8))
CHART_COLORS = ["#0f766e", "#b42318", "#a16207", "#2b5f9e", "#7c3aed", "#db2777"]

MASKED_KEY = "****"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def mask_secret(value: str | None) -> str:
    """Show enough to tell two keys apart, never enough to steal one."""
    if not value:
        return "（未设置）"
    if len(value) <= 8:
        return MASKED_KEY
    return f"{value[:4]}{MASKED_KEY}{value[-2:]}"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=5
    )


def _git_info() -> dict:
    """Deployed commit: the systemd unit's WorkingDirectory is the repo root."""
    for root in (Path.cwd(), Path(__file__).resolve().parents[2]):
        if not (root / ".git").exists():
            continue
        result = _run(
            ["git", "-C", str(root), "log", "-1", "--format=%h%n%s%n%ci%n%D"]
        )
        if result.returncode == 0:
            commit, subject, date, refs = result.stdout.strip().splitlines()[:4]
            branch_result = _run(
                ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"]
            )
            return {
                "commit": commit,
                "subject": subject,
                "date": date,
                "refs": refs,
                "branch": branch_result.stdout.strip() if branch_result.returncode == 0 else "",
                "root": str(root),
            }
    return {}


def _service_units() -> list[dict]:
    if not shutil.which("systemctl"):
        return []
    units = []
    for unit in ("mockinterview", "nginx"):
        result = _run(["systemctl", "is-active", unit])
        state = result.stdout.strip() or "unknown"
        if result.returncode != 0:
            state = state or "unknown"
        units.append({"unit": unit, "state": state})
    return units


def _recent_logs() -> dict:
    """Best effort: reading the system journal needs sudo/group membership."""
    if not shutil.which("journalctl"):
        return {"lines": [], "note": "当前环境没有 journalctl（非 systemd 系统）。"}
    result = _run(
        ["journalctl", "-u", "mockinterview", "-n", str(LOG_LINES),
         "--no-pager", "--output=short-iso"]
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[0] if detail else "权限不足"
        return {
            "lines": [],
            "note": f"进程无权限读取系统日志（{reason}）。请在服务器上执行："
                    f"sudo journalctl -u mockinterview -n {LOG_LINES}",
        }
    return {"lines": result.stdout.strip().splitlines()[-LOG_LINES:], "note": ""}


def _activity_view(rows: list[dict], hours: int = ACTIVITY_HOURS) -> dict:
    """Aggregate model calls into hourly Beijing-time buckets.

    Rows come from store.metrics_window(). Buckets include empty hours so the
    time axis stays honest; aggregation happens in Python because the stored
    ISO timestamps carry microseconds that SQLite date functions may reject.
    """
    now = datetime.now(BJ_TZ)
    start = (now - timedelta(hours=hours - 1)).replace(minute=0, second=0, microsecond=0)
    buckets = [start + timedelta(hours=i) for i in range(hours)]
    index = {bucket: i for i, bucket in enumerate(buckets)}
    tokens = [0] * hours
    errors: dict[str, list[int]] = {}
    last_error: dict[str, str] = {}
    for row in rows:
        try:
            at = datetime.fromisoformat(row["started_at"]).astimezone(BJ_TZ)
        except (ValueError, TypeError):
            continue
        position = index.get(at.replace(minute=0, second=0, microsecond=0))
        if position is None:
            continue
        tokens[position] += int(row.get("tokens") or 0)
        if not row.get("ok"):
            error_type = row.get("error") or "UnknownError"
            counts = errors.setdefault(error_type, [0] * hours)
            counts[position] += 1
            last_error[error_type] = row["started_at"]
    ranked = sorted(
        errors.items(), key=lambda item: sum(item[1]), reverse=True
    )
    return {
        "hours": hours,
        "labels": [bucket.strftime("%m-%d %H时") for bucket in buckets],
        "tokens": tokens,
        "total_tokens": sum(tokens),
        "total_errors": sum(sum(counts) for counts in errors.values()),
        "error_types": [
            {
                "type": error_type,
                "counts": counts,
                "total": sum(counts),
                "last_seen": last_error.get(error_type, ""),
            }
            for error_type, counts in ranked
        ],
    }


def _config_view(env_path: str | None) -> dict:
    """Re-read the env file at request time, so model switches show up here
    immediately after restart — without touching checkpoints or options."""
    if not env_path:
        return {"env_path": "", "note": "未指定 --env（离线演示模式，无模型配置）。", "providers": []}
    if not Path(env_path).exists():
        return {"env_path": env_path, "note": "配置文件不存在。", "providers": []}
    try:
        cfg = Config.load(env_path)
    except Exception as exc:
        return {
            "env_path": env_path,
            "note": f"配置读取失败：{type(exc).__name__}",
            "providers": [],
        }
    providers = [
        {
            "name": "出题 / 快评 / 追问",
            "model": cfg.questioner_model,
            "base_url": cfg.questioner_base_url,
            "key": mask_secret(cfg.questioner_api_key),
            "key_set": bool(cfg.questioner_api_key),
        },
        {
            "name": "单题评分",
            "model": cfg.evaluator_model,
            "base_url": cfg.evaluator_base_url,
            "key": mask_secret(cfg.evaluator_api_key),
            "key_set": bool(cfg.evaluator_api_key),
        },
    ]
    return {
        "env_path": str(env_path),
        "note": "",
        "providers": providers,
        "timeout": cfg.timeout,
        "token_budget": cfg.token_budget,
    }


def _collect_system(data_dir: Path) -> dict:
    """All psutil / os probing in one sync function, run in a worker thread."""
    info: dict = {"errors": []}

    def put(key, fn, default=None):
        try:
            info[key] = fn()
        except Exception as exc:
            info["errors"].append(f"{key}: {type(exc).__name__}: {exc}")
            info[key] = default

    put("host", lambda: {
        "name": os.uname().nodename,
        "system": f"{platform.system()} {platform.release()}",
        "kernel": platform.version(),
        "arch": platform.machine(),
        "boot_uptime": human_duration(time.time() - psutil.boot_time()),
    })
    put("cpu", lambda: {
        "count": psutil.cpu_count(logical=True),
        "percent": psutil.cpu_percent(interval=0.3),
        "load": os.getloadavg(),
    })
    put("memory", lambda: {
        "total": psutil.virtual_memory().total,
        "used": psutil.virtual_memory().used,
        "available": psutil.virtual_memory().available,
        "percent": psutil.virtual_memory().percent,
        "swap_total": psutil.swap_memory().total,
        "swap_used": psutil.swap_memory().used,
        "swap_percent": psutil.swap_memory().percent,
    })

    def disks():
        rows = []
        for part in psutil.disk_partitions(all=False):
            if part.fstype in {"squashfs", "tmpfs", "devtmpfs", "swap"}:
                continue
            usage = psutil.disk_usage(part.mountpoint)
            rows.append({
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        return rows

    put("disks", disks)
    put("net", lambda: {
        "sent": psutil.net_io_counters().bytes_sent,
        "recv": psutil.net_io_counters().bytes_recv,
    })

    def process():
        proc = psutil.Process()
        create_time = datetime.fromtimestamp(proc.create_time(), timezone.utc).astimezone()
        fds = None
        try:
            fds = proc.num_fds()
        except (AttributeError, psutil.AccessDenied):
            pass
        connections = None
        try:
            connections = len(proc.net_connections(kind="inet"))
        except (AttributeError, psutil.AccessDenied, psutil.Error):
            pass
        return {
            "pid": proc.pid,
            "started_at": create_time.isoformat(timespec="seconds"),
            "uptime": human_duration(time.time() - proc.create_time()),
            "rss": proc.memory_info().rss,
            "threads": proc.num_threads(),
            "fds": fds,
            "connections": connections,
            "cwd": str(proc.cwd()),
            "cmdline": " ".join(proc.cmdline()),
            "python": sys.version.split()[0],
        }

    put("process", process)

    def data_files():
        rows = []
        if data_dir.exists():
            for path in sorted(data_dir.iterdir()):
                if path.is_file():
                    rows.append({"name": path.name, "size": path.stat().st_size})
        return rows

    put("data_files", data_files)
    return info


async def collect_server_info(service) -> dict:
    system = await asyncio.to_thread(_collect_system, service.data_dir)
    stats = await service.store.global_stats()
    passes = await service.store.list_passes()
    # The page shows quota and validity, never the credential itself.
    for row in passes:
        row.pop("token", None)
        row.pop("token_hash", None)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ACTIVITY_HOURS)).isoformat()
    window = await service.store.metrics_window(cutoff)
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "demo": bool(service.options_base.get("demo", False)),
        "secure_cookies": service.secure_cookies,
        "config": _config_view(service.env_path),
        "git": _git_info(),
        "units": _service_units(),
        "logs": _recent_logs(),
        "stats": stats,
        "passes": passes,
        "activity": _activity_view(window),
        **system,
    }


# ---------------------------------------------------------------------------
# Rendering. The page is fully server-side HTML: no JS, no fetch, no writes.
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root{--bg:#f5f7f8;--surface:#fff;--soft:#fafbfc;--line:#e2e8ea;--strong:#cfd9dc;
--ink:#1f2930;--muted:#66727c;--faint:#8a959d;--accent:#0f766e;--accent-soft:#e8f4f2;
--warn:#a16207;--warn-soft:#fdf7e8;--danger:#b42318;--danger-soft:#fdf0ee;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
font-size:14px;line-height:1.65}
.wrap{max-width:1060px;margin:0 auto;padding:24px 22px 56px}
.top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:600}
.mark{width:9px;height:9px;border-radius:3px;background:var(--accent);display:inline-block}
.readonly{font-size:12px;color:var(--warn);background:var(--warn-soft);
border:1px solid #eddfb6;border-radius:6px;padding:3px 9px}
.meta{color:var(--faint);font-size:12px}
.meta a{color:var(--accent);text-decoration:none}
.meta a:hover{text-decoration:underline}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:15px;font-weight:600;margin:0 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--soft);border:1px solid var(--line);border-radius:7px;padding:5px 11px;font-size:12px;color:var(--muted)}
.chip b{color:var(--ink);font-weight:500;margin-left:5px}
.chip.bad b{color:var(--danger)}
.chip.good b{color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:360px}
th{color:var(--faint);font-weight:500;background:var(--soft)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.ok{color:var(--accent)}.fail{color:var(--danger)}.faint{color:var(--faint)}
.bar{height:7px;border-radius:4px;background:var(--soft);border:1px solid var(--line);overflow:hidden;margin-top:4px;max-width:260px}
.bar>i{display:block;height:100%;background:var(--accent)}
.bar>i.hot{background:var(--danger)}
pre{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:12px;
overflow-x:auto;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}
.warnbox{background:var(--warn-soft);border:1px solid #eddfb6;color:#7a5209;
border-radius:8px;padding:9px 12px;font-size:12.5px;margin-top:8px}
.sub{color:var(--muted);font-size:12.5px;margin:0 0 4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.grid .card{margin-bottom:0}
.chart{border-bottom:1px solid var(--line)}
.cols{display:flex;align-items:stretch;height:140px;margin-top:6px}
.col{flex:1;display:flex;flex-direction:column;min-width:0;cursor:default}
.colbars{flex:1;display:flex;flex-direction:column;justify-content:flex-end}
.colbars i{display:block}
.lbl{height:15px;font-size:9px;color:var(--faint);text-align:center;white-space:nowrap;overflow:hidden}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:10px 0 2px}
.dot{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:5px;vertical-align:-1px}
h1{font-size:0;margin:0}
@media(max-width:680px){.grid{grid-template-columns:1fr}}
"""


def _card(title: str, inner: str, sub: str = "") -> str:
    sub_html = f'<p class="sub">{esc(sub)}</p>' if sub else ""
    return f'<div class="card"><h2>{esc(title)}</h2>{sub_html}{inner}</div>'


def _chips(pairs: list[tuple]) -> str:
    """(label, value, tone) → chips; tone in {good, bad, ""}."""
    chips = "".join(
        f'<span class="chip {tone}">{esc(label)}<b>{esc(value)}</b></span>'
        for label, value, tone in pairs
    )
    return f'<div class="chips">{chips}</div>'


def _bar(percent: float) -> str:
    width = max(0.0, min(100.0, percent))
    hot = " hot" if width >= 85 else ""
    return f'<div class="bar"><i class="{hot}" style="width:{width:.1f}%"></i></div>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="faint" style="font-size:12.5px">暂无数据</p>'
    head = "".join(
        f'<th class="num">{esc(h)}</th>' if h.startswith("#") else f"<th>{esc(h)}</th>"
        for h in headers
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td class="num">{cell}</td>' if headers[i].startswith("#") else f"<td>{cell}</td>"
            for i, cell in enumerate(row)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _pass_state(row: dict) -> str:
    if row["revoked"]:
        return '<span class="fail">已作废</span>'
    now = datetime.now(timezone.utc).isoformat()
    if row["expires_at"] <= now:
        return '<span class="fail">已过期</span>'
    if row["kind"] == "temp" and row["first_used_at"] is None:
        return '<span class="faint">未激活（首用起计时）</span>'
    return '<span class="ok">有效</span>'


def _kind_label(kind: str) -> str:
    return "长期" if kind == "long" else "临时"


def _quota_text(row: dict) -> str:
    if row["kind"] == "long" or row["quota_tokens"] is None:
        return "不限"
    return f"{row['used_tokens']:,} / {row['quota_tokens']:,}"


def _format_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    return iso[:19].replace("T", " ")


def _stacked_chart(
    series: list[list[int]],
    labels: list[str],
    colors: list[str],
    titles: list[str] | None = None,
    label_every: int = 6,
) -> str:
    """Server-side stacked bar chart: pure CSS, one column per hourly bucket."""
    if not labels:
        return '<p class="faint" style="font-size:13px">暂无数据</p>'
    totals = [sum(values[i] for values in series) for i in range(len(labels))]
    max_total = max(totals) or 1
    columns = []
    for i, label in enumerate(labels):
        segments = "".join(
            f'<i style="height:{values[i] / max_total * 100:.2f}%;'
            f'background:{colors[si % len(colors)]}"></i>'
            for si, values in enumerate(series)
            if values[i]
        )
        text = titles[i] if titles and i < len(titles) else label
        shown = label if i % label_every == 0 else ""
        columns.append(
            f'<div class="col" title="{esc(text)}">'
            f'<div class="colbars">{segments}</div>'
            f'<span class="lbl">{esc(shown)}</span></div>'
        )
    return f'<div class="chart"><div class="cols">{"".join(columns)}</div></div>'


def render_server_page(info: dict) -> str:
    errors = info.get("errors") or []
    error_html = ""
    if errors:
        error_html = (
            '<div class="card"><h2>采集异常</h2><p class="sub">'
            "以下系统信息读取失败，不影响其余部分：</p><pre>"
            + esc("\n".join(errors))
            + "</pre></div>"
        )

    # --- 部署与运行 ---
    host = info.get("host") or {}
    process = info.get("process") or {}
    git = info.get("git") or {}
    units = info.get("units") or []
    deploy_pairs = [
        ("主机名", host.get("name", "—"), ""),
        ("操作系统", host.get("system", "—"), ""),
        ("内核", host.get("kernel", "—"), ""),
        ("架构", host.get("arch", "—"), ""),
        ("开机时长", host.get("boot_uptime", "—"), ""),
        ("运行模式", "离线演示" if info.get("demo") else "真实模型", "bad" if info.get("demo") else "good"),
        ("Cookie Secure", "启用" if info.get("secure_cookies") else "关闭", ""),
    ]
    for unit in units:
        tone = "good" if unit["state"] == "active" else "bad"
        deploy_pairs.append((unit["unit"], unit["state"], tone))
    deploy_html = _chips(deploy_pairs)

    version_rows: list[list[str]] = []
    if git:
        version_rows = [
            ["当前提交", f"<b>{esc(git.get('commit', '—'))}</b>（{esc(git.get('branch', '—'))}）"],
            ["提交说明", esc(git.get("subject", ""))],
            ["提交时间", esc(git.get("date", ""))],
            ["仓库目录", esc(git.get("root", ""))],
        ]
    process_rows = [
        ["PID", str(process.get("pid", "—"))],
        ["Python", esc(process.get("python", "—"))],
        ["进程已运行", esc(process.get("uptime", "—"))],
        ["启动时间", esc(process.get("started_at", "—"))],
        ["常驻内存 RSS", human_bytes(process.get("rss", 0))],
        ["线程数", str(process.get("threads", "—"))],
        ["打开文件数", "—" if process.get("fds") is None else str(process["fds"])],
        ["网络连接数", "—" if process.get("connections") is None else str(process["connections"])],
    ]
    deploy_card = _card(
        "部署与运行",
        deploy_html
        + (_table(["项", "值"], version_rows) if version_rows else "")
        + _table(["进程信息", "值"], process_rows),
    )

    # --- 模型配置 ---
    cfg = info.get("config") or {}
    provider_rows = [
        [
            esc(p["name"]),
            f"<b>{esc(p['model'])}</b>",
            esc(p["base_url"]),
            f'<span class="{"faint" if not p["key_set"] else ""}">{esc(p["key"])}</span>',
        ]
        for p in cfg.get("providers", [])
    ]
    config_inner = _table(["用途", "模型", "Base URL", "API Key（脱敏）"], provider_rows)
    budget = cfg.get("token_budget")
    config_chips = _chips(
        [
            ("请求超时", f"{cfg.get('timeout', '—')} s", ""),
            ("会话 token 预算", f"{budget:,}" if isinstance(budget, int) else "—", ""),
            ("配置文件", cfg.get("env_path") or "未指定", ""),
        ]
    )
    if cfg.get("note"):
        config_inner += f'<div class="warnbox">{esc(cfg["note"])}</div>'
    config_card = _card("模型配置（.env 实时读取）", config_chips + config_inner, "密钥一律脱敏显示，仅供确认配置是否生效")

    # --- 系统资源 ---
    cpu = info.get("cpu") or {}
    memory = info.get("memory") or {}
    load = cpu.get("load") or (0, 0, 0)
    sys_rows = [
        ["CPU 逻辑核数", str(cpu.get("count", "—"))],
        [
            "CPU 使用率",
            f'{cpu.get("percent", 0):.1f}%{_bar(cpu.get("percent", 0))}',
        ],
        [
            "负载（1 / 5 / 15 分钟）",
            f'{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}',
        ],
        [
            "内存",
            f'{human_bytes(memory.get("used", 0))} / {human_bytes(memory.get("total", 0))}'
            f'（可用 {human_bytes(memory.get("available", 0))}）'
            f'{_bar(memory.get("percent", 0))}',
        ],
        [
            "交换分区",
            f'{human_bytes(memory.get("swap_used", 0))} / {human_bytes(memory.get("swap_total", 0))}'
            f'{_bar(memory.get("swap_percent", 0))}',
        ],
    ]
    net = info.get("net") or {}
    sys_rows += [
        ["累计网络流量", f"收 {human_bytes(net.get('recv', 0))} · 发 {human_bytes(net.get('sent', 0))}"],
    ]
    disk_rows = [
        [
            esc(d["mount"]),
            esc(d["fstype"]),
            f'<span class="num">{human_bytes(d["used"])}</span>',
            f'<span class="num">{human_bytes(d["free"])}</span>',
            f'{d["percent"]:.1f}%{_bar(d["percent"])}',
        ]
        for d in info.get("disks", [])
    ]
    data_rows = [
        [f"<code>{esc(f['name'])}</code>", f'<span class="num">{human_bytes(f["size"])}</span>']
        for f in info.get("data_files", [])
    ]
    sys_card = _card(
        "系统资源",
        _table(["项", "值"], sys_rows)
        + '<h2 style="margin-top:16px">磁盘</h2>'
        + _table(["挂载点", "类型", "#已用", "#可用", "#使用率"], disk_rows)
        + '<h2 style="margin-top:16px">数据目录</h2>'
        + _table(["文件", "#大小"], data_rows),
        sub="数据目录即 --data-dir，SQLite 全部文件（含 WAL/SHM）都在这里",
    )

    # --- 业务数据 ---
    stats = info.get("stats") or {}
    stat_pairs = [
        ("会话", f"{stats.get('sessions', 0):,}", ""),
        ("用户", f"{stats.get('users', 0):,}", ""),
        ("界面事件", f"{stats.get('events', 0):,}", ""),
        ("模型调用", f"{stats.get('metric_calls', 0):,}", ""),
        ("累计 token", f"{stats.get('total_tokens', 0):,}", ""),
        ("失败调用", f"{stats.get('metric_failures', 0):,}", "bad" if stats.get("metric_failures") else ""),
        ("令牌有效 / 全部", f"{stats.get('passes_active', 0)} / {stats.get('passes_total', 0)}", ""),
        ("令牌未激活", f"{stats.get('passes_unused', 0)}", ""),
    ]
    pass_rows = [
        [
            str(row["id"]),
            _kind_label(row["kind"]),
            esc(row["label"] or "—"),
            _quota_text(row),
            _format_ts(row["first_used_at"]),
            _format_ts(row["expires_at"]),
            _pass_state(row),
        ]
        for row in info.get("passes", [])
    ]
    data_card = _card(
        "业务数据",
        _chips(stat_pairs)
        + '<h2 style="margin-top:16px">访问令牌</h2>'
        + _table(
            ["ID", "类型", "备注", "#配额 / 已用", "首次使用", "有效期至", "状态"],
            pass_rows,
        ),
        sub="令牌本体与哈希一律不在此页显示；会话只显示末 14 位",
    )

    # --- 按小时的 token / 错误看板 ---
    activity = info.get("activity") or {}
    labels = activity.get("labels", [])
    hours = activity.get("hours", ACTIVITY_HOURS)
    window_note = f"北京时间 · 最近 {hours} 小时 · 悬停可见每小时明细"

    token_values = activity.get("tokens", [])
    token_titles = [
        f"{label} · {token_values[i]:,} token" if i < len(token_values) else label
        for i, label in enumerate(labels)
    ]
    token_card = _card(
        "Token 消耗",
        _stacked_chart([token_values], labels, [CHART_COLORS[0]], titles=token_titles)
        + _chips(
            [
                ("窗口内合计", f"{activity.get('total_tokens', 0):,} token", ""),
                ("窗口内调用错误", f"{activity.get('total_errors', 0)} 次", ""),
            ]
        ),
        sub=window_note,
    )

    error_types = activity.get("error_types", [])
    if error_types:
        error_series = [entry["counts"] for entry in error_types]
        error_titles = [
            " · ".join(
                f"{entry['type']} {entry['counts'][i]} 次"
                for entry in error_types
                if entry["counts"][i]
            ) or f"{label} · 无错误"
            for i, label in enumerate(labels)
        ]
        legend = "".join(
            f'<span class="dot" style="background:{CHART_COLORS[i % len(CHART_COLORS)]}"></span>'
            f'{esc(entry["type"])}（{entry["total"]} 次）'
            for i, entry in enumerate(error_types)
        )
        error_inner = (
            _stacked_chart(
                error_series,
                labels,
                [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(error_types))],
                titles=error_titles,
            )
            + f'<div class="legend">{legend}</div>'
            + _table(
                ["错误类型", "#合计", "最近一次"],
                [
                    [
                        f'<span class="fail">{esc(entry["type"])}</span>',
                        f'<span class="num">{entry["total"]}</span>',
                        esc(_format_ts(entry["last_seen"])),
                    ]
                    for entry in error_types
                ],
            )
        )
    else:
        error_inner = '<p class="faint" style="font-size:13px">窗口内没有 API 错误</p>'
    error_card = _card("API 错误（区分类型）", error_inner, sub=window_note)

    # --- 日志 ---
    logs = info.get("logs") or {}
    log_html = ""
    if logs.get("note"):
        log_html += f'<div class="warnbox">{esc(logs["note"])}</div>'
    if logs.get("lines"):
        log_html += "<pre>" + esc("\n".join(logs["lines"])) + "</pre>"
    log_card = _card(f"最近日志（journalctl，最后 {LOG_LINES} 行）", log_html or '<p class="faint" style="font-size:12.5px">暂无日志输出</p>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>服务状态 · Mock Interview</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><span class="mark"></span>服务状态</div>
    <div class="meta">
      <span class="readonly">只读 · 每页自动刷新（{REFRESH_SECONDS}s）</span>
      生成于 {esc(info.get("generated_at", "—"))}
      · <a href="/">返回面试主页</a>
    </div>
  </div>
  {deploy_card}
  <div class="grid">
    {config_card}
    {sys_card}
  </div>
  {data_card}
  <div class="grid">
    {token_card}
    {error_card}
  </div>
  {log_card}
  {error_html}
</div>
</body>
</html>"""


DENIED_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>需要访问令牌</title>
<style>{css}</style></head>
<body><div class="wrap">
<div class="card">
  <h2>需要访问令牌</h2>
  <p class="sub">服务状态页与 API 一样，凭访问令牌进入。</p>
  <p class="sub">请先从 <a href="/">主页登录</a>（支持带 ?pass= 的链接），再回到 /server。</p>
</div>
</div></body></html>""".replace("{css}", _PAGE_CSS)
