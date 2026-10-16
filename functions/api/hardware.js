const API = "https://api.meshtastic.org/resource/deviceHardware";

export async function onRequestGet() {
  const upstream = await fetch(API, {
    headers: { Accept: "application/json", "User-Agent": "meshtastic-ota-studio" },
    cf: { cacheTtl: 3600, cacheEverything: true },
  });
  if (!upstream.ok) {
    return Response.json({ error: "The official Meshtastic board catalog is temporarily unavailable." }, { status: 502 });
  }
  const catalog = await upstream.json();
  return Response.json({ devices: catalog }, {
    headers: { "Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff" },
  });
}
