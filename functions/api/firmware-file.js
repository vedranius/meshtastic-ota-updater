const BASE = "https://release.meshtastic.org";

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const version = (url.searchParams.get("version") || "").replace(/^v/, "");
  const board = url.searchParams.get("board") || "";
  const file = url.searchParams.get("file") || "";
  if (!/^[0-9A-Za-z._-]{1,64}$/.test(version) || !/^[a-z0-9_-]{1,80}$/.test(board) || !/^firmware-[A-Za-z0-9._-]+\.bin$/i.test(file)) {
    return Response.json({ error: "Invalid firmware image." }, { status: 400 });
  }
  // Unified Wi-Fi OTA expects the main app0 application image. The smaller
  // mt-*-ota.bin image is an OTA loader for a partitioned serial flash and
  // cannot be used as the update payload sent to TCP/3232.
  const manifestUrl = `${BASE}/${version}/firmware-${board}-${version}.mt.json`;
  const manifestResponse = await fetch(manifestUrl, { headers: { Accept: "application/json" }, cf: { cacheTtl: 900 } });
  if (!manifestResponse.ok) return Response.json({ error: "The official target manifest is unavailable." }, { status: 404 });
  const manifest = await manifestResponse.json();
  const selected = (manifest.files || []).find(item => item.name === file && item.part_name === "app0");
  if (!selected) return Response.json({ error: "That file is not the official application image for this board and release." }, { status: 400 });
  const upstream = await fetch(`${BASE}/${version}/${encodeURIComponent(file)}`, { cf: { cacheTtl: 3600 } });
  if (!upstream.ok) return Response.json({ error: "The official OTA image is unavailable." }, { status: 404 });
  return new Response(upstream.body, { headers: {
    "Content-Type": "application/octet-stream",
    "Content-Disposition": `attachment; filename="${file}"`,
    "Cache-Control": "public, max-age=3600",
    "X-Content-Type-Options": "nosniff",
  } });
}
