export async function onRequestGet({ request }) {
  const id = new URL(request.url).searchParams.get("asset") || "";
  if (!/^\d{1,20}$/.test(id)) return Response.json({ error: "Invalid firmware asset." }, { status: 400 });
  const upstream = await fetch(`https://api.github.com/repos/meshtastic/firmware/releases/assets/${id}`, {
    headers: { Accept: "application/octet-stream", "User-Agent": "meshtastic-cloudflare-flasher" },
    cf: { cacheTtl: 3600, cacheEverything: true },
  });
  if (!upstream.ok) return Response.json({ error: "Could not download the official firmware archive." }, { status: 502 });
  const disposition = upstream.headers.get("Content-Disposition") || `attachment; filename="meshtastic-firmware-${id}.zip"`;
  return new Response(upstream.body, { headers: {
    "Content-Type": "application/zip",
    "Content-Disposition": disposition,
    "Cache-Control": "public, max-age=3600",
    "X-Content-Type-Options": "nosniff",
  } });
}
