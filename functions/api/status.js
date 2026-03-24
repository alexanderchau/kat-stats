// Cloudflare Pages Function — proxies pipeline status (no auth needed, but keeps API URL server-side).

export async function onRequestGet() {
  const API = 'https://kat-stats-api.chau.org';

  try {
    const resp = await fetch(`${API}/status`);
    return new Response(resp.body, {
      status: resp.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (e) {
    return new Response(JSON.stringify({ running: false, error: e.message }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
