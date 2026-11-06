/**
 * Payload-parsing check for the instant-reply worker.
 *
 * The worker only ever sees what Meta pushes, and a shape mismatch fails
 * silently (an event Meta sent that we parse into nothing looks exactly
 * like Meta never calling). These are the exact payloads from Meta's
 * Instagram Login webhook docs, plus the echo case that would otherwise
 * make the worker answer its own replies forever.
 *
 * Run:  node tests/test_worker_payload.mjs     (exit 0 = pass)
 */
import { extractEvents } from "../webhook/worker.js";

const bank = [
  {
    name: "comment uses comment_id, not id",
    body: {
      object: "instagram",
      entry: [{ id: "IGID", time: 1, changes: [{
        field: "comments",
        value: {
          from: { id: "IGSID", username: "fan" },
          comment_id: "COMMENT_1",
          text: "love this",
          media: { id: "MEDIA_1", media_product_type: "FEED" },
        },
      }] }],
    },
    want: [{ kind: "comment", id: "COMMENT_1", text: "love this" }],
  },
  {
    name: "message event from messaging[]",
    body: {
      object: "instagram",
      entry: [{ id: "IGID", time: 1, messaging: [{
        sender: { id: "IGSID" },
        recipient: { id: "IGID" },
        timestamp: 1,
        message: { mid: "MID_1", text: "hi" },
      }] }],
    },
    want: [{ kind: "message", id: "MID_1", text: "hi" }],
  },
  {
    // Our own reply comes back flagged is_echo. Replying to this is a loop.
    name: "echo of our own reply is ignored",
    body: {
      object: "instagram",
      entry: [{ id: "IGID", time: 1, messaging: [{
        sender: { id: "IGID" },
        recipient: { id: "IGSID" },
        timestamp: 1,
        message: { mid: "MID_2", text: "our own reply", is_echo: true },
      }] }],
    },
    want: [],
  },
  {
    name: "unrelated change field is ignored",
    body: {
      object: "instagram",
      entry: [{ id: "IGID", time: 1, changes: [{
        field: "story_insights",
        value: { media_id: "M" },
      }] }],
    },
    want: [],
  },
];

let failed = 0;
for (const { name, body, want } of bank) {
  const got = extractEvents(body).map((e) => ({
    kind: e.kind, id: String(e.value.id || ""), text: e.value.text || "",
  }));
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failed++;
  console.log((ok ? "PASS  " : "FAIL  ") + name +
    (ok ? "" : `\n  want ${JSON.stringify(want)}\n  got  ${JSON.stringify(got)}`));
}
console.log(`\n${bank.length - failed}/${bank.length} passed`);
process.exit(failed ? 1 : 0);
