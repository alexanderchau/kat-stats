// Cloudflare Pages Function — proxies pipeline trigger with server-side token.
// Token stored in Cloudflare env var PIPELINE_TOKEN (set in dashboard).

export async function onRequestPost({ env }) {
  const API = 'https://kat-stats-api.chau.org';
  const token = env.PIPELINE_TOKEN;
  if (!token) {
    return new Response(JSON.stringify({ error: 'PIPELINE_TOKEN not configured' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const resp = await fetch(`${API}/trigger`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` },
  });

  return new Response(resp.body, {
    status: resp.status,
    headers: { 'Content-Type': 'application/json' },
  });
}
