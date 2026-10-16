const API = "https://api.meshtastic.org/github/firmware/list";

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const channel = url.searchParams.get("channel") === "beta" ? "beta" : "stable";
  const upstream = await fetch(API, { headers: { Accept: "application/json" }, cf: { cacheTtl: 900 } });
  if (!upstream.ok) return Response.json({ error: "Official Meshtastic firmware releases are temporarily unavailable." }, { status: 502 });
  const body = await upstream.json();
  const stable = body.releases?.stable || [];
  const alpha = channel === "beta" ? (body.releases?.alpha || []) : [];
  const items = [...alpha, ...stable].map(r => ({
    tag: r.id,
    name: r.title || r.id,
    pageUrl: r.page_url,
    prerelease: alpha.includes(r),
    releaseNotes: r.release_notes || "",
  }));
  return Response.json({ channel, releases: items }, {
    headers: { "Cache-Control": "public, max-age=900", "X-Content-Type-Options": "nosniff" },
  });
}
