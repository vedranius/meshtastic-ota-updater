const BASE = "https://release.meshtastic.org";

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const version = (url.searchParams.get("version") || "").replace(/^v/, "");
  const board = url.searchParams.get("board") || "";
  if (!/^[0-9A-Za-z._-]{1,64}$/.test(version) || !/^[a-z0-9_-]{1,80}$/.test(board)) {
    return Response.json({ error: "Invalid firmware version or board target." }, { status: 400 });
  }
  const manifestUrl = `${BASE}/${version}/firmware-${board}-${version}.mt.json`;
  const upstream = await fetch(manifestUrl, { headers: { Accept: "application/json" }, cf: { cacheTtl: 900 } });
  if (!upstream.ok) return Response.json({ error: `No official firmware manifest found for ${board} ${version}.` }, { status: 404 });
  const manifest = await upstream.json();
  // Wi-Fi Unified OTA receives the Meshtastic application image. The app1
  // mt-*-ota.bin artifact is the small OTA loader used for serial partition
  // flashing; offering it as an OTA update payload installs the wrong image.
  const images = (manifest.files || []).filter(file => file.part_name === "app0");
  if (!images.length) return Response.json({ error: "The official target manifest has no OTA image." }, { status: 404 });
  const otaPart = (manifest.part || []).find(part => part.name === "app1" || part.subtype === "ota_1");
  const appPart = (manifest.part || []).find(part => part.name === "app0" || part.subtype === "ota_0");
  const otaFile = (manifest.files || []).find(file => file.part_name === "app1" && /^mt-esp32s3-ota\.bin$/i.test(file.name || ""));
  const otaRepairPartition = manifest.architecture === "esp32-s3" && otaPart && appPart && otaFile ? {
    name: "app1",
    type: otaPart.type,
    subtype: otaPart.subtype,
    offset: otaPart.offset,
    size: otaPart.size,
    image: otaFile.name,
    imageBytes: otaFile.bytes,
    app0Offset: appPart.offset,
    app0Size: appPart.size,
  } : null;
  return Response.json({
    version: manifest.version,
    board: manifest.platformioTarget,
    displayName: manifest.displayName,
    hwModelSlug: manifest.hwModelSlug,
    architecture: manifest.architecture,
    otaRepairPartition,
    images,
  }, { headers: { "Cache-Control": "public, max-age=60", "X-Content-Type-Options": "nosniff" } });
}
