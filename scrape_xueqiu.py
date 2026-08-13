#!/usr/bin/env python3
"""
雪球博主打分数据抓取脚本
=============================
在 Mac 终端运行：
    python3 -m pip install DrissionPage
    python3 scrape_xueqiu.py

核心思路：
  用本地 Chrome 访问雪球（绕过 WAF），
  然后直接在浏览器里用 XHR 调用雪球 API（浏览器自带 Cookie，不需要手动传）。
"""

import json
import re
import time
import os
import sys
from datetime import datetime
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────
USER_ID    = "1811437308"
MAX_PAGES  = 50
PAGE_DELAY = 0.8
OUTPUT_DIR = Path.home() / "Desktop" / "xueqiu"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
JSON_OUT   = OUTPUT_DIR / "xueqiu_data.json"

MAC_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
# ──────────────────────────────────────────────────────


def find_chrome():
    for p in MAC_CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def open_browser():
    """启动 Chrome，导航到雪球用户页（处理 WAF 跳转）"""
    from DrissionPage import ChromiumPage, ChromiumOptions

    opts = ChromiumOptions()
    chrome = find_chrome()
    if chrome:
        print(f"   Chrome：{chrome}")
        opts.set_browser_path(chrome)
    opts.headless(False)

    page = ChromiumPage(addr_or_opts=opts)

    print("   正在打开雪球首页（WAF 验证）…")
    try:
        page.get("https://xueqiu.com")
    except Exception:
        pass
    time.sleep(5)

    print(f"   正在打开目标用户页…")
    try:
        page.get(f"https://xueqiu.com/u/{USER_ID}")
    except Exception:
        pass
    time.sleep(6)

    # 检测是否需要登录
    try:
        cur_url = page.url or ""
    except Exception:
        cur_url = ""
    if "login" in cur_url:
        print("\n⚠️  检测到需要登录！请在弹出的 Chrome 窗口中手动登录雪球。")
        input("登录完成后按 Enter 继续…")
        time.sleep(3)

    return page


def xhr_get(page, url: str):
    """在浏览器内用同步 XHR 调用接口（自动携带 Cookie，完全绕过跨域问题）"""
    js = f"""
    try {{
        var xhr = new XMLHttpRequest();
        xhr.open('GET', {json.dumps(url)}, false);
        xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.send();
        if (xhr.status === 200 && xhr.getResponseHeader('Content-Type') &&
            xhr.getResponseHeader('Content-Type').indexOf('json') !== -1) {{
            return xhr.responseText;
        }}
        return '__STATUS_' + xhr.status;
    }} catch(e) {{
        return '__ERR_' + e.toString();
    }}
    """
    try:
        result = page.run_js(js)
    except Exception as e:
        return None

    if not result or not isinstance(result, str):
        return None
    if result.startswith('__'):
        return None

    try:
        return json.loads(result)
    except Exception:
        return None


def fetch_all_posts(page) -> list:
    """分页拉取所有帖子"""
    all_posts = []
    p = 1

    print(f"\n📥 开始分页拉取帖子（最多 {MAX_PAGES} 页）…")
    while p <= MAX_PAGES:
        url = (
            f"https://xueqiu.com/v4/statuses/user_timeline.json"
            f"?user_id={USER_ID}&page={p}&count=20&type=original"
        )
        data = xhr_get(page, url)

        if not data:
            print(f"\n   ⚠️  第 {p} 页请求失败，停止")
            break

        statuses = data.get("statuses", [])
        if not statuses:
            print(f"\n   第 {p} 页无内容，结束")
            break

        all_posts.extend(statuses)
        max_page = data.get("maxPage", 1)
        print(f"   第 {p}/{max_page} 页，累计 {len(all_posts)} 条", end="\r")

        if p >= max_page:
            break
        p += 1
        time.sleep(PAGE_DELAY)

    print(f"\n   共抓取 {len(all_posts)} 条帖子")
    return all_posts


# ── 打分解析（1-10 分制）─────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# 该博主帖子格式：资产名 分数（空格分隔，1-10小数分）
# 例："纳斯达克 7.35 红利低波 4.81 沪深300 5.35 WTI原油 4.61 中证2000 5.64"
# 资产名：中文或大写字母开头，可混含中英文数字，2-8字符
SCORE_RE = re.compile(
    r"([A-Z一-龥][A-Za-z一-龥\d]{1,7})\s+(\d+\.\d+)"
)


def parse_scores(raw_text: str) -> list:
    text = strip_html(raw_text)
    results = []
    seen = set()

    for m in SCORE_RE.finditer(text):
        name = m.group(1).strip()
        try:
            score = float(m.group(2))
        except ValueError:
            continue
        # 只接受 0.5-10 分
        if not (0.5 <= score <= 10):
            continue
        # 排除数字开头（误匹配日期等）
        if name[0].isdigit():
            continue
        key = (name, round(score, 2))
        if key in seen:
            continue
        seen.add(key)
        results.append({"name": name, "score": score, "raw": m.group(0)})

    return results


def process_posts(posts: list) -> list:
    records = []
    for p in posts:
        raw = p.get("text", "") or p.get("title", "") or ""
        title   = p.get("title") or strip_html(raw)[:50]
        created = p.get("created_at", 0)
        dt = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d") if created else ""
        uid = p.get("user", {}).get("id", USER_ID)
        pid = p.get("id", "")
        scores = parse_scores(raw)

        records.append({
            "id":       pid,
            "title":    title,
            "date":     dt,
            "url":      f"https://xueqiu.com/{uid}/{pid}",
            "text":     strip_html(raw)[:400],
            "scores":   scores,
            "has_score": bool(scores),
            "avg_score": round(sum(s["score"] for s in scores) / len(scores), 2) if scores else None,
            "likes":    p.get("like_count", 0),
            "comments": p.get("comments_count", 0),
            "reposts":  p.get("reposts_count", 0),
        })
    return records


# ── Main ──────────────────────────────────────────────
def main():
    print("=" * 52)
    print(" 雪球量化打分数据抓取工具")
    print(f" 目标用户：{USER_ID}")
    print("=" * 52)

    try:
        from DrissionPage import ChromiumPage  # noqa: F401
    except ImportError:
        print("❌ 请先安装：python3 -m pip install DrissionPage")
        sys.exit(1)

    print("🌐 正在启动 Chrome…")
    page = open_browser()

    posts = fetch_all_posts(page)

    try:
        page.quit()
    except Exception:
        pass

    if not posts:
        print("❌ 未抓到任何帖子")
        sys.exit(1)

    records = process_posts(posts)
    scored  = [r for r in records if r["has_score"]]

    print(f"\n📊 解析结果：")
    print(f"   总帖子：{len(records)} 条")
    print(f"   含打分：{len(scored)} 条")

    # 汇总各资产最新分
    asset_latest = {}
    for r in sorted(records, key=lambda x: x["date"]):
        for s in r["scores"]:
            asset_latest[s["name"]] = {"score": s["score"], "date": r["date"]}
    if asset_latest:
        print("   最新打分：")
        for name, info in asset_latest.items():
            print(f"     {name}：{info['score']}（{info['date']}）")

    output = {
        "user_id":      USER_ID,
        "fetched_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total":        len(records),
        "scored_count": len(scored),
        "posts":        records,
    }

    JSON_OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已保存至：{JSON_OUT}")
    print("   用浏览器打开 xueqiu_dashboard.html，点击「加载/更新数据」选择此 JSON 即可。")


if __name__ == "__main__":
    main()
