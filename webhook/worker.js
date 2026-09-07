/**
 * Feature 5 — Instant-reply webhook worker (Cloudflare Workers free tier).
 *
 * Meta pushes comment + DM events here the second they happen; the worker
 * replies via NVIDIA NIM (the same voice as src/ai.py) + the Instagram
 * Graph API — seconds instead of the hourly sweep's :07-past-the-hour
 * latency. The hourly GitHub Action (engage.yml) stays running as the
 * safety net for any event Meta's webhook misses.
 *
 * Deploy: see webhook/README.md (wrangler + Meta app subscription steps).
 * Every secret lives in Worker env vars (wrangler secret put / dashboard)
 * — NEVER in this file, which sits in a PUBLIC repo:
 *   IG_USER_ID, IG_ACCESS_TOKEN, NVIDIA_API_KEY, WEBHOOK_VERIFY_TOKEN,
 *   APP_SECRET (optional, enables X-Hub-Signature-256 checking),
 *   IG_HANDLE + NO_REPLY_USERS (optional, default @whoisaaniiiya),
 *   REPLY_BANK_URL (optional override of the bank location).
 *
 * Endpoints (both must answer 200 fast — Meta retries anything slower):
 *   GET  /webhook   Meta's subscription handshake: hub.mode/hub.challenge/
 *                   hub.verify_token. 200 + raw challenge text on match.
 *   POST /webhook   The events. Always 200 (Meta retries non-200 with
 *                   backoff, so a poison event would re-fire forever).
 *                   Reply work runs via ctx.waitUntil, past the response.
 */

const GRAPH_BASE = "https://graph.instagram.com/v23.0";
const NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions";

// --- the voice, ported verbatim from src/ai.py --------------------------------

const PERSONALITIES = {
  friendly:
    "You are the DM voice of a friendly Indian girl on Instagram. " +
    "Warm, chatty, a little playful. You love making new friends.",
  professional:
    "You are the DM voice of a professional Indian woman on Instagram " +
    "— a content creator. Polished, courteous, brand-safe. No flirting.",
  funny:
    "You are the DM voice of a funny Indian girl on Instagram. Witty, " +
    "teasing, meme-fluent. Light jokes welcome, never mean.",
  romantic:
    "You are the DM voice of a romantic Indian girl on Instagram. " +
    "Soft, affectionate, poetic — but tasteful and never explicit.",
  emotional:
    "You are the DM voice of a deeply empathetic Indian girl on " +
    "Instagram. You listen first, validate feelings, offer comfort and " +
    "gentle encouragement.",
};
const MIRROR_RULE =
  "Reply in the same language the user writes in (Hindi, English, " +
  "Gujarati, or romanized/Devanagari mixes — mirror them exactly).";
const GENERAL_ROTATION = ["friendly", "funny", "emotional"];
const CATEGORY_PERSONALITY = { love: "romantic", friendship: "friendly" };

// The seeded pick: we need a STABLE choice per key (a re-delivered event
// must choose the same bank line / personality as the first delivery),
// not cryptographic strength — so FNV-1a hash + mixer, fully synchronous.
function seededPick(key, n) {
  let h = 0x811c9dc5;
  for (const ch of key) h = Math.imul(h ^ ch.codePointAt(0), 0x01000193) >>> 0;
  let t = (h + 0x6d2b79f5) >>> 0;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return (((t ^ (t >>> 14)) >>> 0) % n + n) % n;
}
function personalityFor(seed, category) {
  const fixed = CATEGORY_PERSONALITY[category || ""];
  if (fixed) return fixed;
  return GENERAL_ROTATION[seededPick("personality:" + seed, GENERAL_ROTATION.length)];
}
function systemPrompt(personality) {
  const voice = PERSONALITIES[personality] || PERSONALITIES.friendly;
  return (
    voice + "\n" + MIRROR_RULE + "\n" +
    "Keep replies under 300 characters. Answer directly, no preamble.\n" +
    "You are one person, the account owner @whoisaaniiiya — never say " +
    "you are an AI, a bot, or a language model."
  );
}

// --- the reply bank (fallback when NIM is down) --------------------------------
// Fetched from the repo's own GitHub-served replies.json — the same file
// the hourly sweep reads, so instant and hourly fallback replies are drawn
// from one bank. Cached in the isolate (10 min) — a slightly stale bank
// is harmless (it's a rotating pool of lines, not facts).
let bankCache = { data: null, at: 0 };
async function loadBank(env) {
  if (bankCache.data && Date.now() - bankCache.at < 10 * 60 * 1000) {
    return bankCache.data;
  }
  try {
    const r = await fetch(
      env.REPLY_BANK_URL ||
        "https://raw.githubusercontent.com/ompatel1209/instagram-agent/main/content/replies.json");
    if (r.ok) {
      const data = await r.json();
      bankCache = { data, at: Date.now() };
    }
  } catch (e) {
    /* keep the cached or empty bank — never fatal */
  }
  return bankCache.data || { categories: [] };
}

// --- categorization + bank pick (ported from engagement.py) -------------------

function categorize(bank, text) {
  const lowered = (text || "").toLowerCase();
  for (const cat of bank.categories || []) {
    for (const kw of cat.keywords || []) {
      if (kw && lowered.includes(kw.toLowerCase())) return cat;
    }
  }
  for (const cat of bank.categories || []) {
    if (cat.key === "general") return cat;
  }
  const cats = bank.categories || [];
  return cats[cats.length - 1] ||
    { key: "general", comment_replies: [], dm_replies: [] };
}

function bankReply(bank, kind, key, seed) {
  const entries = categorize(bank, key)[kind] || [];
  if (!entries.length) return null;
  return entries[seededPick(kind + ":" + seed, entries.length)];
}

// --- NIM call (ported from src/ai.py generate_reply) ---------------------------

async function nimReply(personality, text, history, env) {
  if (!env.NVIDIA_API_KEY) return null;
  const messages = [
    { role: "system", content: systemPrompt(personality) },
    ...(history || []).map(([role, content]) => ({ role, content })),
    { role: "user", content: text },
  ];
  let r;
  try {
    r = await fetch(NIM_URL, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.NVIDIA_API_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "meta/muse-glimmer-30b",
        messages,
        max_tokens: 2000, // reasoning overhead ~300 tokens before content
        temperature: 0.8,
      }),
      // Reasoning models take 10-30s; cap at 45s so a NIM hang can't eat
      // the whole event batch (the sweep catches anything this drops).
      signal: AbortSignal.timeout(45_000),
    });
  } catch (e) {
    return null;
  }
  if (!r.ok) return null;
  const data = await r.json().catch(() => null);
  if (!data) return null;
  // Reasoning model: content exists only after reasoning completes;
  // finish_reason "length" means truncated mid-answer — scrap it.
  if (data.choices?.[0]?.finish_reason !== "stop") return null;
  return data.choices[0].message?.content || null;
}

function tidy(text, kind) {
  let t = (text || "").trim();
  if (!t) return "";
  if (kind === "comment") t = t.split(/\s+/).join(" ");
  return t.length > 2200 ? t.slice(0, 2197) + "…" : t;
}

// --- Instagram Graph API -------------------------------------------------------

async function ig(path, init) {
  const r = await fetch(GRAPH_BASE + path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.error?.message || "graph api " + r.status);
  return data;
}
// GET with the token in query params (the only place Graph accepts it for
// reads — same shape as instagram.list_comments in the Python client).
function igGet(path, token) {
  return ig(path + (path.includes("?") ? "&" : "?") +
    "access_token=" + encodeURIComponent(token));
}

function ownSet(env) {
  const names = new Set();
  for (const u of (env.NO_REPLY_USERS || "").split(",")) {
    const n = u.trim().replace(/^@/, "").toLowerCase();
    if (n) names.add(n);
  }
  const handle = (env.IG_HANDLE || "whoisaaniiiya")
    .replace(/^@/, "").toLowerCase();
  if (handle) names.add(handle);
  return names;
}
const normalize = (u) => String(u || "").replace(/^@/, "").toLowerCase();
function ownRepliedHere(replies, own) {
  return (replies || []).some((r) => own.has(normalize(r.username)));
}

// --- event handlers -----------------------------------------------------------

async function handleComment(evt, env) {
  const cid = String(evt.id || "");
  const text = String(evt.text || "");
  if (!cid || !text.trim()) return;
  const token = env.IG_ACCESS_TOKEN;
  if (!token) return;
  const own = ownSet(env);
  const username = evt.username || evt.from?.username;
  if (username && own.has(normalize(username))) return; // never self-reply

  // Live dedupe — the SAME edge the hourly sweep checks (Feature 5's
  // cross-system contract): if our handle already replied here (worker
  // retry, sweep race, manual phone reply), skip quietly. The sweep
  // pre-marks such comments into state on its next pass.
  try {
    const replies = await igGet(
      "/" + cid + "/replies?fields=id,text,username&limit=30", token);
    if (ownRepliedHere(replies?.data, own)) {
      console.log("comment " + cid + " already answered — skip");
      return;
    }
  } catch (e) {
    /* failed lookup must not block: fall through and reply; a true
       duplicate only surfaces as a harmless extra reply */
  }

  const bank = await loadBank(env);
  const cat = categorize(bank, text);
  let reply = await nimReply(personalityFor(cid, cat.key), text, null, env);
  reply = reply ? tidy(reply, "comment") : null;
  if (!reply) {
    reply = bankReply(bank, "comment_replies", text, cid);
    if (!reply) return;
  }
  try {
    await ig("/" + cid + "/replies", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ message: reply, access_token: token }),
    });
    console.log("replied to comment " + cid + " (" + cat.key + ")");
  } catch (e) {
    console.log("comment reply failed: " + e.message); // sweep catches it later
  }
}

// The DM thread's prior turns, ported from engagement._thread_history:
// [(speaker, text)] oldest-first, our messages "assistant" so the model
// continues the conversation instead of amnesia-answering.
function threadHistory(msgs, own, senderId) {
  const ordered = [...(msgs || [])].sort(
    (a, b) => String(a.created_time || "") < String(b.created_time || "")
      ? -1 : 1);
  // The newest message is the incoming event message — drop it; nimReply
  // appends it fresh as the user turn.
  ordered.pop();
  const history = [];
  for (const m of ordered) {
    const text = String(m.text || "").trim();
    if (!text) continue;
    const speaker = normalize((m.from || {}).username);
    history.push([own.has(speaker) ? "assistant" : "user", text]);
  }
  return history;
}

async function handleMessage(evt, env) {
  const text = String(evt.text || "");
  if (!text) return;
  const token = env.IG_ACCESS_TOKEN;
  const igUser = String(env.IG_USER_ID || "");
  const senderId = String(evt.sender?.id || "");
  if (!text || !token || !igUser || !senderId) return;
  const own = ownSet(env);

  // Follower or not never mattered — the Graph message edge is blind to
  // it (same as the sweep). The 24h window can't be violated here: Meta
  // only fires this event for a message that just arrived.

  // Thread context (memory parity with the sweep): since the worker's
  // own instant reply makes the thread's newest message ours, the hourly
  // sweep would otherwise never revisit the thread — every instant DM
  // conversation would be single-turn. So the worker itself fetches the
  // thread's history and answers in context.
  let tid = senderId; // personality seed fallback: stable per person
  let history = null;
  try {
    const conv = await igGet(
      "/" + igUser + "/conversations?platform=instagram" +
      "&fields=id,users,messages.limit(10){id,from,text,created_time}",
      token);
    const threads = (conv && conv.data) || [];
    const match = threads.find((t) =>
      (t.users || []).some((u) => String(u.id || "") === senderId));
    if (match) {
      tid = String(match.id || tid);
      // Never trust API list order — sort by created_time, exactly like
      // the sweep's max(msgs, key=created_time).
      const msgs = [...((match.messages || {}).data || [])].sort(
        (a, b) => String(a.created_time || "") < String(b.created_time || "")
          ? -1 : 1);
      // Guard the newest-incoming assumption: if the newest message is
      // ours, the thread is already answered — this event is stale.
      // (Same contract as the sweep: newest message ours -> skip.)
      const newest = msgs[msgs.length - 1];
      const newestSender = normalize((newest?.from || {}).username);
      if (newest && own.has(newestSender)) return;
      history = threadHistory(msgs, own, senderId);
    }
  } catch (e) {
    /* history fetch failed — reply single-turn; still beats silence */
  }

  const bank = await loadBank(env);
  const cat = categorize(bank, text);
  let reply = await nimReply(personalityFor(tid, cat.key), text, history, env);
  reply = reply ? tidy(reply) : null;
  if (!reply) {
    reply = bankReply(bank, "dm_replies", text, tid);
    if (!reply) return;
  }
  try {
    await ig("/" + igUser + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient: { id: senderId },
        message: { text: reply },
        // The token rides in the JSON body — never in the URL.
        access_token: token,
      }),
    });
    console.log("replied to DM from " + senderId);
  } catch (e) {
    console.log("DM reply failed: " + e.message); // sweep catches it later
  }
}

// --- payload parsing ----------------------------------------------------------
// Meta's Instagram webhook shape: { object: "instagram", entry: [ { id, time,
//   changes: [{ field: "comments", value: { id, text, from: {id,username} } }],
//   messaging: [{ sender: {id}, recipient: {id}, message: { mid, text } }] } ] }
// changes carries comment events; messaging carries DM events (incoming
// only — Meta does not deliver the account owner's outgoing DMs here).
function extractEvents(body) {
  const out = [];
  for (const entry of body.entry || []) {
    for (const ch of entry.changes || []) {
      if (ch.field === "comments" && ch.value && ch.value.id) {
        out.push({ kind: "comment", value: ch.value });
      }
    }
    for (const m of entry.messaging || []) {
      const text = m.message && m.message.text;
      if (text) {
        out.push({ kind: "message", value: {
          id: m.message.mid || "",
          text,
          sender: m.sender || {},
        } });
      }
    }
  }
  return out;
}

// --- webhook signature check (optional, when APP_SECRET is set) ----------------

async function hmacMatches(secret, payload, header) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  const hex = [...new Uint8Array(sig)]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  return safeEq(hex, (header || "").replace(/^sha256=/, ""));
}
function safeEq(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// --- the worker ----------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // ---- GET: Meta's subscription handshake ---------------------------------
    if (request.method === "GET") {
      if (url.pathname !== "/webhook" && url.pathname !== "/") {
        return new Response("not found", { status: 404 });
      }
      const mode = url.searchParams.get("hub.mode");
      const token = url.searchParams.get("hub.verify_token");
      const challenge = url.searchParams.get("hub.challenge");
      if (mode === "subscribe" && challenge !== null &&
          token === env.WEBHOOK_VERIFY_TOKEN) {
        return new Response(challenge, { status: 200 });
      }
      return new Response("forbidden", { status: 403 });
    }

    // ---- POST: the events ----------------------------------------------------
    if (request.method !== "POST" ||
        (url.pathname !== "/webhook" && url.pathname !== "/")) {
      return new Response(request.method === "POST" ? "not found"
        : "method not allowed",
        { status: request.method === "POST" ? 404 : 405 });
    }

    const raw = await request.text();
    if (env.APP_SECRET && !await hmacMatches(
        env.APP_SECRET, raw, request.headers.get("X-Hub-Signature-256"))) {
      return new Response("forbidden", { status: 403 });
    }
    let body;
    try {
      body = JSON.parse(raw);
    } catch (e) {
      return new Response("bad json", { status: 400 });
    }
    if (body.object !== "instagram") {
      return new Response("ignored", { status: 200 });
    }

    // Always 200 — a handler crash must not make Meta retry forever.
    ctx.waitUntil((async () => {
      for (const evt of extractEvents(body)) {
        try {
          if (evt.kind === "comment") await handleComment(evt.value, env);
          else if (evt.kind === "message") await handleMessage(evt.value, env);
        } catch (e) {
          console.log("handler error: " + (e && e.message));
        }
      }
    })());
    return new Response("ok", { status: 200 });
  },
};
