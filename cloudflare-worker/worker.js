/**
 * Cloudflare Worker — Discord Slash Command → GitHub Actions trigger
 *
 * Receives /ticker SYMBOL slash commands from Discord, validates the signature,
 * fires a GitHub workflow_dispatch event for ticker-lookup.yml, and acks the
 * user instantly. The actual research PDF is posted to the channel by the
 * GitHub Action (which uses DISCORD_TICKER_WEBHOOK_URL).
 *
 * Required environment variables (set in Cloudflare dashboard → Worker → Settings → Variables):
 *   DISCORD_PUBLIC_KEY  — from Discord Developer Portal → General Information
 *   PAT                 — GitHub fine-grained PAT with Actions:read/write + Contents:read
 *                          (NOTE: Cloudflare blocks variable names starting with "github",
 *                           so this is named PAT instead of GITHUB_PAT)
 *   REPO                — e.g. "brownplaya239/smid-scanner"
 */

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK — worker is alive. POST a Discord interaction here.', { status: 200 });
    }

    const signature = request.headers.get('x-signature-ed25519');
    const timestamp = request.headers.get('x-signature-timestamp');
    const body      = await request.text();

    if (!signature || !timestamp) {
      return new Response('Missing signature headers', { status: 401 });
    }

    // Verify Discord's ed25519 signature
    let valid = false;
    try {
      valid = await verifyDiscordSignature(body, signature, timestamp, env.DISCORD_PUBLIC_KEY);
    } catch (e) {
      return new Response(`Signature verify error: ${e.message}`, { status: 401 });
    }
    if (!valid) {
      return new Response('Invalid signature', { status: 401 });
    }

    const interaction = JSON.parse(body);

    // Type 1 = PING (Discord verification handshake)
    if (interaction.type === 1) {
      return Response.json({ type: 1 });
    }

    // Type 2 = APPLICATION_COMMAND (slash command invocation)
    if (interaction.type === 2) {
      const cmd = interaction.data.name;

      if (cmd === 'ticker') {
        const opt = interaction.data.options?.find(o => o.name === 'symbol');
        const ticker = (opt?.value || '').toUpperCase().trim();

        if (!ticker || !/^[A-Z.\-]{1,8}$/.test(ticker)) {
          return Response.json({
            type: 4,
            data: { content: `❌ Invalid ticker symbol: \`${ticker}\``, flags: 64 } // 64 = ephemeral
          });
        }

        // Fire GitHub workflow_dispatch in the background — do NOT await
        ctx.waitUntil(triggerGitHubWorkflow(ticker, env));

        // Ack immediately (Discord requires response within 3 seconds)
        return Response.json({
          type: 4,
          data: {
            content: `🔍 **Researching ${ticker}**\nGenerating one-pager research report... PDF will arrive in **#onepager-adhoc** in ~60-90 seconds.`,
          }
        });
      }

      return Response.json({
        type: 4,
        data: { content: `Unknown command: \`/${cmd}\``, flags: 64 }
      });
    }

    return new Response('Unhandled interaction type', { status: 400 });
  }
};


// ─── Discord signature verification (ed25519) ────────────────────────────────

async function verifyDiscordSignature(body, signature, timestamp, publicKeyHex) {
  const encoder  = new TextEncoder();
  const message  = encoder.encode(timestamp + body);
  const sigBytes = hexToBytes(signature);
  const keyBytes = hexToBytes(publicKeyHex);

  const key = await crypto.subtle.importKey(
    'raw',
    keyBytes,
    { name: 'Ed25519' },
    false,
    ['verify']
  );

  return await crypto.subtle.verify('Ed25519', key, sigBytes, message);
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}


// ─── GitHub workflow_dispatch trigger ────────────────────────────────────────

async function triggerGitHubWorkflow(ticker, env) {
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/ticker-lookup.yml/dispatches`;

  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization':       `Bearer ${env.PAT}`,
      'Accept':              'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent':          'smid-scanner-discord-bot',
      'Content-Type':        'application/json',
    },
    body: JSON.stringify({
      ref:    'main',
      inputs: { ticker },
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    console.error(`GitHub dispatch failed: ${resp.status} ${text}`);
  }
}
