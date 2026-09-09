import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import path from "path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DATAV = "https://geo.datav.aliyun.com/areas_v3/bound";

export async function GET(
    _req: NextRequest,
    ctx: { params: { adcode: string } },
) {
    const raw = (ctx.params.adcode || "").replace(/[^0-9]/g, "");
    if (!raw) {
        return NextResponse.json({ error: "invalid adcode" }, { status: 400 });
    }
    const adcode = raw;

    // Prefer local cache under public/geo for China provinces
    const localPath = path.join(process.cwd(), "public", "geo", `${adcode}_full.json`);
    try {
        const buf = await readFile(localPath, "utf8");
        return new NextResponse(buf, {
            status: 200,
            headers: {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "public, max-age=86400",
            },
        });
    } catch {
        /* fall through to DataV */
    }

    try {
        const res = await fetch(`${DATAV}/${adcode}_full.json`, {
            // Revalidate daily
            next: { revalidate: 86400 },
        });
        if (!res.ok) {
            return NextResponse.json(
                { error: `upstream ${res.status}` },
                { status: res.status === 404 ? 404 : 502 },
            );
        }
        const body = await res.text();
        return new NextResponse(body, {
            status: 200,
            headers: {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "public, max-age=3600",
            },
        });
    } catch (e) {
        return NextResponse.json(
            { error: e instanceof Error ? e.message : "fetch failed" },
            { status: 502 },
        );
    }
}
