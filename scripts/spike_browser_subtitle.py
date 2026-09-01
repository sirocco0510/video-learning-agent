"""Spike:验证 Puppeteer/playwright + page.evaluate(fetch) 通道,
并演示 dump 字幕到 .srt 文件的完整代码路径。

即使没登录态 subtitles=[],也用 mock JSON 走完 parse → dump,
让你看到真实运行时的 .srt 长什么样。

用法:
  1. Chrome 9222 已在跑(独立 user-data-dir=/tmp/vla-chrome-debug)
  2. uv run python scripts/spike_browser_subtitle.py
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.bilibili.com/video/BV1dzui69EYV/"
BVID = "BV1dzui69EYV"
DUMP_PATH = Path("/tmp/vla_spike_subtitle.srt")


def seconds_to_srt_time(t: float) -> str:
    """把秒数转 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def dump_body_to_srt(body: list[dict], out_path: Path) -> int:
    """把 B站字幕 body[] 转 SRT 文件,返回写入条数。"""
    lines = []
    for i, item in enumerate(body, start=1):
        start = seconds_to_srt_time(float(item["from"]))
        end = seconds_to_srt_time(float(item["to"]))
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(item["content"])
        lines.append("")  # blank separator
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return len(body)


async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print("❌ 连不上 Chrome 9222")
            print("   请确认 Chrome 已启动: --user-data-dir=/tmp/vla-chrome-debug --remote-debugging-port=9222")
            sys.exit(1)

        # ★ 强制新建后台标签页,不复用现有 — 避免抢用户焦点
        if browser.contexts:
            ctx = browser.contexts[0]
        else:
            ctx = await browser.new_context()
        page = await ctx.new_page()
        # 后台标签页,不抢焦点
        print("✓ 新建后台标签页(不抢焦点)")

        # 1) goto B站
        print(f"\n→ goto {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        title = await page.title()
        print(f"  页面 title: {title[:60] if title else '(none)'}")

        # 2) fetch view API
        print(f"\n→ Step 1: fetch /x/web-interface/view?bvid={BVID}")
        view = await page.evaluate(
            """async (bvid) => {
                const r = await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`, {credentials: 'include'});
                return await r.json();
            }""",
            BVID,
        )
        print(f"  code = {view.get('code')}")
        cid = (view.get("data") or {}).get("cid")
        print(f"  cid  = {cid}")

        # 3) fetch player/v2
        print(f"\n→ Step 2: fetch /x/player/v2?bvid={BVID}&cid={cid}")
        player = await page.evaluate(
            """async ({bvid, cid}) => {
                const r = await fetch(`https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${cid}`, {credentials: 'include'});
                return await r.json();
            }""",
            {"bvid": BVID, "cid": cid},
        )
        print(f"  code = {player.get('code')}")
        subs = (player.get("data") or {}).get("subtitle", {}).get("subtitles", [])
        print(f"  subtitles count = {len(subs)}")

        body = None
        if subs:
            # debug: dump 全部 subs
            print(f"  全部 subtitles 列表:")
            for i, s in enumerate(subs):
                print(f"    [{i}] {json.dumps(s, ensure_ascii=False)}")
            chosen = subs[0]
            sub_url = chosen.get("subtitle_url") or chosen.get("subtitle_url_v2") or ""
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            print(f"\n→ Step 3: context.request.get {sub_url[:60]}")
            response = await ctx.request.get(sub_url)
            print(f"  status = {response.status}")
            body_data = await response.json()
            body = body_data.get("body", [])
            print(f"  body[] 长度 = {len(body)}")
            # 也记录第一个字幕的元数据
            for s in subs:
                print(f"    字幕条目: lan={s.get('lan')} lan_doc={s.get('lan_doc')} ai_status={s.get('ai_status')}")
        else:
            print("\n→ Step 3 (真实): 无字幕可取(独立 profile 没登录 B站)")
            print("           演示用 mock body 走 dump 路径...")

        # 5) dump 到 .srt(无论真字幕还是 mock 都演示)
        if not body:
            body = [
                {"from": 0.0,  "to": 3.5,  "content": "[mock] 你好,这是模拟字幕第一条"},
                {"from": 3.5,  "to": 7.0,  "content": "[mock] 第二条字幕内容,用于演示 dump 格式"},
                {"from": 7.0,  "to": 11.0, "content": "[mock] 第三条,SRT 标准格式 HH:MM:SS,mmm"},
                {"from": 11.0, "to": 15.5, "content": "[mock] 第四条,验证换行 + 编码正确"},
            ]

        n = dump_body_to_srt(body, DUMP_PATH)
        print(f"\n✓ dumped {n} 条字幕到 {DUMP_PATH}")
        print(f"  文件大小: {DUMP_PATH.stat().st_size} bytes")

        # 6) show first 30 lines
        print("\n--- .srt 前 30 行 ---")
        content = DUMP_PATH.read_text(encoding="utf-8")
        for line in content.splitlines()[:30]:
            print(line)
        if len(content.splitlines()) > 30:
            print(f"...({len(content.splitlines())} 行 total)")

        await page.close()
        print("\n✓ 后台标签页已关闭")


if __name__ == "__main__":
    asyncio.run(main())
